#!/usr/bin/env python3
"""Telegram interface for ReClip. Uses yt-dlp for extraction and Telegram inline keyboards."""
from __future__ import annotations

import html
import io
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import requests
import telebot
import yt_dlp
from requests_toolbelt.multipart.encoder import MultipartEncoder, MultipartEncoderMonitor
from telebot import types
from yt_dlp.utils import DownloadError

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("reclip.telegram")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", str(Path(__file__).parent / "downloads")))
MAX_FILE_MB = int(os.getenv("TELEGRAM_MAX_FILE_MB", "49"))
MAX_CONCURRENT = max(1, int(os.getenv("BOT_MAX_CONCURRENT", "2")))
if not BOT_TOKEN:
    raise SystemExit("Missing TELEGRAM_BOT_TOKEN environment variable.")

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=True, num_threads=8)
sessions: dict[int, dict] = {}
active_users: set[int] = set()
active_lock = threading.Lock()
last_edit: dict[tuple[int, int], float] = {}


def valid_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme in ("http", "https") and bool(parsed.hostname) and not any(
            c.isspace() for c in value
        )
    except Exception:
        return False


def duration_text(seconds) -> str:
    if not seconds:
        return "Unknown"
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def human_size(value) -> str:
    if not value:
        return "Unknown size"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return "Unknown size"


