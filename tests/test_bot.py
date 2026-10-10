from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TimedOut

from bot import CopyService, DedupStore, Settings, parse_chat_id, retry_telegram_call


def settings(tmp_path: Path) -> Settings:
    return Settings("token", -1001, -1002, tmp_path / "state.sqlite3", 0.01, 3)


def test_parse_chat_id() -> None:
    assert parse_chat_id(" -100123 ") == -100123
    assert parse_chat_id("@source") == "@source"
    with pytest.raises(ValueError):
        parse_chat_id("source")


def test_dedup_store_persists(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = DedupStore(path)
    assert not store.contains_any(-1001, [10, 11])
    store.mark_copied(-1001, [10, 11])
    store.close()
    reopened = DedupStore(path)
    assert reopened.contains_any(-1001, [10])
    assert reopened.contains_any(-1001, [11, 12])
    reopened.close()


@pytest.mark.asyncio
async def test_retry_transient_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("bot.asyncio.sleep", AsyncMock())
    operation = AsyncMock(side_effect=[TimedOut(), "ok"])
    assert await retry_telegram_call(operation, 3) == "ok"
    assert operation.await_count == 2


@pytest.mark.asyncio
async def test_single_message_is_copied_once(tmp_path: Path) -> None:
    store = DedupStore(tmp_path / "state.sqlite3")
    service = CopyService(settings(tmp_path), store)
    bot = SimpleNamespace(copy_message=AsyncMock(return_value=object()), copy_messages=AsyncMock())
    context = SimpleNamespace(bot=bot)

    await service._copy_ids(context, [42])
    await service._copy_ids(context, [42])

    bot.copy_message.assert_awaited_once_with(chat_id=-1002, from_chat_id=-1001, message_id=42)
    bot.copy_messages.assert_not_awaited()
    store.close()


@pytest.mark.asyncio
async def test_album_uses_copy_messages(tmp_path: Path) -> None:
    store = DedupStore(tmp_path / "state.sqlite3")
    service = CopyService(settings(tmp_path), store)
    bot = SimpleNamespace(copy_message=AsyncMock(), copy_messages=AsyncMock(return_value=[]))
    context = SimpleNamespace(bot=bot)

    await service._copy_ids(context, [7, 8, 9])

    bot.copy_messages.assert_awaited_once_with(
        chat_id=-1002, from_chat_id=-1001, message_ids=[7, 8, 9]
    )
    bot.copy_message.assert_not_awaited()
    store.close()
