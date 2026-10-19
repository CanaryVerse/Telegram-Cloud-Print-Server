# telegram_listener.py
# Receive files/text from a Telegram Bot and save them to the ./printing folder
#
# Install the library:
#   pip install python-telegram-bot
#
# Run:
#   python telegram_listener.py
#
# How to use:
#   1. Create a bot with BotFather.
#   2. Copy token bot.
#   3. Paste the bot token into BOT_TOKEN below.
#   4. Edit PRINT_DIR if needed.
#   5. Run this file.
#
# Security note:
#   If ALLOWED_CHAT_IDS = [], anyone who knows the bot can send files.
#   Recommended:
#     - Run the bot for the first time.
#     - Send /start to the bot.
#     - The bot will return the Chat ID.
#     - Copy that Chat ID into ALLOWED_CHAT_IDS = [123456789]

import asyncio
import logging
import mimetypes
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)


# =============================================================================
# CONFIG - ONLY EDIT THIS SECTION
# =============================================================================

# Token from BotFather.
from bot_secrets import BOT_TOKEN, ALLOWED_CHAT_IDS


# Folder for saving files received from Telegram.
# To save into the printing folder next to this file:
PRINT_DIR = Path(__file__).resolve().parent / "printing"

# To force a specific project folder, uncomment the line below and edit the path:
# PRINT_DIR = Path(r"C:\chrome-auto-print\printing")

# Only allow these chat_id values to send files.
# Use [] for no restriction; anyone who knows the bot can send files.
# Example: ALLOWED_CHAT_IDS = [123456789, 987654321]


# HTML download limit for URLs, to avoid downloading overly large files by mistake.
MAX_HTML_DOWNLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

# Timeout when fetching HTML URLs.
HTML_FETCH_TIMEOUT_SEC = 20

# If a file/photo has a caption, save the caption as a separate .txt file.
SAVE_CAPTION = True

# Send a confirmation message after saving.
REPLY_AFTER_SAVE = True

# If the text is an HTML URL, download it as .html.
# If False, links are also saved only as .txt.
DOWNLOAD_HTML_LINK = True


# =============================================================================
# LOGGING
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("telegram_listener")


# =============================================================================
# HELPERS
# =============================================================================

URL_RE = re.compile(r"^https?://[^\s<>\"']+$", re.IGNORECASE)


def ensure_print_dir() -> None:
    PRINT_DIR.mkdir(parents=True, exist_ok=True)


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]

def internet_shortcut_content(url: str) -> str:
    return (
        "[InternetShortcut]\r\n"
        f"URL={url.strip()}\r\n"
    )
def safe_filename(name: str, fallback: str = "telegram_file") -> str:
    """
    Clean the file name for Windows/Linux.
    """
    if not name:
        name = fallback

    name = unicodedata.normalize("NFKC", name)
    name = name.replace("\x00", "")
    name = re.sub(r'[<>:"/\\|?*\r\n\t]+', "_", name)
    name = re.sub(r"\s+", " ", name).strip()
    name = name.strip(" .")

    reserved = {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
    }

    stem = Path(name).stem.upper()
    if not name or stem in reserved:
        name = fallback

    if len(name) > 180:
        p = Path(name)
        suffix = p.suffix[:20]
        stem = p.stem[: 180 - len(suffix)]
        name = stem + suffix

    return name


def unique_path(filename: str) -> Path:
    """
    Create a path without overwriting existing files.
    """
    ensure_print_dir()

    filename = safe_filename(filename)
    path = PRINT_DIR / filename

    if not path.exists():
        return path

    p = Path(filename)
    for i in range(1, 10000):
        candidate = PRINT_DIR / f"{p.stem}_{i}{p.suffix}"
        if not candidate.exists():
            return candidate

    raise RuntimeError("Could not create a unique file name in the printing folder.")


def atomic_write_bytes(final_path: Path, data: bytes) -> None:
    """
    Safely write the file: write to .part first, then rename.
    Prevent the auto-printer from picking up a file before it is fully written.
    """
    ensure_print_dir()
    tmp_path = final_path.with_name(final_path.name + ".part")

    if tmp_path.exists():
        tmp_path.unlink()

    tmp_path.write_bytes(data)
    tmp_path.replace(final_path)


def atomic_write_text(final_path: Path, text: str) -> None:
    atomic_write_bytes(final_path, text.encode("utf-8", errors="replace"))


async def atomic_download_telegram_file(tg_file, final_path: Path) -> None:
    ensure_print_dir()
    tmp_path = final_path.with_name(final_path.name + ".part")

    if tmp_path.exists():
        tmp_path.unlink()

    await tg_file.download_to_drive(custom_path=str(tmp_path))
    tmp_path.replace(final_path)