def progress_bar(percent: float, width: int = 12) -> str:
    filled = max(0, min(width, int(percent / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def safe_filename(name: str, suffix: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name).strip(" .")
    name = re.sub(r"\s+", " ", name)[:150].strip()
    return (name or "reclip-download") + suffix


def extract_info(url: str) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 25,
        "retries": 2,
        "extractor_retries": 2,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def quality_options(info: dict) -> list[tuple[str, str]]:
    best_by_height = {}
    for fmt in info.get("formats", []):
        height = fmt.get("height")
        if not height or fmt.get("vcodec") in (None, "none"):
            continue
        # Prefer a video-only format: yt-dlp merges bestaudio and keeps the chosen height.
        key = str(height)
        score = (fmt.get("vcodec") != "none", fmt.get("tbr") or 0,
                 fmt.get("filesize") or fmt.get("filesize_approx") or 0)
        if key not in best_by_height or score > best_by_height[key][0]:
            best_by_height[key] = (score, fmt)
    items = [(f["format_id"], f"{height}p") for height, (_, f) in best_by_height.items()]
    items.sort(key=lambda item: int(item[1][:-1]), reverse=True)
    # Avoid callback-data overflow and overly long keyboards.
    return items[:12]


def make_format_keyboard(user_id: int) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        types.InlineKeyboardButton("🎬 MP4 Video", callback_data=f"mode:video:{user_id}"),
        types.InlineKeyboardButton("🎵 MP3 Audio", callback_data=f"mode:audio:{user_id}"),
    )
    keyboard.add(types.InlineKeyboardButton("🔄 Fetch another link", callback_data="new"))
    return keyboard


def make_quality_keyboard(user_id: int, options: list[tuple[str, str]]) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup(row_width=3)
    buttons = [
        types.InlineKeyboardButton(label, callback_data=f"q:{user_id}:{format_id}")
        for format_id, label in options
    ]
    keyboard.add(*buttons)
    keyboard.add(types.InlineKeyboardButton("⭐ Best available", callback_data=f"q:{user_id}:best"))
    keyboard.add(types.InlineKeyboardButton("⬅️ Format", callback_data=f"back:{user_id}"))
    return keyboard


def edit_status(chat_id: int, message_id: int, text: str, force: bool = False) -> None:
    key = (chat_id, message_id)
    now = time.monotonic()
    if not force and now - last_edit.get(key, 0) < 2.2:
        return
    last_edit[key] = now
    try:
        bot.edit_message_text(text, chat_id, message_id, parse_mode="HTML")
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            log.debug("Could not edit progress message: %s", exc)


def send_upload_with_progress(chat_id: int, path: Path, caption: str, status_message_id: int) -> bool:
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendDocument"
    total = path.stat().st_size
    with path.open("rb") as file_obj:
        encoder = MultipartEncoder(fields={
            "chat_id": str(chat_id),
            "caption": caption[:1024],
            "parse_mode": "HTML",
            "document": (path.name, file_obj, "application/octet-stream"),
        })
        last_update = [0.0, -1]
        def on_upload(monitor):
            percent = monitor.bytes_read * 100 / max(1, monitor.len)
            now = time.monotonic()
            if percent >= 100 or (percent - last_update[1] >= 3 and now - last_update[0] >= 1.5):
                last_update[:] = [now, percent]
                text = (
                    "📤 <b>Uploading to Telegram</b>\n"
                    f"<code>{progress_bar(percent)}</code> <b>{percent:.1f}%</b>\n"
                    f"{human_size(monitor.bytes_read)} / {human_size(monitor.len)}"
                )
                edit_status(chat_id, status_message_id, text, force=percent >= 100)
        monitor = MultipartEncoderMonitor(encoder, on_upload)
        response = requests.post(
            url, data=monitor, headers={"Content-Type": monitor.content_type},
            timeout=(30, 1800),
        )
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.ok and payload.get("ok"):
        return True
    description = payload.get("description", response.text[:300])
    raise RuntimeError(f"Telegram upload failed: {description}")


def download_and_send(chat_id: int, user_id: int, status_message_id: int, mode: str, format_id: str):
    session = sessions.get(user_id, {})
    url = session.get("url")
    info = session.get("info", {})
    if not url:
        edit_status(chat_id, status_message_id, "⚠️ Session expired. Send the link again.", True)
        return
    job_id = uuid.uuid4().hex[:12]
    out_template = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")
    last_progress = [0.0, -1.0]
    def hook(data):
        if data.get("status") == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
            downloaded = data.get("downloaded_bytes") or 0
            percent = downloaded * 100 / total if total else 0
            now = time.monotonic()
            if percent >= 100 or (percent - last_progress[1] >= 2 and now - last_progress[0] >= 1.5):
                last_progress[:] = [now, percent]
                speed = human_size(data.get("speed")) + "/s" if data.get("speed") else "calculating…"
                eta = duration_text(data.get("eta")) if data.get("eta") is not None else "—"
                edit_status(
                    chat_id, status_message_id,
                    "⬇️ <b>Downloading</b>\n"
                    f"<code>{progress_bar(percent)}</code> <b>{percent:.1f}%</b>\n"
                    f"{human_size(downloaded)} / {human_size(total)}\n"
                    f"⚡ {html.escape(speed)}   ⏱ ETA {eta}",
                )
        elif data.get("status") == "finished":
            edit_status(chat_id, status_message_id, "✅ Download finished.\n⏳ Preparing file for Telegram upload…", True)

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": out_template,
        "progress_hooks": [hook],
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 30,
        "continuedl": True,
        "overwrites": True,
    }
    if mode == "audio":
        opts.update({"format": "bestaudio/best", "postprocessors": [{
            "key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192",
        }]})
    else:
        opts.update({"format": f"{format_id}+bestaudio/best" if format_id != "best" else "bestvideo+bestaudio/best",
                     "merge_output_format": "mp4"})
    files_before = set(DOWNLOAD_DIR.glob(f"{job_id}.*"))
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = [p for p in DOWNLOAD_DIR.glob(f"{job_id}.*") if p.is_file() and p not in files_before]
        if not files:
            raise RuntimeError("Download finished but output file was not found.")
        # Prefer the requested final container and ignore temporary/partial files.
        suffix = ".mp3" if mode == "audio" else ".mp4"
        preferred = [p for p in files if p.suffix.lower() == suffix]
        path = max(preferred or files, key=lambda p: p.stat().st_size)
        size = path.stat().st_size
        if size > MAX_FILE_MB * 1024 * 1024:
            raise RuntimeError(
                f"File is {human_size(size)}; configured Telegram upload limit is {MAX_FILE_MB} MB. "
                "Use a lower quality or download it through the ReClip web UI."
            )
        caption = (
            f"✅ <b>{html.escape(info.get('title') or path.stem)}</b>\n"
            f"📦 {human_size(size)} · {suffix[1:].upper()}\n"
            "Downloaded with ReClip"
        )
        if send_upload_with_progress(chat_id, path, caption, status_message_id):
            edit_status(chat_id, status_message_id, "🎉 <b>Complete!</b>\nFile uploaded to this chat.", True)
    except (DownloadError, subprocess.SubprocessError, requests.RequestException, RuntimeError) as exc:
        log.warning("Job failed for user %s: %s", user_id, exc)
        edit_status(chat_id, status_message_id, f"❌ <b>Download failed</b>\n{html.escape(str(exc)[:900])}", True)
    except Exception:
        log.exception("Unexpected download failure")
        edit_status(chat_id, status_message_id, "❌ Something went wrong. Check the bot logs and try again.", True)
    finally:
        for path in DOWNLOAD_DIR.glob(f"{job_id}.*"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        with active_lock:
            active_users.discard(user_id)


@bot.message_handler(commands=["start", "help"])
def start(message):
    bot.reply_to(
        message,
        "👋 <b>Welcome to ReClip Bot</b>\n\n"
        "Send me a video or audio link. I’ll fetch its title, thumbnail and available qualities, "
        "then let you choose <b>MP4</b> or <b>MP3</b> with buttons.\n\n"
        "• Quality picker for video\n• Live download progress, speed and ETA\n"
        "• Real Telegram upload progress\n• Supports sites supported by yt-dlp\n\n"
        "Use /cancel to clear your current link. Only download content you have permission to save.",
    )


@bot.message_handler(commands=["cancel"])
def cancel(message):
    sessions.pop(message.from_user.id, None)
    bot.reply_to(message, "🧹 Current link cleared. Send another URL when you’re ready.")


@bot.message_handler(func=lambda message: bool(message.text))
def receive_url(message):
    user_id = message.from_user.id
    url = message.text.strip()
    if not valid_url(url):
        bot.reply_to(message, "Send a valid <code>https://</code> or <code>http://</code> video/audio URL.")
        return
    status = bot.reply_to(message, "🔎 <b>Fetching link details…</b>\nGetting title, thumbnail and available qualities.")
    try:
        info = extract_info(url)
        sessions[user_id] = {"url": url, "info": info, "qualities": quality_options(info)}
        title = html.escape((info.get("title") or "Untitled media")[:180])
        uploader = html.escape((info.get("uploader") or info.get("channel") or "Unknown creator")[:100])
        details = (
            f"🎞 <b>{title}</b>\n"
            f"👤 {uploader}\n"
            f"⏱ {duration_text(info.get('duration'))}\n"
            f"📦 {len(sessions[user_id]['qualities'])} video quality options found\n\n"
            "Choose your output format:"
        )
        thumbnail = info.get("thumbnail")
        keyboard = make_format_keyboard(user_id)
        if thumbnail and thumbnail.startswith(("https://", "http://")):
            try:
                bot.send_photo(message.chat.id, thumbnail, caption=details, reply_markup=keyboard)
                try:
                    bot.delete_message(message.chat.id, status.message_id)
                except Exception:
                    pass
                return
            except Exception:
                log.debug("Thumbnail could not be sent; using text preview.")
        bot.edit_message_text(details, message.chat.id, status.message_id, reply_markup=keyboard, parse_mode="HTML")
    except Exception as exc:
        log.info("Fetch failed for user %s: %s", user_id, exc)
        bot.edit_message_text(
            f"❌ <b>Could not fetch this link</b>\n{html.escape(str(exc)[:800])}",
            message.chat.id, status.message_id, parse_mode="HTML",
        )


@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    user_id = call.from_user.id
    data = call.data or ""
    if data == "new":
        bot.answer_callback_query(call.id, "Send another link.")
        bot.send_message(call.message.chat.id, "🔗 Send the next video/audio URL.")
        return
    parts = data.split(":")
    if parts[0] in ("mode", "back", "q") and len(parts) >= 2:
        try:
            menu_owner = int(parts[1])
        except ValueError:
            menu_owner = -1
        if menu_owner != user_id:
            bot.answer_callback_query(call.id, "This menu belongs to another user.", show_alert=True)
            return
    session = sessions.get(user_id)
    if not session:
        bot.answer_callback_query(call.id, "Session expired. Send the URL again.", show_alert=True)
        return
    if parts[0] == "mode" and len(parts) == 3:
        mode = parts[1]
        if mode not in ("video", "audio"):
            bot.answer_callback_query(call.id, "Unknown format.")
            return
        session["mode"] = mode
        if mode == "audio":
            bot.answer_callback_query(call.id, "MP3 selected")
            keyboard = types.InlineKeyboardMarkup()
            keyboard.add(types.InlineKeyboardButton("⬇️ Download MP3", callback_data=f"q:{user_id}:best"))
            keyboard.add(types.InlineKeyboardButton("⬅️ Format", callback_data=f"back:{user_id}"))
            bot.edit_message_caption(
                "🎵 <b>MP3 audio selected</b>\nAudio will be extracted and converted to MP3.",
                call.message.chat.id, call.message.message_id, reply_markup=keyboard, parse_mode="HTML",
            ) if call.message.content_type == "photo" else bot.edit_message_text(
                "🎵 <b>MP3 audio selected</b>\nAudio will be extracted and converted to MP3.",
                call.message.chat.id, call.message.message_id, reply_markup=keyboard, parse_mode="HTML",
            )
        else:
            bot.answer_callback_query(call.id, "MP4 selected")
            options = session.get("qualities", [])
            keyboard = make_quality_keyboard(user_id, options)
            text = "🎬 <b>Choose video quality</b>\nPick a resolution, or choose best available."
            try:
                if call.message.content_type == "photo":
                    bot.edit_message_caption(text, call.message.chat.id, call.message.message_id, reply_markup=keyboard, parse_mode="HTML")
                else:
                    bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=keyboard, parse_mode="HTML")
            except Exception:
                bot.send_message(call.message.chat.id, text, reply_markup=keyboard)
        return
    if parts[0] == "back":
        bot.answer_callback_query(call.id)
        try:
            if call.message.content_type == "photo":
                bot.edit_message_caption("Choose your output format:", call.message.chat.id, call.message.message_id,
                                         reply_markup=make_format_keyboard(user_id))
            else:
                bot.edit_message_text("Choose your output format:", call.message.chat.id, call.message.message_id,
                                      reply_markup=make_format_keyboard(user_id))
        except Exception:
            bot.send_message(call.message.chat.id, "Choose your output format:", reply_markup=make_format_keyboard(user_id))
        return
    if parts[0] == "q" and len(parts) == 3:
        mode = session.get("mode")
        if mode not in ("video", "audio"):
            bot.answer_callback_query(call.id, "Choose MP4 or MP3 first.", show_alert=True)
            return
        format_id = parts[2] if mode == "video" else "best"
        if format_id != "best" and format_id not in {fid for fid, _ in session.get("qualities", [])}:
            bot.answer_callback_query(call.id, "Invalid quality choice.", show_alert=True)
            return
        with active_lock:
            if user_id in active_users:
                bot.answer_callback_query(call.id, "You already have a download running.", show_alert=True)
                return
            if len(active_users) >= MAX_CONCURRENT:
                bot.answer_callback_query(call.id, "The bot is busy. Try again in a moment.", show_alert=True)
                return
            active_users.add(user_id)
        bot.answer_callback_query(call.id, "Starting download…")
        # Keep the preview card intact; progress updates use a dedicated text message.
        status_message = bot.send_message(call.message.chat.id, "⏳ <b>Starting download…</b>")
        thread = threading.Thread(
            target=download_and_send,
            args=(call.message.chat.id, user_id, status_message.message_id, mode, format_id),
            daemon=True,
        )
        thread.start()
        return
    bot.answer_callback_query(call.id, "Unknown action.")


if __name__ == "__main__":
    log.info("Starting ReClip Telegram bot")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
