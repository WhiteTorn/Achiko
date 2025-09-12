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
from telegram import Update, constants, File, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    CommandHandler,  # added
    CallbackQueryHandler,  # added for inline keyboards
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
    """Get list of files with their sizes from both upload and download directories"""
    files = []
    
    # Get files from upload directory
    try:
        for file_path in upload_dir.rglob("*"):
            if file_path.is_file():
                rel_path = file_path.relative_to(upload_dir)
                size = file_path.stat().st_size
                files.append((str(rel_path), size))
    except Exception as e:
        log.error("Error listing upload files: %s", e)
    
    # Get files from download directory  
    try:
        for file_path in DOWNLOAD_DIR.rglob("*"):
            if file_path.is_file():
                rel_path = file_path.relative_to(DOWNLOAD_DIR)
                size = file_path.stat().st_size
                files.append((str(rel_path), size))
    except Exception as e:
        log.error("Error listing download files: %s", e)
    
    return sorted(files)

def get_upload_folders(upload_dir: Path) -> List[str]:
    """Get list of folders from both upload and download directories"""
    folders = []
    
    # Get folders from upload directory
    try:
        # Add root folder option for uploads
        folders.append(".")  # Root upload folder
        
        for item in upload_dir.rglob("*"):
            if item.is_dir():
                rel_path = item.relative_to(upload_dir)
                folders.append(str(rel_path))
    except Exception as e:
        log.error("Error listing upload folders: %s", e)
    
    # Get folders from download directory
    try:
        # Add root folder option for downloads
        folders.append(".-d")  # Root download folder with -d suffix
        
        for item in DOWNLOAD_DIR.rglob("*"):
            if item.is_dir():
                rel_path = item.relative_to(DOWNLOAD_DIR)
                folders.append(str(rel_path) + "-d")  # Add -d suffix for download folders
    except Exception as e:
        log.error("Error listing download folders: %s", e)
    
    return sorted(folders)

def get_files_in_folder(upload_dir: Path, folder_path: str) -> List[Path]:
    """Get all files in a specific folder (non-recursive) from upload or download directory"""
    files = []
    is_download = folder_path.endswith("-d")
    
    try:
        if is_download:
            # Remove -d suffix and use download directory
            actual_folder_path = folder_path[:-2]  # Remove "-d"
            if actual_folder_path == "." or actual_folder_path == "":
                # Root download folder
                target_dir = DOWNLOAD_DIR
            else:
                # Specific subfolder in downloads
                target_dir = DOWNLOAD_DIR / actual_folder_path
        else:
            # Upload directory
            if folder_path == "." or folder_path == "":
                # Root upload folder
                target_dir = upload_dir
            else:
                # Specific subfolder in uploads
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
    """Find a file in upload or download directory (case-insensitive)"""
    try:
        # First search in upload directory
        candidate = upload_dir / filename
        if candidate.is_file():
            return candidate
        
        # Case-insensitive search in upload directory
        for file_path in upload_dir.rglob("*"):
            if file_path.is_file() and file_path.name.lower() == filename.lower():
                return file_path
        
        # Then search in download directory
        candidate = DOWNLOAD_DIR / filename
        if candidate.is_file():
            return candidate
            
        # Case-insensitive search in download directory
        for file_path in DOWNLOAD_DIR.rglob("*"):
            if file_path.is_file() and file_path.name.lower() == filename.lower():
                return file_path
                
    except Exception as e:
        log.error("Error finding file %s: %s", filename, e)
    return None

