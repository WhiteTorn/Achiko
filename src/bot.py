#!/usr/bin/env python3
import asyncio
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple, List
import mimetypes
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
UPLOAD_ROOT = os.getenv("UPLOAD_ROOT", "").strip()

if not BOT_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN in environment.")
if not ALLOWED_TELEGRAM_USER_ID or not ALLOWED_TELEGRAM_USER_ID.isdigit():
    raise SystemExit("Missing or invalid ALLOWED_TELEGRAM_USER_ID in environment (must be numeric).")
ALLOWED_TELEGRAM_USER_ID = int(ALLOWED_TELEGRAM_USER_ID)
if not DOWNLOAD_ROOT:
    raise SystemExit("Missing DOWNLOAD_ROOT in environment.")
if not UPLOAD_ROOT:
    raise SystemExit("Missing UPLOAD_ROOT in environment.")

DOWNLOAD_DIR = Path(DOWNLOAD_ROOT).expanduser().resolve()
UPLOAD_DIR = Path(UPLOAD_ROOT).expanduser().resolve()
# Ensure directory exists and is private
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
try:
    # Attempt to set restrictive permissions (best-effort on Unix)
    os.chmod(DOWNLOAD_DIR, 0o700)
except Exception:
    pass

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
try:
    # Attempt to set restrictive permissions (best-effort on Unix)
    os.chmod(UPLOAD_DIR, 0o700)
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

# Add these functions after the existing helper functions (around line 120):

def get_upload_files(upload_dir: Path) -> List[Tuple[str, int]]:
    """Get list of files with their sizes from upload directory"""
    files = []
    try:
        for file_path in upload_dir.rglob("*"):
            if file_path.is_file():
                rel_path = file_path.relative_to(upload_dir)
                size = file_path.stat().st_size
                files.append((str(rel_path), size))
    except Exception as e:
        log.error("Error listing upload files: %s", e)
    return sorted(files)

def get_upload_folders(upload_dir: Path) -> List[str]:
    """Get list of folders in upload directory"""
    folders = []
    try:
        # Add root folder option
        folders.append(".")  # Root folder
        
        for item in upload_dir.rglob("*"):
            if item.is_dir():
                rel_path = item.relative_to(upload_dir)
                folders.append(str(rel_path))
    except Exception as e:
        log.error("Error listing upload folders: %s", e)
    return sorted(folders)

def get_files_in_folder(upload_dir: Path, folder_path: str) -> List[Path]:
    """Get all files in a specific folder (non-recursive)"""
    files = []
    try:
        if folder_path == "." or folder_path == "":
            # Root folder - get files directly in upload_dir
            target_dir = upload_dir
        else:
            # Specific subfolder
            target_dir = upload_dir / folder_path
        
        if not target_dir.exists() or not target_dir.is_dir():
            return files
            
        # Get only files in this specific directory (not subdirectories)
        for item in target_dir.iterdir():
            if item.is_file():
                files.append(item)
                
    except Exception as e:
        log.error("Error getting files in folder %s: %s", folder_path, e)
    return sorted(files)

def find_upload_file(upload_dir: Path, filename: str) -> Optional[Path]:
    """Find a file in upload directory (case-insensitive)"""
    try:
        # Try exact match first
        candidate = upload_dir / filename
        if candidate.is_file():
            return candidate
        
        # Try case-insensitive search
        for file_path in upload_dir.rglob("*"):
            if file_path.is_file() and file_path.name.lower() == filename.lower():
                return file_path
    except Exception as e:
        log.error("Error finding file %s: %s", filename, e)
    return None

def find_upload_folder(upload_dir: Path, folder_name: str) -> Optional[str]:
    """Find a folder in upload directory (case-insensitive)"""
    try:
        # Handle root folder
        if folder_name.lower() in [".", "root", ""]:
            return "."
            
        # Try exact match first
        candidate = upload_dir / folder_name
        if candidate.exists() and candidate.is_dir():
            return folder_name
        
        # Try case-insensitive search
        for item in upload_dir.rglob("*"):
            if item.is_dir():
                rel_path = item.relative_to(upload_dir)
                if str(rel_path).lower() == folder_name.lower():
                    return str(rel_path)
    except Exception as e:
        log.error("Error finding folder %s: %s", folder_name, e)
    return None