def is_allowed(update: Update) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True

    chat = update.effective_chat
    if chat is None:
        return False

    return chat.id in ALLOWED_CHAT_IDS


def get_sender_prefix(update: Update) -> str:
    chat_id = update.effective_chat.id if update.effective_chat else "unknown"
    user_id = update.effective_user.id if update.effective_user else "unknown"
    return f"chat{chat_id}_user{user_id}"


def text_is_single_url(text: str) -> bool:
    return bool(URL_RE.match(text.strip()))


def filename_from_url(url: str) -> str:
    parsed = urlparse(url)
    host = safe_filename(parsed.netloc or "web")
    path_name = safe_filename(Path(parsed.path).name or "index.html")

    lower = path_name.lower()
    if not lower.endswith((".html", ".htm")):
        path_name = Path(path_name).stem + ".html"

    return f"{timestamp()}_{host}_{path_name}"


def fetch_html_url_sync(url: str) -> tuple[bytes, str]:
    """
    Fetch the URL if the content is HTML.
    Returns: (html_bytes, final_url)
    """
    req = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 telegram-listener/1.0",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        method="GET",
    )

    with urlopen(req, timeout=HTML_FETCH_TIMEOUT_SEC) as resp:
        content_type = (resp.headers.get("Content-Type") or "").lower()
        final_url = resp.geturl()

        data = resp.read(MAX_HTML_DOWNLOAD_BYTES + 1)
        if len(data) > MAX_HTML_DOWNLOAD_BYTES:
            raise ValueError(f"HTML is too large, exceeds {MAX_HTML_DOWNLOAD_BYTES} bytes.")

        parsed_path = urlparse(final_url).path.lower()
        suffix = Path(parsed_path).suffix
        looks_html_by_url = parsed_path.endswith((".html", ".htm", "/")) or not suffix

        if (
            "text/html" not in content_type
            and "application/xhtml+xml" not in content_type
            and not looks_html_by_url
        ):
            raise ValueError(f"URL does not look like HTML. Content-Type: {content_type or 'unknown'}")

        return data, final_url


async def fetch_html_url(url: str) -> tuple[bytes, str]:
    return await asyncio.to_thread(fetch_html_url_sync, url)


async def save_caption_if_any(update: Update, base_final_path: Path) -> Optional[Path]:
    msg = update.effective_message
    if not SAVE_CAPTION or not msg or not msg.caption:
        return None

    caption_path = unique_path(f"{base_final_path.stem}_caption.txt")
    atomic_write_text(caption_path, msg.caption)
    return caption_path


async def reply_if_enabled(update: Update, text: str) -> None:
    if REPLY_AFTER_SAVE and update.effective_message:
        await update.effective_message.reply_text(text)

# =============================================================================
# HANDLERS
# =============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return

    ensure_print_dir()
    chat_id = update.effective_chat.id if update.effective_chat else "unknown"

    await update.effective_message.reply_text(
        "OK. Send a photo, PDF, file, or text here, and the bot will save it to the folder:\n"
        f"{PRINT_DIR}\n\n"
        f"Your Chat ID: {chat_id}"
    )