def find_upload_folder(upload_dir: Path, folder_name: str) -> Optional[str]:
    """Find a folder in upload or download directory (case-insensitive)"""
    try:
        is_download = folder_name.endswith("-d")
        
        if is_download:
            # Download folder
            actual_folder_name = folder_name[:-2]  # Remove "-d"
            base_dir = DOWNLOAD_DIR
        else:
            # Upload folder
            actual_folder_name = folder_name
            base_dir = upload_dir
        
        # Handle root folder
        if actual_folder_name.lower() in [".", "root", ""]:
            return ".-d" if is_download else "."
            
        # Try exact match first
        candidate = base_dir / actual_folder_name
        if candidate.exists() and candidate.is_dir():
            return folder_name  # Return original name with -d suffix if applicable
        
        # Try case-insensitive search
        for item in base_dir.rglob("*"):
            if item.is_dir():
                rel_path = item.relative_to(base_dir)
                if str(rel_path).lower() == actual_folder_name.lower():
                    return str(rel_path) + ("-d" if is_download else "")
                    
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

async def setup_bot_commands(application: Application) -> None:
    """Set up bot commands that appear in Telegram's native command menu"""
    commands = [
        BotCommand("start", "🏠 Show welcome message and main menu"),
        BotCommand("menu", "📋 Show interactive main menu"),
        BotCommand("list", "📄 List all available files to send"),
        BotCommand("folders", "📁 List all available folders"),
        BotCommand("listfolder", "📂 List files in a specific folder"),
        BotCommand("send", "📤 Send a file or folder to Telegram"),
    ]
    
    try:
        await application.bot.set_my_commands(commands)
        log.info("Bot commands set successfully")
    except Exception as e:
        log.error("Failed to set bot commands: %s", e)

