#!/usr/bin/env python3
"""ReClip Telegram bot using Pyrogram/MTProto for large uploads (up to ~2 GB)."""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from pyrogram import Client, filters
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from yt_dlp.utils import DownloadError

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("reclip.telegram")

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", str(Path(__file__).parent / "downloads")))
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "2000"))
MAX_CONCURRENT = max(1, int(os.getenv("BOT_MAX_CONCURRENT", "2")))
PROGRESS_INTERVAL = max(1.0, float(os.getenv("PROGRESS_INTERVAL", "2.0")))

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise SystemExit("Set API_ID, API_HASH and TELEGRAM_BOT_TOKEN in .env")

DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
app = Client(
    "reclip_telegram_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    workers=8,
    sleep_threshold=30,
)
sessions: dict[int, dict] = {}
active_users: set[int] = set()
active_lock = threading.Lock()
last_edit: dict[tuple[int, int], float] = {}


def valid_url(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
        return parsed.scheme in ("http", "https") and bool(parsed.hostname) and not any(c.isspace() for c in value)
    except Exception:
        return False


def duration_text(seconds) -> str:
    if seconds is None:
        return "Unknown"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def human_size(value) -> str:
    if value is None:
        return "Unknown"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return "Unknown"


def bar(percent: float, width: int = 14) -> str:
    filled = max(0, min(width, int(percent / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", name).strip(" .")
    return re.sub(r"\s+", " ", name)[:150].strip() or "reclip-media"


def extract_info(url: str) -> dict:
    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "skip_download": True, "socket_timeout": 30,
        "retries": 2, "extractor_retries": 2,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def quality_options(info: dict) -> list[tuple[str, str]]:
    by_height = {}
    for fmt in info.get("formats", []):
        height = fmt.get("height")
        if not height or fmt.get("vcodec") in (None, "none"):
            continue
        fmt_id = str(fmt.get("format_id", ""))
        if not fmt_id or len(fmt_id) > 20:
            continue
        score = (fmt.get("tbr") or 0, fmt.get("filesize") or fmt.get("filesize_approx") or 0)
        old = by_height.get(int(height))
        if old is None or score > old[0]:
            by_height[int(height)] = (score, fmt_id)
    result = [(fmt_id, f"{height}p") for height, (_, fmt_id) in by_height.items()]
    result.sort(key=lambda item: int(item[1][:-1]), reverse=True)
    return result[:12]


def keyboard_formats(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🎬 MP4 Video", callback_data=f"mode:video:{uid}"),
            InlineKeyboardButton("🎵 MP3 Audio", callback_data=f"mode:audio:{uid}"),
        ],
        [InlineKeyboardButton("🔄 New link", callback_data="new")],
    ])


def keyboard_quality(uid: int, qualities: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for fmt_id, label in qualities:
        row.append(InlineKeyboardButton(label, callback_data=f"q:{uid}:{fmt_id}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⭐ Best available", callback_data=f"q:{uid}:best")])
    rows.append([InlineKeyboardButton("⬅️ Format", callback_data=f"back:{uid}")])
    return InlineKeyboardMarkup(rows)


async def edit_progress(message, text: str, force: bool = False) -> None:
    key = (message.chat.id, message.id)
    now = time.monotonic()
    if not force and now - last_edit.get(key, 0) < PROGRESS_INTERVAL:
        return
    last_edit[key] = now
    try:
        await message.edit_text(text, disable_web_page_preview=True)
    except Exception as exc:
        if "MESSAGE_NOT_MODIFIED" not in str(exc).upper():
            log.debug("Progress edit failed: %s", exc)


async def download_and_upload(client: Client, chat_id: int, uid: int, status_message, mode: str, format_id: str):
    session = sessions.get(uid, {})
    url = session.get("url")
    info = session.get("info", {})
    if not url:
        await edit_progress(status_message, "⚠️ Session expired. Send the link again.", True)
        return

    job_id = uuid.uuid4().hex[:12]
    title = info.get("title") or "ReClip media"
    template = str(DOWNLOAD_DIR / f"{job_id}.%(ext)s")
    loop = asyncio.get_running_loop()
    last_hook = [0.0, -1.0]

    def hook(data):
        if data.get("status") != "downloading":
            if data.get("status") == "finished":
                asyncio.run_coroutine_threadsafe(
                    edit_progress(status_message, "✅ Download finished.\n⏳ Preparing upload…", True), loop
                )
            return
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        done = data.get("downloaded_bytes") or 0
        percent = done * 100 / total if total else 0.0
        now = time.monotonic()
        if percent >= 100 or (percent - last_hook[1] >= 2 and now - last_hook[0] >= PROGRESS_INTERVAL):
            last_hook[:] = [now, percent]
            speed = f"{human_size(data.get('speed'))}/s" if data.get("speed") else "calculating…"
            eta = duration_text(data.get("eta")) if data.get("eta") is not None else "—"
            text = (
                "⬇️ <b>Downloading</b>\n"
                f"<code>{bar(percent)}</code> <b>{percent:.1f}%</b>\n"
                f"{human_size(done)} / {human_size(total)}\n"
                f"⚡ {html.escape(speed)} · ⏱ ETA {eta}"
            )
            asyncio.run_coroutine_threadsafe(edit_progress(status_message, text), loop)

    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "outtmpl": template, "progress_hooks": [hook],
        "retries": 3, "fragment_retries": 3, "socket_timeout": 30,
        "continuedl": True, "overwrites": True,
    }
    if mode == "audio":
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        })
    else:
        opts.update({
            "format": f"{format_id}+bestaudio/best" if format_id != "best" else "bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
        })

    try:
        await edit_progress(status_message, "⬇️ <b>Starting download…</b>", True)
        await asyncio.to_thread(_run_ytdlp, opts, url)
        candidates = [p for p in DOWNLOAD_DIR.glob(f"{job_id}.*") if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        if not candidates:
            raise RuntimeError("yt-dlp finished but no output file was found.")
        suffix = ".mp3" if mode == "audio" else ".mp4"
        preferred = [p for p in candidates if p.suffix.lower() == suffix]
        path = max(preferred or candidates, key=lambda p: p.stat().st_size)
        size = path.stat().st_size
        if size > MAX_FILE_SIZE_MB * 1024 * 1024:
            raise RuntimeError(
                f"File size is {human_size(size)}; configured limit is {MAX_FILE_SIZE_MB} MB. "
                "Choose a lower resolution or increase MAX_FILE_SIZE_MB if your Telegram account/API supports it."
            )
        if path.suffix.lower() not in (".mp3", ".mp4"):
            log.warning("Expected %s but yt-dlp output is %s", suffix, path.suffix)

        caption = (
            f"✅ <b>{html.escape(title[:180])}</b>\n"
            f"📦 {human_size(size)} · {path.suffix[1:].upper()}\n"
            "Downloaded with ReClip"
        )

        last_upload = [0.0, -1.0]
        async def upload_progress(current: int, total: int, *args):
            percent = current * 100 / max(total, 1)
            now = time.monotonic()
            if percent >= 100 or (percent - last_upload[1] >= 2 and now - last_upload[0] >= PROGRESS_INTERVAL):
                last_upload[:] = [now, percent]
                await edit_progress(
                    status_message,
                    "📤 <b>Uploading to Telegram</b>\n"
                    f"<code>{bar(percent)}</code> <b>{percent:.1f}%</b>\n"
                    f"{human_size(current)} / {human_size(total)}",
                    force=percent >= 100,
                )

        await edit_progress(status_message, "📤 <b>Uploading to Telegram…</b>", True)
        # Pyrogram uses Telegram's MTProto API rather than the HTTP Bot API.
        # send_document is used for both audio and video to avoid re-encoding.
        await client.send_document(
            chat_id=chat_id,
            document=str(path),
            file_name=safe_filename(title) + path.suffix,
            caption=caption,
            progress=upload_progress,
            progress_args=(),
        )
        await edit_progress(status_message, "🎉 <b>Complete!</b>\nFile uploaded to this chat.", True)

    except (DownloadError, RPCError, FloodWait, RuntimeError, OSError) as exc:
        log.warning("Job failed for user %s: %s", uid, exc)
        await edit_progress(status_message, f"❌ <b>Failed</b>\n{html.escape(str(exc)[:900])}", True)
    except Exception:
        log.exception("Unexpected download/upload failure")
        await edit_progress(status_message, "❌ Something went wrong. Check the bot logs and try again.", True)
    finally:
        for path in DOWNLOAD_DIR.glob(f"{job_id}.*"):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        with active_lock:
            active_users.discard(uid)


def _run_ytdlp(opts: dict, url: str):
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])


