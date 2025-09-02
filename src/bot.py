#!/usr/bin/env python3
import asyncio
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple
import signal

from dotenv import load_dotenv
from telegram import Update, constants, File
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,  # added
    filters,
)
from telegram.constants import ParseMode

# ---------------------------
# Configuration and logging
# ---------------------------
load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_TELEGRAM_USER_ID = os.getenv("ALLOWED_TELEGRAM_USER_ID", "").strip()
DOWNLOAD_ROOT = os.getenv("DOWNLOAD_ROOT", "").strip()

if not BOT_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN in environment.")
if not ALLOWED_TELEGRAM_USER_ID or not ALLOWED_TELEGRAM_USER_ID.isdigit():
    raise SystemExit("Missing or invalid ALLOWED_TELEGRAM_USER_ID in environment (must be numeric).")
ALLOWED_TELEGRAM_USER_ID = int(ALLOWED_TELEGRAM_USER_ID)
if not DOWNLOAD_ROOT:
    raise SystemExit("Missing DOWNLOAD_ROOT in environment.")

DOWNLOAD_DIR = Path(DOWNLOAD_ROOT).expanduser().resolve()

# Ensure directory exists and is private
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
try:
    # Attempt to set restrictive permissions (best-effort on Unix)
    os.chmod(DOWNLOAD_DIR, 0o700)
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s"
)
log = logging.getLogger("tgdownloader")

# ---------------------------
# Helpers
# ---------------------------

SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

def sanitize_filename(name: str) -> str:
    # Strip directory components and sanitize
    name = os.path.basename(name)
    # Collapse spaces and unsafe chars
    name = SAFE_NAME_RE.sub("_", name).strip("._ ")
    # Avoid empty name
    return name or "file"

def safe_join(root: Path, filename: str) -> Path:
    candidate = (root / filename).resolve()
    root_resolved = root.resolve()
    if not str(candidate).startswith(str(root_resolved) + os.sep) and candidate != root_resolved:
        # Prevent path traversal outside root
        raise ValueError("Unsafe path resolution detected.")
    return candidate

def utc_stamp() -> str:
    return datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")

async def get_telegram_file(context: ContextTypes.DEFAULT_TYPE, file_id: str) -> File:
    return await context.bot.get_file(file_id=file_id)

def guess_extension_from_file_path(file_path: Optional[str]) -> str:
    if not file_path:
        return ""
    # file_path often includes extension, e.g., documents/file_12345.mp4
    ext = Path(file_path).suffix
    if ext and len(ext) <= 10:
        return ext
    return ""

async def download_file(file_obj: File, dest_path: Path) -> Tuple[bool, Optional[str]]:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    # Best effort to prevent overwriting
    try:
        if hasattr(file_obj, "download_to_drive"):
            await file_obj.download_to_drive(custom_path=str(dest_path))
        elif hasattr(file_obj, "download"):
            # Fallback for older PTB variants
            maybe_coro = file_obj.download(custom_path=str(dest_path))  # type: ignore
            if asyncio.iscoroutine(maybe_coro):
                await maybe_coro
        else:
            return False, "No suitable download method available on File object."
        return True, None
    except Exception as e:
        return False, str(e)

def user_is_allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == ALLOWED_TELEGRAM_USER_ID)

def is_private_chat(update: Update) -> bool:
    chat = update.effective_chat
    return bool(chat and chat.type == "private")

# ---------------------------
# Core handler
# ---------------------------

MEDIA_FILTER = (
    filters.PHOTO
    | filters.VIDEO
    | filters.Document.ALL
    | filters.AUDIO
    | filters.VOICE
    | filters.VIDEO_NOTE
    | filters.ANIMATION
    # Uncomment to include stickers as files:
    # | filters.Sticker.ALL
)