def format_file_size(size: int) -> str:
    """Format file size in human-readable format"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"

async def send_file_to_telegram(context: ContextTypes.DEFAULT_TYPE, 
                               file_path: Path, 
                               chat_id: int) -> Tuple[bool, str]:
    """Send a file to Telegram chat"""
    try:
        # Initialize mimetypes if needed
        if not mimetypes.inited:
            mimetypes.init()
        
        # Get file info
        mime_type, _ = mimetypes.guess_type(str(file_path))
        file_size = file_path.stat().st_size
        
        # Fallback for unknown mime types
        if not mime_type:
            # Try to guess from extension
            ext = file_path.suffix.lower()
            if ext in ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']:
                mime_type = 'image/' + ext.lstrip('.')
            elif ext in ['.mp4', '.avi', '.mov', '.mkv', '.webm']:
                mime_type = 'video/' + ext.lstrip('.')
            elif ext in ['.mp3', '.wav', '.ogg', '.m4a', '.flac']:
                mime_type = 'audio/' + ext.lstrip('.')
            else:
                mime_type = 'application/octet-stream'  # Default fallback
        
        # Check file size (Telegram limits)
        if file_size > 50 * 1024 * 1024:  # 50MB limit
            return False, "File too large (>50MB). Telegram API limit exceeded."
        
        with open(file_path, 'rb') as f:
            filename = file_path.name
            
            # Send based on file type
            if mime_type and mime_type.startswith('image/'):
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    caption=f"📸 {filename}"
                )
            elif mime_type and mime_type.startswith('video/'):
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=f,
                    caption=f"🎥 {filename}"
                )
            elif mime_type and mime_type.startswith('audio/'):
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=f,
                    caption=f"🎵 {filename}"
                )
            else:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=f,
                    caption=f"📄 {filename}"
                )
        
        return True, f"✅ Successfully sent: {filename}"
        
    except Exception as e:
        log.error("Error sending file %s: %s", file_path, str(e))
        return False, f"❌ Failed to send file: {str(e)}"

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
    
    # Get some stats for the welcome message
    try:
        upload_files = get_upload_files(UPLOAD_DIR)
        upload_count = len(upload_files)
    except:
        upload_count = "?"
    
    await update.message.reply_text(
        "🤖 <b>Achiko</b>\n\n"
        "👋 Hey there! I can help you transfer files both ways:\n\n"
        
        "📥 <b>DOWNLOAD FROM TELEGRAM:</b>\n"
        "   • Just send me any media (photos, videos, documents, audio...)\n"
        f"   • I'll save them to: <code>{DOWNLOAD_DIR}</code>\n\n"
        
        "📤 <b>SEND TO TELEGRAM:</b>\n"
        f"   • <code>/list</code> - Show available files ({upload_count} files ready)\n"
        "   • <code>/folders</code> - Show available folders\n"
        "   • <code>/send filename.ext</code> - Send a specific file\n"
        "   • <code>/send foldername</code> - Send all files in a folder\n"
        f"   • Files are loaded from: <code>{UPLOAD_DIR}</code>\n\n"
        
        "🎯 <b>AVAILABLE COMMANDS:</b>\n"
        "   • <code>/start</code> - Show this help message\n"
        "   • <code>/list</code> - List all files available to send\n"
        "   • <code>/folders</code> - List all folders available to send\n"
        "   • <code>/send &lt;filename&gt;</code> - Send a file from PC to Telegram\n"
        "   • <code>/send &lt;foldername&gt;</code> - Send all files in a folder\n\n"
        
        "📋 <b>EXAMPLES:</b>\n"
        "   • <code>/send document.pdf</code> - Send single file\n"
        "   • <code>/send photos</code> - Send all files in photos folder\n"
        "   • <code>/send .</code> - Send all files in root folder\n"
        "   • <code>/send music/rock</code> - Send all files in music/rock folder\n\n"
        
        "🔒 <b>Security:</b> Only you can use this bot!\n"
        "📁 <b>File limit:</b> Max 50MB per file (Telegram limit)",
        
        parse_mode=ParseMode.HTML
    )

# Add these command handlers after the start() function:

async def handle_list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /list command - show available files"""
    if not user_is_allowed(update) or not is_private_chat(update):
        return
    
    files = get_upload_files(UPLOAD_DIR)
    total_count = len(files)
    
    if not files:
        await update.message.reply_text("📁 No files available to send.")
        return
    
    # Calculate total size of all files
    total_size = sum(size for _, size in files)
    total_size_str = format_file_size(total_size)
    
    # Format file list
    file_list = []
    for filename, size in files[:20]:  # Limit to 20 files to avoid message too long
        size_str = format_file_size(size)
        file_list.append(f"📄 <code>{filename}</code> ({size_str})")
    
    message = f"📊 <b>Total:</b> {total_count} files ({total_size_str})\n"
    message += "📁 <b>Available files to send:</b>\n\n" + "\n".join(file_list)
    
    if len(files) > 20:
        message += f"\n\n<i>... and {len(files) - 20} more files</i>"
    
    message += f"\n\n💡 Use <code>/send filename</code> to send a file"
    
    await update.message.reply_text(message, parse_mode=ParseMode.HTML)