@app.on_message(filters.private & filters.command(["start", "help"]))
async def start_handler(client: Client, message):
    await message.reply_text(
        "👋 <b>Welcome to ReClip Bot</b>\n\n"
        "Send a media link. I’ll fetch its title, thumbnail and available qualities, then let you choose MP4/MP3.\n\n"
        "• Inline quality buttons\n• Download progress, speed and ETA\n"
        "• Telegram upload progress through Pyrogram/MTProto\n"
        "• Large file uploads, up to about 2 GB where supported\n\n"
        "Use /cancel to clear your current link. Only download media you are authorized to save.",
        disable_web_page_preview=True,
    )


@app.on_message(filters.private & filters.command("cancel"))
async def cancel_handler(client: Client, message):
    sessions.pop(message.from_user.id, None)
    await message.reply_text("🧹 Current link cleared. Send another URL when ready.")


@app.on_message(filters.private & filters.text & ~filters.command(["start", "help", "cancel"]))
async def link_handler(client: Client, message):
    uid = message.from_user.id
    url = message.text.strip()
    if not valid_url(url):
        await message.reply_text("Send a valid <code>https://</code> or <code>http://</code> media URL.")
        return

    status = await message.reply_text("🔎 <b>Fetching link details…</b>")
    try:
        info = await asyncio.to_thread(extract_info, url)
        qualities = quality_options(info)
        sessions[uid] = {"url": url, "info": info, "qualities": qualities}
        title = html.escape((info.get("title") or "Untitled media")[:180])
        uploader = html.escape((info.get("uploader") or info.get("channel") or "Unknown creator")[:100])
        text = (
            f"🎞 <b>{title}</b>\n👤 {uploader}\n⏱ {duration_text(info.get('duration'))}\n"
            f"🎚 {len(qualities)} video qualities found\n\nChoose your output format:"
        )
        thumb = info.get("thumbnail")
        if thumb and thumb.startswith(("https://", "http://")):
            try:
                await client.send_photo(
                    message.chat.id, thumb, caption=text, reply_markup=keyboard_formats(uid)
                )
                await status.delete()
                return
            except Exception as exc:
                log.debug("Thumbnail preview unavailable: %s", exc)
        await status.edit_text(text, reply_markup=keyboard_formats(uid), disable_web_page_preview=True)
    except Exception as exc:
        log.info("Fetch failed for user %s: %s", uid, exc)
        await status.edit_text(f"❌ <b>Could not fetch this link</b>\n{html.escape(str(exc)[:800])}")