async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not user_is_allowed(update) or not is_private_chat(update):
        # Ignore silently for security
        return

    msg = update.effective_message
    if not msg:
        return

    # Determine which media object exists on this message
    tasks = []

    async def process_one(file_id: str, original_name: Optional[str], fallback_base: str) -> None:
        try:
            tg_file = await get_telegram_file(context, file_id)
            # Prefer original filename when present (sanitized)
            if original_name:
                base = sanitize_filename(original_name)
                ext = Path(base).suffix or guess_extension_from_file_path(getattr(tg_file, "file_path", None))
                stem = Path(base).stem if Path(base).suffix else base
                final_name = f"{utc_stamp()}_{stem}{ext}"
            else:
                # Build name from unique id + guessed extension
                ext = guess_extension_from_file_path(getattr(tg_file, "file_path", None))
                final_name = f"{utc_stamp()}_{fallback_base}{ext}"

            dest = safe_join(DOWNLOAD_DIR, final_name)
            ok, err = await download_file(tg_file, dest)
            if ok:
                await msg.reply_text(f"Download complete. Saved to: {dest}")
                log.info("Downloaded %s -> %s", file_id, dest)
            else:
                await msg.reply_text(f"Download failed: {err}")
                log.error("Download failed for %s: %s", file_id, err)
        except Exception as e:
            await msg.reply_text(f"Download failed: {e}")
            log.exception("Unhandled error while processing media: %s", e)

    # Photos are a list of sizes; pick the largest
    if msg.photo:
        largest = msg.photo[-1]
        tasks.append(process_one(largest.file_id, None, f"photo_{largest.file_unique_id}"))

    if msg.video:
        v = msg.video
        tasks.append(process_one(v.file_id, v.file_name, f"video_{v.file_unique_id}"))

    if msg.document:
        d = msg.document
        tasks.append(process_one(d.file_id, d.file_name, f"document_{d.file_unique_id}"))

    if msg.audio:
        a = msg.audio
        tasks.append(process_one(a.file_id, a.file_name, f"audio_{a.file_unique_id}"))

    if msg.voice:
        vc = msg.voice
        tasks.append(process_one(vc.file_id, None, f"voice_{vc.file_unique_id}"))

    if msg.video_note:
        vn = msg.video_note
        tasks.append(process_one(vn.file_id, None, f"videonote_{vn.file_unique_id}"))

    if msg.animation:
        an = msg.animation
        # Animation can have file_name (e.g., GIF)
        tasks.append(process_one(an.file_id, an.file_name, f"animation_{an.file_unique_id}"))

    # If you want stickers, uncomment MEDIA_FILTER above and include here:
    # if msg.sticker:
    #     st = msg.sticker
    #     tasks.append(process_one(st.file_id, None, f"sticker_{st.file_unique_id}"))

    if not tasks:
        await msg.reply_text("No downloadable media found in this message.")
        return

    # Run sequentially for simplicity and lower resource usage.
    for t in tasks:
        await t

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not user_is_allowed(update) or not is_private_chat(update):
        return
    await update.message.reply_text(
        "👋 <b>Hey there!</b>\n\n"
        "I'm all set and ready to help you.\n\n"
        "📩 Just send or forward me any <i>media</i> (photos, videos, documents...)\n\n"
        "💾 I’ll save everything neatly into:\n"
        f"   <code>{DOWNLOAD_DIR}</code>\n\n"
        "🔒 Don’t worry — only <b>you</b> can use this bot!",
        parse_mode=ParseMode.HTML
    )

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Error handling update: %s", context.error)

def build_app() -> Application:
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.User(ALLOWED_TELEGRAM_USER_ID) & MEDIA_FILTER,
        handle_media
    ))
    # Register /start for the allowed user in a private chat
    app.add_handler(CommandHandler(
        "start",
        start,
        filters=filters.ChatType.PRIVATE & filters.User(ALLOWED_TELEGRAM_USER_ID)
    ))
    app.add_error_handler(error_handler)
    return app

def main() -> None:
    app = build_app()
    log.info("Starting bot with long polling. Allowed user id: %s. Download dir: %s",
             ALLOWED_TELEGRAM_USER_ID, DOWNLOAD_DIR)
    # Long polling; restrict updates to messages only and drop pending to avoid backlog
    app.run_polling(
        allowed_updates=[constants.UpdateType.MESSAGE],
        drop_pending_updates=True,
        poll_interval=1.5,
        timeout=30,
        stop_signals=(signal.SIGINT, signal.SIGTERM),
    )

if __name__ == "__main__":
    main()
