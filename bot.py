"""Telegram channel auto-copy bot.

Copies new channel posts without a forwarded tag. Runtime state is stored in SQLite
so a redelivered update is not posted twice after a restart.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import sqlite3
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Awaitable, Callable, Sequence, TypeVar

from dotenv import load_dotenv
from telegram import Message, Update
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import AIORateLimiter, Application, ApplicationBuilder, ContextTypes, MessageHandler, filters

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


def parse_chat_id(raw: str) -> int | str:
    """Parse a numeric Telegram ID, or accept an @channel username."""
    value = raw.strip()
    if not value:
        raise ValueError("channel ID cannot be empty")
    if value.startswith("@") and len(value) > 1:
        return value
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError("channel ID must be numeric or an @channel username") from exc


@dataclass(frozen=True)
class Settings:
    bot_token: str
    source_chat_id: int | str
    destination_chat_id: int | str
    database_path: Path
    album_wait_seconds: float
    max_retries: int

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv()
        missing = [name for name in ("BOT_TOKEN", "SOURCE_CHANNEL_ID", "DEST_CHANNEL_ID") if not os.getenv(name)]
        if missing:
            raise ValueError(f"Missing required environment variable(s): {', '.join(missing)}")

        album_wait = float(os.getenv("ALBUM_WAIT_SECONDS", "1.0"))
        max_retries = int(os.getenv("MAX_RETRIES", "5"))
        if album_wait <= 0:
            raise ValueError("ALBUM_WAIT_SECONDS must be greater than zero")
        if max_retries < 1:
            raise ValueError("MAX_RETRIES must be at least 1")

        return cls(
            bot_token=os.environ["BOT_TOKEN"].strip(),
            source_chat_id=parse_chat_id(os.environ["SOURCE_CHANNEL_ID"]),
            destination_chat_id=parse_chat_id(os.environ["DEST_CHANNEL_ID"]),
            database_path=Path(os.getenv("DATABASE_PATH", "data/copybot.sqlite3")),
            album_wait_seconds=album_wait,
            max_retries=max_retries,
        )


class DedupStore:
    """Durable, atomic record of source messages that have been copied."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path)
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS copied_messages (
                source_chat_id TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                copied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (source_chat_id, message_id)
            )
            """
        )
        self._connection.commit()

    def contains_any(self, source_chat_id: int | str, message_ids: Sequence[int]) -> bool:
        if not message_ids:
            return False
        placeholders = ",".join("?" for _ in message_ids)
        row = self._connection.execute(
            f"SELECT 1 FROM copied_messages WHERE source_chat_id = ? AND message_id IN ({placeholders}) LIMIT 1",
            (str(source_chat_id), *message_ids),
        ).fetchone()
        return row is not None

    def mark_copied(self, source_chat_id: int | str, message_ids: Sequence[int]) -> None:
        with self._connection:
            self._connection.executemany(
                "INSERT OR IGNORE INTO copied_messages (source_chat_id, message_id) VALUES (?, ?)",
                ((str(source_chat_id), message_id) for message_id in message_ids),
            )

    def close(self) -> None:
        self._connection.close()


async def retry_telegram_call(operation: Callable[[], Awaitable[T]], max_retries: int) -> T:
    """Retry Telegram/network failures with flood-wait-aware backoff."""
    for attempt in range(1, max_retries + 1):
        try:
            return await operation()
        except RetryAfter as exc:
            if attempt == max_retries:
                raise
            retry_after = exc.retry_after
            seconds = retry_after.total_seconds() if isinstance(retry_after, timedelta) else float(retry_after)
            delay = seconds + 0.25
            LOGGER.warning("Telegram rate limit reached; retrying in %.2f seconds", delay)
        except (TimedOut, NetworkError) as exc:
            if attempt == max_retries:
                raise
            delay = min(2 ** (attempt - 1), 30) + random.uniform(0, 0.25)
            LOGGER.warning("Temporary Telegram error (%s); retrying in %.2f seconds", exc, delay)
        await asyncio.sleep(delay)
    raise RuntimeError("retry loop exited unexpectedly")


class CopyService:
    def __init__(self, settings: Settings, store: DedupStore) -> None:
        self.settings = settings
        self.store = store
        self._albums: dict[str, list[Message]] = {}
        self._album_versions: dict[str, int] = {}
        self._album_tasks: dict[str, asyncio.Task[None]] = {}
        self._copy_lock = asyncio.Lock()

    def _is_source(self, message: Message) -> bool:
        configured = self.settings.source_chat_id
        if isinstance(configured, int):
            return message.chat_id == configured
        return message.chat.username is not None and f"@{message.chat.username}".casefold() == configured.casefold()

    async def handle_channel_post(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.channel_post
        if message is None or not self._is_source(message):
            return

        if message.media_group_id:
            group_id = message.media_group_id
            self._albums.setdefault(group_id, []).append(message)
            self._album_versions[group_id] = self._album_versions.get(group_id, 0) + 1
            if group_id not in self._album_tasks:
                task = context.application.create_task(
                    self._flush_album_after_delay(group_id, context),
                    update=update,
                    name=f"copy-album-{group_id}",
                )
                self._album_tasks[group_id] = task
            return

        await self._copy_ids(context, [message.message_id])

    async def _flush_album_after_delay(self, group_id: str, context: ContextTypes.DEFAULT_TYPE) -> None:
        try:
            # Debounce until no new item has joined this album for a full window.
            version = self._album_versions[group_id]
            while True:
                await asyncio.sleep(self.settings.album_wait_seconds)
                current_version = self._album_versions[group_id]
                if current_version == version:
                    break
                version = current_version
            messages = self._albums.pop(group_id, [])
            self._album_versions.pop(group_id, None)
            message_ids = sorted({message.message_id for message in messages})
            if message_ids:
                await self._copy_ids(context, message_ids)
        finally:
            self._album_tasks.pop(group_id, None)

    async def _copy_ids(self, context: ContextTypes.DEFAULT_TYPE, message_ids: Sequence[int]) -> None:
        async with self._copy_lock:
            if self.store.contains_any(self.settings.source_chat_id, message_ids):
                LOGGER.info("Skipping already copied source message(s): %s", message_ids)
                return

            async def copy() -> object:
                if len(message_ids) == 1:
                    return await context.bot.copy_message(
                        chat_id=self.settings.destination_chat_id,
                        from_chat_id=self.settings.source_chat_id,
                        message_id=message_ids[0],
                    )
                return await context.bot.copy_messages(
                    chat_id=self.settings.destination_chat_id,
                    from_chat_id=self.settings.source_chat_id,
                    message_ids=message_ids,
                )

            await retry_telegram_call(copy, self.settings.max_retries)
            self.store.mark_copied(self.settings.source_chat_id, message_ids)
            LOGGER.info("Copied source message(s): %s", message_ids)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, TelegramError):
        LOGGER.error("Telegram error while processing an update: %s", context.error)
    else:
        error = context.error
        LOGGER.error(
            "Unhandled error while processing an update",
            exc_info=(type(error), error, error.__traceback__),
        )


def build_application(settings: Settings) -> tuple[Application, DedupStore]:
    store = DedupStore(settings.database_path)
    service = CopyService(settings, store)
    application = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .rate_limiter(AIORateLimiter(max_retries=0))
        .build()
    )
    application.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, service.handle_channel_post))
    application.add_error_handler(error_handler)
    application.bot_data["copy_service"] = service
    return application, store


def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs full Telegram API URLs, which contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env()
        application, store = build_application(settings)
    except (ValueError, OSError) as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc

    LOGGER.info("Starting polling for source channel %s", settings.source_chat_id)
    try:
        application.run_polling(allowed_updates=["channel_post"], drop_pending_updates=False)
    finally:
        store.close()


if __name__ == "__main__":
    main()