def create_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Create the main menu inline keyboard"""
    keyboard = [
        [
            InlineKeyboardButton("📄 List Files", callback_data="cmd_list"),
            InlineKeyboardButton("📁 List Folders", callback_data="cmd_folders")
        ],
        [
            InlineKeyboardButton("📤 Send File", callback_data="cmd_send_file"),
            InlineKeyboardButton("📂 Send Folder", callback_data="cmd_send_folder")
        ],
        [
            InlineKeyboardButton("🔄 Refresh Menu", callback_data="cmd_menu"),
            InlineKeyboardButton("ℹ️ Help", callback_data="cmd_help")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def create_files_menu_keyboard(files: List[Tuple[str, int]], page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    """Create inline keyboard for file selection"""
    keyboard = []
    
    start_idx = page * per_page
    end_idx = min(start_idx + per_page, len(files))
    
    # Add file buttons (2 per row)
    for i in range(start_idx, end_idx, 2):
        row = []
        # First file in row
        filename, size = files[i]
        size_str = format_file_size(size)
        # Truncate long filenames for button display
        display_name = filename[:25] + "..." if len(filename) > 25 else filename
        row.append(InlineKeyboardButton(
            f"📄 {display_name} ({size_str})",
            callback_data=f"send_file:{filename}"
        ))
        
        # Second file in row (if exists)
        if i + 1 < end_idx:
            filename2, size2 = files[i + 1]
            size_str2 = format_file_size(size2)
            display_name2 = filename2[:25] + "..." if len(filename2) > 25 else filename2
            row.append(InlineKeyboardButton(
                f"📄 {display_name2} ({size_str2})",
                callback_data=f"send_file:{filename2}"
            ))
        
        keyboard.append(row)
    
    # Add navigation buttons
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"files_page:{page-1}"))
    if end_idx < len(files):
        nav_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"files_page:{page+1}"))
    
    if nav_row:
        keyboard.append(nav_row)
    
    # Add back to menu button
    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="cmd_menu")])
    
    return InlineKeyboardMarkup(keyboard)

def create_folders_menu_keyboard(folders: List[str], page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    """Create inline keyboard for folder selection"""
    keyboard = []
    
    start_idx = page * per_page
    end_idx = min(start_idx + per_page, len(folders))
    
    # Add folder buttons (2 per row)
    for i in range(start_idx, end_idx, 2):
        row = []
        # First folder in row
        folder = folders[i]
        display_name = "🏠 Root" if folder == "." else f"📁 {folder}"
        if folder.endswith("-d"):
            actual_name = folder[:-2]
            display_name = f"📥 {actual_name}" if actual_name != "." else "📥 Root-Downloads"
        
        # Truncate long folder names
        if len(display_name) > 30:
            display_name = display_name[:27] + "..."
            
        row.append(InlineKeyboardButton(display_name, callback_data=f"send_folder:{folder}"))
        
        # Second folder in row (if exists)
        if i + 1 < end_idx:
            folder2 = folders[i + 1]
            display_name2 = "🏠 Root" if folder2 == "." else f"📁 {folder2}"
            if folder2.endswith("-d"):
                actual_name2 = folder2[:-2]
                display_name2 = f"📥 {actual_name2}" if actual_name2 != "." else "📥 Root-Downloads"
                
            if len(display_name2) > 30:
                display_name2 = display_name2[:27] + "..."
                
            row.append(InlineKeyboardButton(display_name2, callback_data=f"send_folder:{folder2}"))
        
        keyboard.append(row)
    
    # Add navigation buttons
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"folders_page:{page-1}"))
    if end_idx < len(folders):
        nav_row.append(InlineKeyboardButton("➡️ Next", callback_data=f"folders_page:{page+1}"))
    
    if nav_row:
        keyboard.append(nav_row)
    
    # Add back to menu button
    keyboard.append([InlineKeyboardButton("🔙 Back to Menu", callback_data="cmd_menu")])
    
    return InlineKeyboardMarkup(keyboard)

async def send_file_to_telegram(context: ContextTypes.DEFAULT_TYPE, 
                               file_path: Path, 
                               chat_id: int) -> Tuple[bool, str]:
    """Send a file to Telegram chat as document (uncompressed)"""
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
            file_size_str = format_file_size(file_size)
            
            # Always send as document to preserve original quality (no compression)
            # Create caption with file info and appropriate emoji based on file type
            if mime_type and mime_type.startswith('image/'):
                caption = f"🖼️ <code>{filename}</code> ({file_size_str}) • Original quality"
            elif mime_type and mime_type.startswith('video/'):
                caption = f"🎬 <code>{filename}</code> ({file_size_str}) • Original quality"
            elif mime_type and mime_type.startswith('audio/'):
                caption = f"🎵 <code>{filename}</code> ({file_size_str}) • Original quality"
            else:
                caption = f"📄 <code>{filename}</code> ({file_size_str})"
            
            await context.bot.send_document(
                chat_id=chat_id,
                document=f,
                caption=caption,
                parse_mode=ParseMode.HTML
            )
        
        return True, f"✅ Successfully sent: <code>{filename}</code> ({file_size_str}) • Uncompressed"
        
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
                await msg.reply_text(
                    f"✅ Download complete. Saved as: <code>{final_name}</code>\n\n"
                    f"📁 Path: <code>{dest}</code>",
                    parse_mode=ParseMode.HTML
                )
                log.info("Downloaded %s -> %s", file_id, dest)
            else:
                await msg.reply_text(f"❌ Download failed: {err}", parse_mode=ParseMode.HTML)
                log.error("Download failed for %s: %s", file_id, err)
        except Exception as e:
            await msg.reply_text(f"❌ Download failed: {e}", parse_mode=ParseMode.HTML)
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
    
    keyboard = create_main_menu_keyboard()
    
    await update.message.reply_text(
        "🤖 <b>Achiko Bot</b>\n\n"
        "👋 Hey there! I can help you transfer files both ways:\n\n"
        
        "📥 <b>DOWNLOAD FROM TELEGRAM:</b>\n"
        "   • Just send me any media (photos, videos, documents, audio...)\n"
        f"   • I'll save them to: <code>{DOWNLOAD_DIR}</code>\n\n"
        
        "📤 <b>SEND TO TELEGRAM:</b>\n"
        f"   • Use the menu buttons below or commands ({upload_count} files ready)\n"
        f"   • Files are loaded from: <code>{UPLOAD_DIR}</code>\n\n"
        
        "🎯 <b>QUICK ACCESS:</b>\n"
        "   • Use the buttons below for easy navigation\n"
        "   • Or type <code>/</code> to see all available commands\n"
        "   • Type <code>/menu</code> anytime to return to this menu\n\n"
        
        "🔒 <b>Security:</b> Only you can use this bot!\n"
        "📁 <b>File limit:</b> Max 50MB per file (Telegram limit)",
        
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML
    )

async def handle_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /menu command - show interactive main menu"""
    if not user_is_allowed(update) or not is_private_chat(update):
        return
    
    try:
        upload_files = get_upload_files(UPLOAD_DIR)
        upload_count = len(upload_files)
        
        upload_folders = get_upload_folders(UPLOAD_DIR)
        folder_count = len(upload_folders)
    except:
        upload_count = "?"
        folder_count = "?"
    
    keyboard = create_main_menu_keyboard()
    
    await update.message.reply_text(
        "📋 <b>Main Menu</b>\n\n"
        f"📊 <b>Statistics:</b>\n"
        f"   • {upload_count} files available\n"
        f"   • {folder_count} folders available\n\n"
        
        "🎯 <b>Quick Actions:</b>\n"
        "   • Click any button below for instant action\n"
        "   • Or use commands: type <code>/</code> for command list\n\n"
        
        "📥 <b>Send me media</b> to download it automatically!",
        
        reply_markup=keyboard,
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
    
    # Separate upload and download folders
    upload_folders = [f for f in folders if not f.endswith("-d")]
    download_folders = [f for f in folders if f.endswith("-d")]
    
    message = "📁 <b>Available folders:</b>\n\n"
    
    # Show upload folders
    if upload_folders:
        message += "📤 <b>Upload folders:</b>\n"
        for folder in upload_folders[:10]:  # Limit to avoid too long message
            if folder == ".":
                files_count = len(get_files_in_folder(UPLOAD_DIR, "."))
                message += f"📂 <code>. (root)</code> ({files_count} files)\n"
            else:
                files_count = len(get_files_in_folder(UPLOAD_DIR, folder))
                message += f"📂 <code>{folder}</code> ({files_count} files)\n"
        message += "\n"
    
    # Show download folders  
    if download_folders:
        message += "📥 <b>Download folders:</b>\n"
        for folder in download_folders[:10]:  # Limit to avoid too long message
            if folder == ".-d":
                files_count = len(get_files_in_folder(UPLOAD_DIR, ".-d"))
                message += f"📂 <code>.-d (root-download)</code> ({files_count} files)\n"
            else:
                files_count = len(get_files_in_folder(UPLOAD_DIR, folder))
                folder_display = folder[:-2]  # Remove -d for display
                message += f"📂 <code>{folder}</code> ({folder_display}-download, {files_count} files)\n"
    
    total_folders = len(upload_folders) + len(download_folders)
    if total_folders > 20:
        message += f"\n<i>... and {total_folders - 20} more folders</i>"
    
    message += "\n💡 Use <code>/send foldername</code> to send upload folder files"
    message += "\n💡 Use <code>/send foldername-d</code> to send download folder files"
    message += "\n💡 Use <code>/send .</code> for root upload, <code>/send .-d</code> for root download"
    
    await update.message.reply_text(message, parse_mode=ParseMode.HTML)

async def handle_listfolder_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /listfolder command - show files in a specific folder"""
    if not user_is_allowed(update) or not is_private_chat(update):
        return
    
    if not context.args:
        await update.message.reply_text(
            "❗ Please specify a folder name.\n\n"
            "Usage: <code>/listfolder foldername</code>\n"
            "Examples:\n"
            "   • <code>/listfolder photos</code> - List files in photos folder\n"
            "   • <code>/listfolder photos-d</code> - List files in photos download folder\n"
            "   • <code>/listfolder .</code> - List files in root upload folder\n"
            "   • <code>/listfolder .-d</code> - List files in root download folder\n\n"
            "Use <code>/folders</code> to see available folders.",
            parse_mode=ParseMode.HTML
        )
        return
    
    folder_name = " ".join(context.args)  # Handle folder names with spaces
    
    # Check if folder exists
    folder_path = find_upload_folder(UPLOAD_DIR, folder_name)
    
    if folder_path is None:
        await update.message.reply_text(
            f"❌ Folder not found: <code>{folder_name}</code>\n\n"
            f"Use <code>/folders</code> to see available folders.",
            parse_mode=ParseMode.HTML
        )
        return
    
    # Get files in the folder
    files_in_folder = get_files_in_folder(UPLOAD_DIR, folder_path)
    
    if not files_in_folder:
        folder_type = "download" if folder_path.endswith("-d") else "upload"
        folder_display = folder_path[:-2] if folder_path.endswith("-d") else folder_path
        folder_display = "root" if folder_display == "." else folder_display
        
        await update.message.reply_text(
            f"📁 Folder <code>{folder_name}</code> is empty.\n"
            f"📂 Type: {folder_type}\n"
            f"📍 Path: {folder_display}",
            parse_mode=ParseMode.HTML
        )
        return
    
    # Calculate total size
    total_size = sum(f.stat().st_size for f in files_in_folder)
    total_size_str = format_file_size(total_size)
    
    # Determine folder type and display name
    is_download = folder_path.endswith("-d")
    folder_type = "📥 Download" if is_download else "📤 Upload"
    folder_display = folder_path[:-2] if is_download else folder_path
    folder_display = "root" if folder_display == "." else folder_display
    
    # Format file list
    file_list = []
    for file_path in files_in_folder[:30]:  # Limit to 30 files to avoid too long message
        size = file_path.stat().st_size
        size_str = format_file_size(size)
        filename = file_path.name
        file_list.append(f"📄 <code>{filename}</code> ({size_str})")
    
    # Build message
    message = f"{folder_type} folder: <b>{folder_display}</b>\n"
    message += f"📊 <b>Total:</b> {len(files_in_folder)} files ({total_size_str})\n\n"
    message += "📁 <b>Files in this folder:</b>\n\n" + "\n".join(file_list)
    
    if len(files_in_folder) > 30:
        message += f"\n\n<i>... and {len(files_in_folder) - 30} more files</i>"
    
    message += f"\n\n💡 Use <code>/send {folder_name}</code> to send all files in this folder"
    message += f"\n💡 Use <code>/send filename</code> to send individual files"
    
    await update.message.reply_text(message, parse_mode=ParseMode.HTML)

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button clicks"""
    if not user_is_allowed(update) or not is_private_chat(update):
        return
    
    query = update.callback_query
    if not query or not query.data:
        return
        
    await query.answer()  # Acknowledge the callback query
    
    data = query.data
    
    try:
        # Main menu commands
        if data == "cmd_menu":
            # Show main menu
            try:
                upload_files = get_upload_files(UPLOAD_DIR)
                upload_count = len(upload_files)
                upload_folders = get_upload_folders(UPLOAD_DIR)
                folder_count = len(upload_folders)
            except:
                upload_count = "?"
                folder_count = "?"
            
            keyboard = create_main_menu_keyboard()
            await query.edit_message_text(
                "📋 <b>Main Menu</b>\n\n"
                f"📊 <b>Statistics:</b>\n"
                f"   • {upload_count} files available\n"
                f"   • {folder_count} folders available\n\n"
                
                "🎯 <b>Quick Actions:</b>\n"
                "   • Click any button below for instant action\n"
                "   • Or use commands: type <code>/</code> for command list\n\n"
                
                "📥 <b>Send me media</b> to download it automatically!",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
            
        elif data == "cmd_list":
            # Show files list with inline keyboard
            files = get_upload_files(UPLOAD_DIR)
            
            if not files:
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Menu", callback_data="cmd_menu")
                ]])
                await query.edit_message_text(
                    "📁 <b>No files available</b>\n\n"
                    "Send me some files first or check your upload directory!",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )
                return
            
            total_size = sum(size for _, size in files)
            total_size_str = format_file_size(total_size)
            
            keyboard = create_files_menu_keyboard(files)
            await query.edit_message_text(
                f"📄 <b>Available Files</b>\n\n"
                f"📊 <b>Total:</b> {len(files)} files ({total_size_str})\n\n"
                "🎯 <b>Click on a file to send it instantly:</b>",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
            
        elif data == "cmd_folders":
            # Show folders list with inline keyboard
            folders = get_upload_folders(UPLOAD_DIR)
            
            if not folders:
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Menu", callback_data="cmd_menu")
                ]])
                await query.edit_message_text(
                    "📁 <b>No folders available</b>\n\n"
                    "Create some folders in your upload/download directories first!",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )
                return
            
            keyboard = create_folders_menu_keyboard(folders)
            await query.edit_message_text(
                f"📁 <b>Available Folders</b>\n\n"
                f"📊 <b>Total:</b> {len(folders)} folders\n\n"
                "🎯 <b>Click on a folder to send all its files:</b>",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
            
        elif data == "cmd_send_file":
            # Show file selection menu
            files = get_upload_files(UPLOAD_DIR)
            
            if not files:
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Menu", callback_data="cmd_menu")
                ]])
                await query.edit_message_text(
                    "📁 <b>No files available</b>\n\n"
                    "Send me some files first or check your upload directory!\n\n"
                    "💡 You can also use: <code>/send filename.ext</code>",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )
                return
                
            keyboard = create_files_menu_keyboard(files)
            await query.edit_message_text(
                f"📤 <b>Send File</b>\n\n"
                f"📊 <b>Available:</b> {len(files)} files\n\n"
                "🎯 <b>Select a file to send:</b>",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
            
        elif data == "cmd_send_folder":
            # Show folder selection menu
            folders = get_upload_folders(UPLOAD_DIR)
            
            if not folders:
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Menu", callback_data="cmd_menu")
                ]])
                await query.edit_message_text(
                    "📁 <b>No folders available</b>\n\n"
                    "Create some folders in your directories first!\n\n"
                    "💡 You can also use: <code>/send foldername</code>",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )
                return
                
            keyboard = create_folders_menu_keyboard(folders)
            await query.edit_message_text(
                f"📂 <b>Send Folder</b>\n\n"
                f"📊 <b>Available:</b> {len(folders)} folders\n\n"
                "🎯 <b>Select a folder to send all its files:</b>",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
            
        elif data == "cmd_help":
            # Show help information
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back to Menu", callback_data="cmd_menu")
            ]])
            await query.edit_message_text(
                "ℹ️ <b>Help & Instructions</b>\n\n"
                
                "📥 <b>Download from Telegram:</b>\n"
                "   • Send any media to me\n"
                "   • I'll save it automatically\n\n"
                
                "📤 <b>Send to Telegram:</b>\n"
                "   • Use menu buttons for easy access\n"
                "   • Or use commands like <code>/send filename</code>\n\n"
                
                "🎯 <b>Available Commands:</b>\n"
                "   • <code>/menu</code> - Show this interactive menu\n"
                "   • <code>/list</code> - List all files\n"
                "   • <code>/folders</code> - List all folders\n"
                "   • <code>/send &lt;name&gt;</code> - Send file or folder\n"
                "   • <code>/listfolder &lt;name&gt;</code> - List folder contents\n\n"
                
                "💡 <b>Tips:</b>\n"
                "   • Type <code>/</code> to see all commands\n"
                "   • Folders ending with <code>-d</code> are from downloads\n"
                "   • Max file size: 50MB (Telegram limit)",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
            
        # Handle pagination for files
        elif data.startswith("files_page:"):
            page = int(data.split(":")[1])
            files = get_upload_files(UPLOAD_DIR)
            keyboard = create_files_menu_keyboard(files, page)
            total_size = sum(size for _, size in files)
            total_size_str = format_file_size(total_size)
            
            await query.edit_message_text(
                f"📄 <b>Available Files</b> (Page {page + 1})\n\n"
                f"📊 <b>Total:</b> {len(files)} files ({total_size_str})\n\n"
                "🎯 <b>Click on a file to send it instantly:</b>",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
            
        # Handle pagination for folders
        elif data.startswith("folders_page:"):
            page = int(data.split(":")[1])
            folders = get_upload_folders(UPLOAD_DIR)
            keyboard = create_folders_menu_keyboard(folders, page)
            
            await query.edit_message_text(
                f"📁 <b>Available Folders</b> (Page {page + 1})\n\n"
                f"📊 <b>Total:</b> {len(folders)} folders\n\n"
                "🎯 <b>Click on a folder to send all its files:</b>",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
            
        # Handle file sending
        elif data.startswith("send_file:"):
            filename = data[10:]  # Remove "send_file:" prefix
            
            # Find and send the file
            file_path = find_upload_file(UPLOAD_DIR, filename)
            
            if not file_path:
                await query.edit_message_text(
                    f"❌ <b>File not found:</b> <code>{filename}</code>\n\n"
                    "The file may have been moved or deleted.",
                    parse_mode=ParseMode.HTML
                )
                return
            
            # Update message to show sending status
            await query.edit_message_text(
                f"📤 <b>Sending file...</b>\n\n"
                f"📄 File: <code>{filename}</code>\n"
                f"📏 Size: {format_file_size(file_path.stat().st_size)}",
                parse_mode=ParseMode.HTML
            )
            
            # Send the file
            success, message = await send_file_to_telegram(context, file_path, query.message.chat.id)
            
            # Create back button
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("📄 Back to Files", callback_data="cmd_list"),
                InlineKeyboardButton("🔙 Main Menu", callback_data="cmd_menu")
            ]])
            
            if success:
                await query.edit_message_text(
                    f"✅ <b>File sent successfully!</b>\n\n"
                    f"📄 {filename}\n\n"
                    f"{message}",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )
            else:
                await query.edit_message_text(
                    f"❌ <b>Failed to send file</b>\n\n"
                    f"📄 {filename}\n\n"
                    f"Error: {message}",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )
                
        # Handle folder sending
        elif data.startswith("send_folder:"):
            folder_name = data[12:]  # Remove "send_folder:" prefix
            
            # Find folder and get files
            folder_path = find_upload_folder(UPLOAD_DIR, folder_name)
            
            if not folder_path:
                await query.edit_message_text(
                    f"❌ <b>Folder not found:</b> <code>{folder_name}</code>\n\n"
                    "The folder may have been moved or deleted.",
                    parse_mode=ParseMode.HTML
                )
                return
            
            files_in_folder = get_files_in_folder(UPLOAD_DIR, folder_path)
            
            if not files_in_folder:
                keyboard = InlineKeyboardMarkup([[
                    InlineKeyboardButton("📁 Back to Folders", callback_data="cmd_folders"),
                    InlineKeyboardButton("🔙 Main Menu", callback_data="cmd_menu")
                ]])
                await query.edit_message_text(
                    f"📁 <b>Folder is empty:</b> <code>{folder_name}</code>\n\n"
                    "No files to send.",
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML
                )
                return
            
            # Update message to show sending status
            folder_display = "root" if folder_path == "." else folder_path
            await query.edit_message_text(
                f"📂 <b>Sending folder...</b>\n\n"
                f"📁 Folder: <code>{folder_display}</code>\n"
                f"📊 Files: {len(files_in_folder)} files\n\n"
                "⏳ Please wait...",
                parse_mode=ParseMode.HTML
            )
            
            # Send all files in the folder
            sent_count = 0
            failed_count = 0
            failed_files = []
            
            for file_path in files_in_folder:
                success, message = await send_file_to_telegram(context, file_path, query.message.chat.id)
                if success:
                    sent_count += 1
                else:
                    failed_count += 1
                    failed_files.append(file_path.name)
                    log.error("Failed to send %s: %s", file_path.name, message)
                
                # Small delay to avoid rate limits
                await asyncio.sleep(0.5)
            
            # Create back button
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("📁 Back to Folders", callback_data="cmd_folders"),
                InlineKeyboardButton("🔙 Main Menu", callback_data="cmd_menu")
            ]])
            
            # Send summary
            summary = f"📂 <b>Folder send complete!</b>\n\n"
            summary += f"📁 Folder: <code>{folder_display}</code>\n"
            summary += f"✅ Successfully sent: {sent_count} files\n"
            if failed_count > 0:
                summary += f"❌ Failed: {failed_count} files\n"
                if failed_files:
                    summary += f"Failed files: {', '.join(failed_files[:3])}"
                    if len(failed_files) > 3:
                        summary += f" and {len(failed_files) - 3} more..."
            
            await query.edit_message_text(
                summary,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
            
    except Exception as e:
        log.error("Error handling callback query %s: %s", data, str(e))
        # Try to show an error message
        try:
            keyboard = InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back to Menu", callback_data="cmd_menu")
            ]])
            await query.edit_message_text(
                f"❌ <b>Error occurred</b>\n\n"
                f"Something went wrong while processing your request.\n\n"
                f"Error: {str(e)}",
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML
            )
        except:
            pass  # If we can't even show the error message, just log it

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Error handling update: %s", context.error)

