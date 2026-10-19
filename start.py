import asyncio
import json
import os
import re
import shutil
import sys
import threading
import uuid
import time
import traceback
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue

from print_config_loader import load_print_job_config, get_config_path_for_file
from print_engine import print_file


# =========================
# STARTER CONFIG
# =========================

CURRENT_DIR = Path(__file__).resolve().parent
PRINT_FOLDER = Path(r"C:\chrome-auto-print\printing")
DONE_FOLDER = PRINT_FOLDER / "Done Jobs"
POLL_INTERVAL_SEC = 1

PRINTABLE_EXTS = {
    ".pdf", ".html", ".htm", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".txt", ".url"
}


def find_chrome_exe():
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        str(Path(os.environ.get("LOCALAPPDATA", "")) / r"Google\Chrome\Application\chrome.exe"),
    ]

    for p in candidates:
        if p and Path(p).exists():
            return p

    found = shutil.which("chrome") or shutil.which("chrome.exe")
    if found:
        return found

    # Không raise ở đây để PDF gốc vẫn có thể in thẳng bằng PDFtoPrinter.
    return ""


ENGINE_CONFIG = {
    "chrome_exe": find_chrome_exe(),
    "debug_port": 9222,
    "profile_dir": str(CURRENT_DIR / "dev_profile"),
    "chrome_headless": True,
    "terminate_chrome_after_job": True,
    "rendered_pdf_dir": str(CURRENT_DIR / "_rendered_jobs"),
    "wrapper_html_dir": str(CURRENT_DIR / "_render_wrappers"),
    "pdftoprinter_exe": str(CURRENT_DIR / "tools" / "PDFtoPrinter.exe"),
}


# Toàn bộ default config đặt ở đây.
# File printing/print_settings.config nếu có sẽ override từng phần.
DEFAULT_JOB_CONFIG = {
    "printer_name": "HPRT N41",
    "pdftoprinter_exe": str(CURRENT_DIR / "tools" / "PDFtoPrinter.exe"),
    
    "image_render_wait_sec": 0.3,
    "local_html_render_wait_sec": 0.5,
    "text_render_wait_sec": 0.2,
    # Khổ tem. Nếu paper_size_text là "76mm * 130mm" thì code cũng tự parse ra mm.
    "paper_size_text": "76mm * 130mm",
    "paper_width_mm": 76,
    "paper_height_mm": 130,

    # Với PDFtoPrinter, page range không chắc được hỗ trợ khi in PDF gốc.
    # Với HTML/link/ảnh/text render qua Chrome, print_pages có tác dụng khi tạo PDF tạm.
    "print_pages": "All",
    "copies": 1,

    # True: PDF gốc gửi thẳng PDFtoPrinter, không render lại bằng Chrome.
    "direct_print_pdf": True,
    "render_pdf_files_with_chrome": False,

    # Chrome Page.printToPDF cho link/html/ảnh/text.
    "chrome_start_timeout_sec": 15,
    "page_load_timeout_sec": 25,
    "web_render_wait_sec": 3,
    "after_document_ready_wait_sec": 1,
    "print_background": True,
    "prefer_css_page_size": False,
    "landscape": False,
    "scale": 1.0,
    "margin_top_mm": 0,
    "margin_bottom_mm": 0,
    "margin_left_mm": 0,
    "margin_right_mm": 0,
    "image_fit": "contain",

    # Fit/scale PDF trước khi gửi PDFtoPrinter để tránh crop.
    # pdf_fit_scale < 1.0 giúp chừa an toàn cho máy in nhiệt có vùng chết ở mép.
    "normalize_pdf_before_print": True,
    "pdf_fit_margin_mm": 1.5,
    "pdf_fit_scale": 0.96,
    "pdf_fit_allow_upscale": True,

    # PDFtoPrinter.
    "pdftoprinter_wait_sec": 2,
    "pdftoprinter_timeout_sec": 60,
    "fail_on_pdftoprinter_nonzero": True,

    # Debug file PDF tạm.
    "keep_temp_pdf": True,
    "cleanup_temp_pdf_after_success": False,
}


# =========================
# FILE HELPERS
# =========================


