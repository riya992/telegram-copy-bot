# Telegram Auto-Copy Bot

A production-oriented Python bot that watches one Telegram channel and copies each new post to another channel without Telegram's **Forwarded from** label. It supports text, photos, videos, documents, captions, and media albums.

## How it works

- Polls Telegram for new `channel_post` updates.
- Uses `copy_message` for individual posts and `copy_messages` for albums, preserving their order and grouping.
- Records copied source message IDs in SQLite to reject duplicate updates across restarts.
- Uses `AIORateLimiter`, honors Telegram `RetryAfter` flood waits, and retries temporary network failures with exponential backoff.
- Does not download or re-upload media, so captions and formatting are retained by Telegram.

Only posts published after the bot begins receiving updates are copied. Edited posts are intentionally ignored. Telegram service messages and protected content cannot be copied by the Bot API.

## Telegram setup

1. Open [@BotFather](https://t.me/BotFather), run `/newbot`, and save the token securely.
2. Add the bot to the **source channel** as an administrator. It needs permission to see channel posts.
3. Add the bot to the **destination channel** as an administrator with **Post Messages** permission.
4. Find both channel IDs. A private channel ID usually looks like `-1001234567890`. You can temporarily add an ID-helper bot, inspect an update with the Bot API, or use public `@channelusername` values. Numeric IDs are the most reliable.

Do not run another polling process with the same token. Telegram permits only one `getUpdates` consumer per bot token.

## Local installation

Python 3.11 or newer is recommended.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and replace the three required values:

```dotenv
BOT_TOKEN=your_real_token
SOURCE_CHANNEL_ID=-1001234567890
DEST_CHANNEL_ID=-1009876543210
```

Start the bot:

```bash
python bot.py
```

The SQLite database is created at `data/copybot.sqlite3`. Keep this file on persistent storage in production; deleting it resets duplicate history.

## Step-by-step test

1. Start `python bot.py` and confirm the log says polling has started.
2. Publish a plain text post in the source channel. Confirm it appears in the destination without a forwarded tag.
3. Publish one photo with a formatted caption, then a video or document. Confirm media and captions match.
4. Publish a multi-photo/video album. Wait a couple of seconds and confirm it arrives as one album in the original order.
5. Restart the bot. Existing posts should not be copied again; only new posts should appear.
6. Run the automated checks with `pip install -r requirements-dev.txt && pytest`.

If nothing arrives, verify the channel IDs, the bot's administrator roles, and that no second instance uses the token. Increase `LOG_LEVEL=DEBUG` temporarily for diagnosis.

## 24/7 deployment

### Docker Compose on a VPS (recommended)

Install Docker, upload this project, create `.env`, then run:

```bash
docker compose up -d --build
docker compose logs -f telegram-copy-bot
```

The included named volume preserves SQLite state and `restart: unless-stopped` brings the bot back after crashes or host reboots. Back up the `copybot-data` volume periodically.

Update it with:

```bash
docker compose up -d --build
```

### systemd on a Linux VPS

Install the project in `/opt/telegram-copy-bot`, create its virtual environment and `.env`, then create `/etc/systemd/system/telegram-copy-bot.service`:

```ini
[Unit]
Description=Telegram Auto-Copy Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=copybot
WorkingDirectory=/opt/telegram-copy-bot
EnvironmentFile=/opt/telegram-copy-bot/.env
ExecStart=/opt/telegram-copy-bot/.venv/bin/python /opt/telegram-copy-bot/bot.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable it with `sudo systemctl daemon-reload && sudo systemctl enable --now telegram-copy-bot`, and view logs with `journalctl -u telegram-copy-bot -f`.

### Render/Railway/Fly.io

Deploy as a background worker using `python bot.py` (the included `Procfile` supplies this command). Set the three required environment variables in the provider dashboard and attach a persistent disk mounted at `/app/data`. Free tiers that sleep are not suitable for uninterrupted copying.

## Operational notes

- Keep exactly one running replica because polling and the local SQLite store are single-instance by design.
- Rotate the BotFather token immediately if it is exposed.
- There is a narrow unavoidable crash window after Telegram accepts a copy but before SQLite commits it; Telegram's copy methods do not provide an idempotency key. On a hard crash in that instant, one duplicate can occur.
- Polling is suitable for a single always-on worker. A later move to webhooks changes update delivery but not the copy logic.
