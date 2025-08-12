# Telegram Media Download Bot (Single-user, Secure-by-default)

This bot downloads any media you send or forward to it (photos, videos, documents, audio, voice, animations, video notes) into a local folder on an Ubuntu host. It only responds to a single allowed user ID.

## Features (v1)
- Long polling (no inbound ports required).
- Single allowed user; ignores everyone else.
- Private chats only.
- Supports photo, video, document, audio, voice, animation, video note.
- Sanitized filenames; safe path checks.
- Success/failure user messages that include the saved path or reason.

## Setup

1. System requirements
   - Python 3.10+ on Ubuntu.
   - A dedicated system user is recommended (e.g., `tgdownloader`).

2. Create a virtual environment and install dependencies:
   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -r requirements.txt
   ```

3. Configure environment:
   - Copy `.env.example` to `.env` and set:
     - `TELEGRAM_BOT_TOKEN`
     - `ALLOWED_TELEGRAM_USER_ID` (numeric)
     - `DOWNLOAD_ROOT` (e.g., `/srv/tg-downloads`)
   - Ensure the download directory exists (the bot will create it) and is owned by your bot user.

4. Run the bot:
   ```bash
   . .venv/bin/activate
   python src/bot.py
   ```

5. Get your numeric Telegram user ID (if needed):
   - Use `@userinfobot` or `@MyTelegramID_bot`, or add a temporary `/start` handler and log `update.effective_user.id`.

## Hardening tips
- Run as a dedicated Linux user with minimal permissions:
  ```bash
  sudo useradd --system --create-home --shell /usr/sbin/nologin tgdownloader
  sudo mkdir -p /srv/tg-downloads
  sudo chown tgdownloader:tgdownloader /srv/tg-downloads
  sudo chmod 700 /srv/tg-downloads
  ```
- Use systemd with sandboxing (PrivateTmp, ProtectSystem, NoNewPrivileges).
- Keep `.env` readable only by the bot user (e.g., `chmod 600 .env`).
- Pin versions in `requirements.txt` and update periodically.

## Notes
- For forwarded media, Telegram exposes the same media kinds on the message; the bot treats them the same.
- If a message contains no downloadable media, the bot replies with a short notice.
- Filenames: prefer original file name (when provided) sanitized; otherwise, timestamp + unique id + extension.

## Next steps
- v1.1: systemd unit, resource limits, max file size guard.
- v2: sort into type-based subfolders, optional daily subfolders, dedup policies.
- v3: webhook mode behind HTTPS and reverse proxy.
- v4: cleanup/backup tasks, multi-user allowlist (if needed).