def get_latest_print_file(folder):
    folder = Path(folder)

    if not folder.exists():
        folder.mkdir(parents=True, exist_ok=True)

    files = []
    for p in folder.iterdir():
        if not p.is_file():
            continue
        if p.name.startswith("~$"):
            continue
        if p.name.lower().endswith(".config"):
            continue
        if p.name.lower().endswith(".part"):
            continue
        if p.suffix.lower() not in PRINTABLE_EXTS:
            continue
        files.append(p)

    if not files:
        return None

    return max(files, key=lambda p: p.stat().st_mtime)


def wait_until_file_stable(file_path, stable_checks=3, interval_sec=0.15):
    file_path = Path(file_path)
    last_size = -1
    last_mtime = -1
    stable_count = 0

    while stable_count < stable_checks:
        if not file_path.exists():
            return False

        stat = file_path.stat()
        size = stat.st_size
        mtime = stat.st_mtime

        if size == last_size and mtime == last_mtime:
            stable_count += 1
        else:
            stable_count = 0
            last_size = size
            last_mtime = mtime

        time.sleep(interval_sec)

    return True


def unique_dest_path(dest_dir, src_path):
    dest_dir = Path(dest_dir)
    src_path = Path(src_path)
    dest = dest_dir / src_path.name

    if not dest.exists():
        return dest

    stem = src_path.stem
    suffix = src_path.suffix
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = dest_dir / f"{stem}_{stamp}{suffix}"

    counter = 1
    while candidate.exists():
        candidate = dest_dir / f"{stem}_{stamp}_{counter}{suffix}"
        counter += 1

    return candidate


def move_job_to_done(file_path):
    file_path = Path(file_path)
    DONE_FOLDER.mkdir(parents=True, exist_ok=True)
    dest_file = unique_dest_path(DONE_FOLDER, file_path)
    shutil.move(str(file_path), str(dest_file))

    return {
        "file": str(dest_file),
        "config": None,
    }


# =========================
# TELEGRAM: reliable, TLS-verified status delivery
# =========================

# Save unsent messages outside the printing directory. A network failure or
# application restart must not permanently lose the print result.
TELEGRAM_STATUS_QUEUE = Queue()  # Wake-up signal; files on disk are authoritative.
PENDING_STATUS_DIR = CURRENT_DIR / "telegram_pending_status"


def _save_pending_status(path, payload):
    """Atomically create/update a pending notification on Windows."""
    PENDING_STATUS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def queue_telegram_status(file_path, status, detail=""):
    """Persist the actual print result before trying to contact Telegram."""
    from bot_secrets import ALLOWED_CHAT_IDS

    # Telegram uploads: *_chat<id>_user<id>_filename.ext
    match = re.search(r"(?:^|_)chat(-?\d+)_user-?\d+_", Path(file_path).name)
    if match and (not ALLOWED_CHAT_IDS or int(match.group(1)) in ALLOWED_CHAT_IDS):
        chat_ids = [int(match.group(1))]
    else:
        # Local/Samba jobs are reported to explicitly allowed chats.
        chat_ids = list(ALLOWED_CHAT_IDS)

    if not chat_ids:
        print("[TELEGRAM] No destination chat ID. Configure ALLOWED_CHAT_IDS "
              "for local/LAN print jobs.", flush=True)
        return

    titles = {
        "success": "✅ PDFtoPrinter.exe: THÀNH CÔNG",
        "failed": "❌ PDFtoPrinter.exe: THẤT BẠI",
        "exception": "❌ Tác vụ in gặp lỗi",
    }
    message = f"{titles[status]}\nTệp: {Path(file_path).name}"
    if detail:
        message += "\nChi tiết: " + str(detail)[:1500]

    payload = {
        "chat_ids": list(dict.fromkeys(chat_ids)),
        "message": message,
        "attempts": 0,
        "retry_after": 0,
    }
    path = PENDING_STATUS_DIR / f"{time.time_ns()}_{uuid.uuid4().hex}.json"
    _save_pending_status(path, payload)
    TELEGRAM_STATUS_QUEUE.put(True)
    print("[TELEGRAM] Print result saved for delivery:", path.name, flush=True)