@app.on_callback_query()
async def callback_handler(client: Client, query):
    uid = query.from_user.id
    data = query.data or ""
    if data == "new":
        await query.answer("Send another link.")
        await client.send_message(query.message.chat.id, "🔗 Send the next media URL.")
        return

    parts = data.split(":")
    if parts[0] in ("mode", "back", "q"):
        owner_index = 2 if parts[0] == "mode" else 1
        try:
            owner = int(parts[owner_index])
        except (ValueError, IndexError):
            owner = -1
        if owner != uid:
            await query.answer("This menu belongs to another user.", show_alert=True)
            return

    session = sessions.get(uid)
    if not session:
        await query.answer("Session expired. Send the URL again.", show_alert=True)
        return

    is_photo = bool(getattr(query.message, "photo", None))
    async def edit_menu(text, markup):
        if is_photo:
            await query.message.edit_caption(text, reply_markup=markup)
        else:
            await query.message.edit_text(text, reply_markup=markup, disable_web_page_preview=True)

    if parts[0] == "mode" and len(parts) == 3:
        mode = parts[1]
        if mode not in ("video", "audio"):
            await query.answer("Unknown format.")
            return
        session["mode"] = mode
        await query.answer("MP3 selected" if mode == "audio" else "MP4 selected")
        if mode == "audio":
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("⬇️ Download MP3", callback_data=f"q:{uid}:best")],
                [InlineKeyboardButton("⬅️ Format", callback_data=f"back:{uid}")],
            ])
            await edit_menu("🎵 <b>MP3 audio selected</b>\nAudio will be extracted and converted to MP3.", markup)
        else:
            await edit_menu("🎬 <b>Choose video quality</b>\nPick a resolution or best available.", keyboard_quality(uid, session.get("qualities", [])))
        return

    if parts[0] == "back":
        await query.answer()
        await edit_menu("Choose your output format:", keyboard_formats(uid))
        return

    if parts[0] == "q" and len(parts) == 3:
        mode = session.get("mode")
        if mode not in ("video", "audio"):
            await query.answer("Choose MP4 or MP3 first.", show_alert=True)
            return
        format_id = parts[2] if mode == "video" else "best"
        if format_id != "best" and format_id not in {fmt for fmt, _ in session.get("qualities", [])}:
            await query.answer("Invalid quality choice.", show_alert=True)
            return
        with active_lock:
            if uid in active_users:
                await query.answer("A download is already running.", show_alert=True)
                return
            if len(active_users) >= MAX_CONCURRENT:
                await query.answer("Bot is busy. Try again shortly.", show_alert=True)
                return
            active_users.add(uid)
        await query.answer("Starting download…")
        status = await client.send_message(query.message.chat.id, "⏳ <b>Starting download…</b>")
        asyncio.create_task(download_and_upload(client, query.message.chat.id, uid, status, mode, format_id))
        return

    await query.answer("Unknown action.")


if __name__ == "__main__":
    log.info("Starting ReClip Telegram bot with Pyrogram/MTProto")
    app.run()