async def handle_send_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /send command - send a specific file or all files in a folder"""
    if not user_is_allowed(update) or not is_private_chat(update):
        return
    
    if not context.args:
        await update.message.reply_text(
            "❗ Please specify a filename or folder name.\n\n"
            "Usage: <code>/send filename.ext</code> or <code>/send foldername</code>\n"
            "Use <code>/list</code> to see available files.\n"
            "Use <code>/folders</code> to see available folders.",
            parse_mode=ParseMode.HTML
        )
        return
    
    target = " ".join(context.args)  # Handle names with spaces
    
    # First, try to find it as a file
    file_path = find_upload_file(UPLOAD_DIR, target)
    
    if file_path:
        # It's a file - send single file
        status_msg = await update.message.reply_text(f"📤 Sending file: <code>{target}</code>...", 
                                                    parse_mode=ParseMode.HTML)
        
        success, message = await send_file_to_telegram(context, file_path, update.effective_chat.id)
        await status_msg.edit_text(message, parse_mode=ParseMode.HTML)
        return
    
    # Not a file, try to find it as a folder
    folder_path = find_upload_folder(UPLOAD_DIR, target)
    
    if folder_path is not None:
        # It's a folder - send all files in the folder
        files_in_folder = get_files_in_folder(UPLOAD_DIR, folder_path)
        
        if not files_in_folder:
            await update.message.reply_text(
                f"📁 Folder found but contains no files: <code>{target}</code>",
                parse_mode=ParseMode.HTML
            )
            return
        
        folder_display = "root" if folder_path == "." else folder_path
        status_msg = await update.message.reply_text(
            f"📁 Sending {len(files_in_folder)} files from folder: <code>{folder_display}</code>...",
            parse_mode=ParseMode.HTML
        )
        
        # Send all files in the folder
        sent_count = 0
        failed_count = 0
        failed_files = []
        
        for file_path in files_in_folder:
            success, message = await send_file_to_telegram(context, file_path, update.effective_chat.id)
            if success:
                sent_count += 1
            else:
                failed_count += 1
                failed_files.append(file_path.name)
                log.error("Failed to send %s: %s", file_path.name, message)
            
            # Small delay to avoid hitting rate limits
            await asyncio.sleep(0.5)
        
        # Send summary
        summary = f"📁 <b>Folder send complete!</b>\n\n"
        summary += f"✅ Successfully sent: {sent_count} files\n"
        if failed_count > 0:
            summary += f"❌ Failed: {failed_count} files\n"
            if failed_files:
                summary += f"Failed files: {', '.join(failed_files[:5])}"
                if len(failed_files) > 5:
                    summary += f" and {len(failed_files) - 5} more..."
        
        await status_msg.edit_text(summary, parse_mode=ParseMode.HTML)
        return
    
    # Neither file nor folder found
    await update.message.reply_text(
        f"❌ File or folder not found: <code>{target}</code>\n\n"
        f"Use <code>/list</code> to see available files.\n"
        f"Use <code>/folders</code> to see available folders.",
        parse_mode=ParseMode.HTML
    )


async def handle_folders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /folders command - show available folders"""
    if not user_is_allowed(update) or not is_private_chat(update):
        return
    
    folders = get_upload_folders(UPLOAD_DIR)
    
    if not folders:
        await update.message.reply_text("📁 No folders available.")
        return
    
    message = "📁 <b>Available folders:</b>\n\n"
    
    for folder in folders[:20]:  # Limit to avoid too long message
        if folder == ".":
            # Count files in root
            files_in_root = get_files_in_folder(UPLOAD_DIR, ".")
            message += f"📂 <code>. (root)</code> ({len(files_in_root)} files)\n"
        else:
            # Count files in subfolder  
            files_in_folder = get_files_in_folder(UPLOAD_DIR, folder)
            message += f"📂 <code>{folder}</code> ({len(files_in_folder)} files)\n"
    
    if len(folders) > 20:
        message += f"\n<i>... and {len(folders) - 20} more folders</i>"
    
    message += "\n💡 Use <code>/send foldername</code> to send all files in a folder"
    message += "\n💡 Use <code>/send .</code> to send all files in root folder"
    
    await update.message.reply_text(message, parse_mode=ParseMode.HTML)


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
    # Add upload commands
    app.add_handler(CommandHandler(
        "list",
        handle_list_command,
        filters=filters.ChatType.PRIVATE & filters.User(ALLOWED_TELEGRAM_USER_ID)
    ))
    
    app.add_handler(CommandHandler(
        "send",
        handle_send_command,
        filters=filters.ChatType.PRIVATE & filters.User(ALLOWED_TELEGRAM_USER_ID)
    ))
    
    app.add_handler(CommandHandler(
        "folders",
        handle_folders_command,
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