async def _send_status_to_chats(chat_ids, message):
    """Use the same verified HTTPX transport as the working Telegram listener.

    No ssl.CERT_NONE / verify=False override: certificate validation stays ON.
    Return a per-chat error map so successful recipients are not retried.
    """
    from bot_secrets import BOT_TOKEN
    from telegram import Bot

    errors = {}
    async with Bot(token=BOT_TOKEN) as bot:
        for chat_id in chat_ids:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=message,
                    connect_timeout=10,
                    read_timeout=10,
                    write_timeout=10,
                )
                print(f"[TELEGRAM] Sent print result to chat {chat_id}", flush=True)
            except Exception as exc:
                # Never log a bot token even when a library embeds it in an URL.
                safe_error = str(exc).replace(BOT_TOKEN, "<BOT_TOKEN>")
                errors[chat_id] = f"{type(exc).__name__}: {safe_error}"
    return errors


def _deliver_pending_statuses():
    """One delivery pass; failed messages remain on disk for automatic retry."""
    PENDING_STATUS_DIR.mkdir(parents=True, exist_ok=True)
    for path in sorted(PENDING_STATUS_DIR.glob("*.json")):
        payload = None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if time.time() < float(payload.get("retry_after", 0)):
                continue

            chat_ids = payload["chat_ids"]
            if not chat_ids:
                path.unlink(missing_ok=True)
                continue

            errors = asyncio.run(_send_status_to_chats(chat_ids, payload["message"]))
            failed_chat_ids = [chat_id for chat_id in chat_ids if chat_id in errors]
            if not failed_chat_ids:
                path.unlink(missing_ok=True)
                continue

            for chat_id, error in errors.items():
                print(f"[TELEGRAM] Cannot send status to chat {chat_id}: {error}", flush=True)
            payload["chat_ids"] = failed_chat_ids
        except Exception as exc:
            # Keep notification for retry rather than silently losing it.
            error = str(exc)
            try:
                from bot_secrets import BOT_TOKEN
                error = error.replace(BOT_TOKEN, "<BOT_TOKEN>")
            except ImportError:
                pass
            print(f"[TELEGRAM] Status delivery postponed: "
                  f"{type(exc).__name__}: {error}", flush=True)
            if not isinstance(payload, dict):
                # Malformed JSON is not actionable; leave on disk for inspection.
                continue

        payload["attempts"] = int(payload.get("attempts", 0)) + 1
        retry_delay = min(300, 5 * (2 ** min(payload["attempts"] - 1, 6)))
        payload["retry_after"] = time.time() + retry_delay
        _save_pending_status(path, payload)
        print(f"[TELEGRAM] Will retry print status in {retry_delay} seconds.", flush=True)


def send_telegram_notifications():
    """Background thread: retries pending results without blocking printing."""
    while True:
        try:
            try:
                TELEGRAM_STATUS_QUEUE.get(timeout=5)
                TELEGRAM_STATUS_QUEUE.task_done()
            except Empty:
                pass
            _deliver_pending_statuses()
        except Exception:
            print("[TELEGRAM] Status sender error; retrying in 5 seconds.", flush=True)
            traceback.print_exc()
            time.sleep(5)


def start_telegram_listener():
    # Telegram polling uses its own event loop in a daemon thread.
    from telegram_listener import main as telegram_main, validate_config
    validate_config()

    def run_listener():
        while True:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                telegram_main(background=True)
            except Exception:
                print("[TELEGRAM] Listener crashed; restarting in 10 seconds.", flush=True)
                traceback.print_exc()
            finally:
                if not loop.is_closed():
                    loop.close()
                asyncio.set_event_loop(None)
            time.sleep(10)

    threading.Thread(target=run_listener, name="telegram-listener", daemon=True).start()
    threading.Thread(target=send_telegram_notifications,
                     name="telegram-status-sender", daemon=True).start()


# =========================
# MAIN LOOP
# =========================