def build_app() -> Application:
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )
    
    # Add callback query handler for inline keyboards (must be added first)
    app.add_handler(CallbackQueryHandler(
        handle_callback_query,
        filters=filters.ChatType.PRIVATE & filters.User(ALLOWED_TELEGRAM_USER_ID)
    ))
    
    app.add_handler(MessageHandler(
        filters.ChatType.PRIVATE & filters.User(ALLOWED_TELEGRAM_USER_ID) & MEDIA_FILTER,
        handle_media
    ))
    
    # Register command handlers for the allowed user in private chat
    app.add_handler(CommandHandler(
        "start",
        start,
        filters=filters.ChatType.PRIVATE & filters.User(ALLOWED_TELEGRAM_USER_ID)
    ))
    
    app.add_handler(CommandHandler(
        "menu", 
        handle_menu_command,
        filters=filters.ChatType.PRIVATE & filters.User(ALLOWED_TELEGRAM_USER_ID)
    ))
    
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
    
    app.add_handler(CommandHandler(
        "listfolder",
        handle_listfolder_command,
        filters=filters.ChatType.PRIVATE & filters.User(ALLOWED_TELEGRAM_USER_ID)
    ))
    
    app.add_error_handler(error_handler)
    return app

async def main_async() -> None:
    app = build_app()
    
    # Set up bot commands first
    await setup_bot_commands(app)
    
    log.info("Starting bot with long polling. Allowed user id: %s. Download dir: %s",
             ALLOWED_TELEGRAM_USER_ID, DOWNLOAD_DIR)
    
    # Long polling; restrict updates to messages and callback queries, drop pending to avoid backlog
    await app.run_polling(
        allowed_updates=[constants.UpdateType.MESSAGE, constants.UpdateType.CALLBACK_QUERY],
        drop_pending_updates=True,
        poll_interval=1.5,
        timeout=30,
        stop_signals=(signal.SIGINT, signal.SIGTERM),
    )

def main() -> None:
    # Run the async main function
    asyncio.run(main_async())

if __name__ == "__main__":
    main()