async def where_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        return

    chat_id = update.effective_chat.id if update.effective_chat else "unknown"
    await update.effective_message.reply_text(
        f"PRINT_DIR = {PRINT_DIR}\n"
        f"Chat ID = {chat_id}"
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.effective_message.reply_text("This chat is not allowed to use the bot.")
        return

    msg = update.effective_message
    photo = msg.photo[-1]
    tg_file = await photo.get_file()

    filename = f"{timestamp()}_{get_sender_prefix(update)}_{photo.file_unique_id}.jpg"
    final_path = unique_path(filename)

    await atomic_download_telegram_file(tg_file, final_path)
    caption_path = await save_caption_if_any(update, final_path)

    log.info("Saved photo: %s", final_path)

    reply = f"Saved photo:\n{final_path.name}"
    if caption_path:
        reply += f"\nSaved caption:\n{caption_path.name}"

    await reply_if_enabled(update, reply)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.effective_message.reply_text("This chat is not allowed to use the bot.")
        return

    msg = update.effective_message
    doc = msg.document

    original_name = doc.file_name or ""
    if not original_name:
        guessed_ext = mimetypes.guess_extension(doc.mime_type or "") or ".bin"
        original_name = f"{doc.file_unique_id}{guessed_ext}"

    filename = f"{timestamp()}_{get_sender_prefix(update)}_{safe_filename(original_name)}"
    final_path = unique_path(filename)

    tg_file = await doc.get_file()
    await atomic_download_telegram_file(tg_file, final_path)
    caption_path = await save_caption_if_any(update, final_path)

    log.info("Saved document: %s", final_path)

    reply = f"Saved file:\n{final_path.name}"
    if caption_path:
        reply += f"\nSaved caption:\n{caption_path.name}"

    await reply_if_enabled(update, reply)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_allowed(update):
        await update.effective_message.reply_text("This chat is not allowed to use the bot.")
        return

    msg = update.effective_message
    text = msg.text or ""

    # If the text is exactly one HTTP/HTTPS URL, save it as a .url file
    if text_is_single_url(text):
        url = text.strip()

        filename = f"{timestamp()}_{get_sender_prefix(update)}_link.url"
        final_path = unique_path(filename)

        atomic_write_text(final_path, internet_shortcut_content(url))

        log.info("Saved URL shortcut: %s -> %s", url, final_path)
        await reply_if_enabled(update, f"Saved link:\n{final_path.name}")
        return

    # Normal text or an HTML URL that cannot be downloaded: save as .txt
    filename = f"{timestamp()}_{get_sender_prefix(update)}_message.txt"
    final_path = unique_path(filename)

    atomic_write_text(final_path, text)
    log.info("Saved text: %s", final_path)

    await reply_if_enabled(update, f"Saved text:\n{final_path.name}")


async def handle_other_attachment(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Also catch video/audio/voice/sticker/... if needed.
    Telegram has many media types that are not document/photo.
    """
    if not is_allowed(update):
        await update.effective_message.reply_text("This chat is not allowed to use the bot.")
        return

    msg = update.effective_message
    attachment = msg.effective_attachment

    if not attachment:
        await reply_if_enabled(update, "This message has no savable content.")
        return

    if isinstance(attachment, list):
        await reply_if_enabled(update, "This attachment type already has its own handler or is not supported.")
        return

    if not hasattr(attachment, "get_file"):
        await reply_if_enabled(update, "The bot cannot download this attachment type yet.")
        return

    tg_file = await attachment.get_file()

    file_unique_id = getattr(attachment, "file_unique_id", "unknown")
    mime_type = getattr(attachment, "mime_type", "") or ""
    ext = mimetypes.guess_extension(mime_type) or ".bin"

    filename = f"{timestamp()}_{get_sender_prefix(update)}_{file_unique_id}{ext}"
    final_path = unique_path(filename)

    await atomic_download_telegram_file(tg_file, final_path)
    caption_path = await save_caption_if_any(update, final_path)

    log.info("Saved attachment: %s", final_path)

    reply = f"Saved attachment:\n{final_path.name}"
    if caption_path:
        reply += f"\nSaved caption:\n{caption_path.name}"

    await reply_if_enabled(update, reply)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("Telegram handler error: %s", context.error)


def validate_config() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise SystemExit(
            "BOT_TOKEN is missing.\n"
            "Open telegram_listener.py and find the line:\n"
            '  BOT_TOKEN = "PASTE_YOUR_BOT_TOKEN_HERE"\n'
            "Then replace it with the real token from BotFather."
        )

    if not isinstance(ALLOWED_CHAT_IDS, list):
        raise SystemExit("ALLOWED_CHAT_IDS must be a list, for example: [] or [123456789].")

    for chat_id in ALLOWED_CHAT_IDS:
        if not isinstance(chat_id, int):
            raise SystemExit("Each item in ALLOWED_CHAT_IDS must be an int, for example: [123456789].")


def main(background=False) -> None:
    validate_config()
    ensure_print_dir()

    log.info("PRINT_DIR = %s", PRINT_DIR)

    if not ALLOWED_CHAT_IDS:
        log.warning("ALLOWED_CHAT_IDS is empty. Anyone who knows the bot token/bot username can send files.")
    else:
        log.info("Allowed chat ids: %s", ALLOWED_CHAT_IDS)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("where", where_command))

    # Important order:
    # - PHOTO catches images sent as regular photos.
    # - Document.ALL catches PDFs/files/images sent as files.
    # - TEXT catches text/links.
    # - ATTACHMENT catches the remaining media types.
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.ATTACHMENT, handle_other_attachment))

    app.add_error_handler(error_handler)

    log.info("Telegram listener started. Press Ctrl+C to stop.")
    # Signal handlers only work on the main thread. The print server runs
    # this polling loop in a daemon thread with its own asyncio event loop.
    polling_options = {"stop_signals": None} if background else {}
    app.run_polling(allowed_updates=Update.ALL_TYPES, **polling_options)


if __name__ == "__main__":
    main()