def main():
    PRINT_FOLDER.mkdir(parents=True, exist_ok=True)
    DONE_FOLDER.mkdir(parents=True, exist_ok=True)

    if "--telegram" in sys.argv[1:]:
        start_telegram_listener()

    # Prevent repeated Telegram alerts while the same failed job is retried
    # (the original retry/move behavior is left unchanged).
    notified = {}

    print("=" * 80)
    print("AUTO PRINT STARTER - PDFtoPrinter BACKEND")
    print("=" * 80)
    print("Project folder:", CURRENT_DIR)
    print("Print folder  :", PRINT_FOLDER)
    print("Done folder   :", DONE_FOLDER)
    print("Config file   :", get_config_path_for_file())
    print("Chrome exe    :", ENGINE_CONFIG.get("chrome_exe") or "NOT FOUND - only direct PDF may work")
    print("PDFtoPrinter  :", ENGINE_CONFIG.get("pdftoprinter_exe"))

    startup_cfg, startup_config_path, startup_config_status = load_print_job_config(
        None,
        DEFAULT_JOB_CONFIG,
    )

    if startup_config_status == "CONFIG LOADED":
        print("Config check  : OK, using printing/print_settings.config")
    else:
        print("Config check  : INVALID / NOT FOUND - using DEFAULT SETTINGS")
        print("Config reason :", startup_config_status)

    print("Poll every    :", POLL_INTERVAL_SEC, "sec")
    print("=" * 80)

    while True:
        latest_file = None
        print_engine_returned = False
        try:
            latest_file = get_latest_print_file(PRINT_FOLDER)

            if not latest_file:
                time.sleep(POLL_INTERVAL_SEC)
                continue

            print()
            print("=" * 80)
            print("Latest file:", latest_file)
            print("Waiting until file is stable...")

            if not wait_until_file_stable(latest_file):
                print("[WARN] File disappeared while waiting:", latest_file)
                time.sleep(POLL_INTERVAL_SEC)
                continue

            job_config, config_path, config_status = load_print_job_config(
                latest_file,
                DEFAULT_JOB_CONFIG,
            )

            print("Config file  :", config_path)
            print("Config status:", config_status)
            print("Job config:")
            print(json.dumps(job_config, indent=2, ensure_ascii=False))
            print("=" * 80)

            result = print_file(
                target_file=latest_file,
                job_config=job_config,
                engine_config=ENGINE_CONFIG,
            )
            print_engine_returned = True

            print("Print result:")
            print(json.dumps(result, indent=2, ensure_ascii=False))

            if "--telegram" in sys.argv[1:]:
                try:
                    # The exit code is decisive, including when the existing
                    # fail_on_pdftoprinter_nonzero config is False.
                    runs = result.get("print_info", {}).get("runs", [])
                    codes = [run.get("returncode") for run in runs]
                    success = bool(result.get("printed")) and bool(codes) and all(
                        code == 0 for code in codes
                    )
                    status = "success" if success else "failed"
                    job_key = (str(latest_file), latest_file.stat().st_mtime_ns)
                    if notified.get(job_key) != status:
                        detail = "" if success else f"Return codes: {codes}"
                        queue_telegram_status(latest_file, status, detail)
                        notified[job_key] = status
                        if len(notified) > 256:
                            notified.pop(next(iter(notified)))
                except Exception as notify_error:
                    print("[TELEGRAM] Status notification error:", notify_error)

            if result.get("printed"):
                moved = move_job_to_done(latest_file)
                print("Moved to Done Jobs:")
                print(json.dumps(moved, indent=2, ensure_ascii=False))
            else:
                print("[INFO] Job was not printed. File will NOT be moved to Done Jobs.")

            time.sleep(POLL_INTERVAL_SEC)

        except KeyboardInterrupt:
            print()
            print("Stopped by user.")
            break

        except Exception as e:
            print()
            print("[ERROR]")
            print(e)
            print("File will NOT be moved to Done Jobs.")
            if (latest_file is not None and not print_engine_returned
                    and "--telegram" in sys.argv[1:]):
                try:
                    job_key = (str(latest_file), latest_file.stat().st_mtime_ns)
                    if notified.get(job_key) != "exception":
                        queue_telegram_status(
                            latest_file, "exception", f"{type(e).__name__}: {e}"
                        )
                        notified[job_key] = "exception"
                        if len(notified) > 256:
                            notified.pop(next(iter(notified)))
                except Exception as notify_error:
                    print("[TELEGRAM] Status notification error:", notify_error)
            time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
