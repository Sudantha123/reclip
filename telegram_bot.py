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
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"

def load_env_file():
    if not ENV_FILE.is_file():
        return
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

load_env_file()

API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", str(Path(__file__).parent / "downloads")))
MAX_FILE_SIZE_MB = int(os.getenv("MAX_FILE_SIZE_MB", "2000"))
MAX_CONCURRENT = max(1, int(os.getenv("BOT_MAX_CONCURRENT", "2")))
PROGRESS_INTERVAL = max(1.0, float(os.getenv("PROGRESS_INTERVAL", "2.0")))

if not API_ID or not API_HASH or not BOT_TOKEN:
    raise SystemExit(f"Missing API_ID, API_HASH or TELEGRAM_BOT_TOKEN. Check {ENV_FILE}")

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



# Per-user cookie profiles. Cookie files contain session secrets: never print values.
COOKIE_ROOT = BASE_DIR / "cookies"
COOKIE_ROOT.mkdir(parents=True, exist_ok=True)
cookie_upload_targets = {}

def normalize_domain(value):
    value = value.strip().lower()
    if "://" in value:
        value = urlparse(value).hostname or ""
    value = value.split("/", 1)[0].split(":", 1)[0].strip(".")
    if not value or any(ch.isspace() for ch in value) or "." not in value:
        return None
    if not re.fullmatch(r"[a-z0-9.-]+", value) or ".." in value:
        return None
    return value

def cookie_profile_dir(uid):
    path = COOKIE_ROOT / str(uid)
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path

def cookie_profiles(uid):
    folder = cookie_profile_dir(uid)
    result = []
    for path in sorted(folder.glob("*.txt")):
        domain = path.stem
        try:
            records = parse_cookie_file(path)
            result.append({"domain": domain, "path": path, "count": len(records),
                           "expired": sum(1 for x in records if x["expiry"] and x["expiry"] <= int(time.time()))})
        except ValueError:
            result.append({"domain": domain, "path": path, "count": 0, "expired": 0, "invalid": True})
    return result

def parse_cookie_file(path):
    records = []
    with open(path, "r", encoding="utf-8", errors="replace") as stream:
        for raw in stream:
            line = raw.rstrip("\\r\\n")
            if not line or line.startswith("#") and not line.startswith("#HttpOnly_"):
                continue
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            parts = line.split("\\t")
            if len(parts) != 7:
                raise ValueError("Invalid Netscape cookies.txt format: expected 7 tab-separated fields.")
            domain, include_subdomains, cookie_path, secure, expiry, name, value = parts
            if not name or not domain:
                raise ValueError("Cookie row is missing a domain or name.")
            try:
                expires = int(expiry or "0")
            except ValueError:
                raise ValueError("Cookie expiry must be a Unix timestamp.")
            records.append({
                "domain": domain.lstrip(".").lower(),
                "include_subdomains": include_subdomains.upper() == "TRUE" or domain.startswith("."),
                "path": cookie_path or "/",
                "secure": secure.upper() == "TRUE",
                "expiry": expires,
                "name": name,
            })
    if not records:
        raise ValueError("No Netscape-format cookie records found.")
    return records

def cookie_domain_matches(cookie_domain, include_subdomains, host):
    cookie_domain = cookie_domain.lstrip(".").lower()
    host = (host or "").lower().strip(".")
    return host == cookie_domain or (include_subdomains and host.endswith("." + cookie_domain))

def cookie_file_for_url(uid, url):
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return None
    matches = []
    for profile in cookie_profiles(uid):
        if profile.get("invalid"):
            continue
        try:
            records = parse_cookie_file(profile["path"])
        except ValueError:
            continue
        score = 0
        for record in records:
            if cookie_domain_matches(record["domain"], record["include_subdomains"], host):
                score += 1
        if score:
            # Prefer the most specific profile name; only cookies whose own
            # Netscape domain scope matches the URL will be honored by yt-dlp.
            matches.append((len(profile["domain"]), score, profile["path"]))
    return max(matches, default=(0, 0, None), key=lambda item: (item[0], item[1]))[2]

def cookie_options_for_url(uid, url):
    path = cookie_file_for_url(uid, url)
    return {"cookiefile": str(path)} if path else {}


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


def extract_info(url: str, cookie_options: dict | None = None) -> dict:
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
        # Pyrogram uses Telegram MTProto; send media with its native Telegram preview.
        if mode == "audio" and path.suffix.lower() == ".mp3":
            await client.send_audio(
                chat_id=chat_id,
                audio=str(path),
                file_name=safe_filename(title) + ".mp3",
                title=title[:64],
                caption=caption,
                progress=upload_progress,
                progress_args=(),
            )
        elif path.suffix.lower() == ".mp4":
            await client.send_video(
                chat_id=chat_id,
                video=str(path),
                file_name=safe_filename(title) + ".mp4",
                caption=caption,
                supports_streaming=True,
                progress=upload_progress,
                progress_args=(),
            )
        else:
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



@app.on_message(filters.private & filters.command("cookies"))
async def cookies_command(client, message):
    uid = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) == 2:
        domain = normalize_domain(args[1])
        if not domain:
            await message.reply_text("Send a valid domain, e.g. /cookies youtube.com")
            return
        cookie_upload_targets[uid] = domain
        await message.reply_text(
            f"🍪 <b>Cookie upload target:</b> <code>{html.escape(domain)}</code>\\n"
            "Now upload the Netscape-format cookies.txt as a Telegram document. "
            "The profile is private to your Telegram account."
        )
        return
    profiles = cookie_profiles(uid)
    if not profiles:
        await message.reply_text(
            "🍪 No cookie profiles saved. Start with <code>/cookies youtube.com</code>, "
            "then upload your cookies.txt file."
        )
        return
    lines = ["🍪 <b>Your cookie profiles</b>"]
    for p in profiles:
        status = "⚠️ invalid format" if p.get("invalid") else f"{p['count']} cookies · {p['expired']} expired"
        lines.append(f"• <code>{html.escape(p['domain'])}</code> — {status}")
    lines.append("\\nUse /cookieinfo for details or /checkcookies URL to test a site.")
    await message.reply_text("\\n".join(lines), disable_web_page_preview=True)

@app.on_message(filters.private & filters.command("cookieinfo"))
async def cookieinfo_command(client, message):
    uid = message.from_user.id
    profiles = cookie_profiles(uid)
    if not profiles:
        await message.reply_text("No cookie profiles saved. Use /cookies domain.com first.")
        return
    lines = ["🍪 <b>Cookie profile details</b>"]
    for p in profiles:
        lines.append(f"\\n<b>{html.escape(p['domain'])}</b>")
        if p.get("invalid"):
            lines.append("Format: invalid")
            continue
        lines.append(f"Records: {p['count']} · Expired: {p['expired']}")
        try:
            records = parse_cookie_file(p["path"])
            domains = sorted({r["domain"] for r in records})
            lines.append("Cookie domains: " + html.escape(", ".join(domains[:15])))
            if len(domains) > 15:
                lines.append(f"… and {len(domains) - 15} more")
            lines.append("Secure cookies: " + str(sum(r["secure"] for r in records)))
            lines.append("HttpOnly flags are preserved when present.")
        except ValueError as exc:
            lines.append("Format error: " + html.escape(str(exc)))
    lines.append("\\n⚠️ Cookie values are never displayed.")
    await message.reply_text("\\n".join(lines), disable_web_page_preview=True)

@app.on_message(filters.private & filters.command("checkcookies"))
async def checkcookies_command(client, message):
    uid = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) != 2 or not valid_url(args[1]):
        await message.reply_text("Usage: <code>/checkcookies https://example.com/</code>")
        return
    url = args[1].strip()
    path = cookie_file_for_url(uid, url)
    if not path:
        await message.reply_text(
            "❌ No saved cookies match this URL's host and cookie domain scope. "
            "Use /cookies example.com and upload a valid cookies.txt."
        )
        return
    try:
        records = parse_cookie_file(path)
    except ValueError as exc:
        await message.reply_text("❌ Invalid cookie file: " + html.escape(str(exc)))
        return
    host = (urlparse(url).hostname or "").lower()
    matched = [r for r in records if cookie_domain_matches(r["domain"], r["include_subdomains"], host)]
    expired = sum(1 for r in matched if r["expiry"] and r["expiry"] <= int(time.time()))
    status = await message.reply_text(
        f"🍪 <b>Cookie file check</b>\\nHost: <code>{html.escape(host)}</code>\\n"
        f"Profile: <code>{html.escape(path.stem)}</code>\\n"
        f"Matching cookie records: {len(matched)}\\nExpired matches: {expired}\\n"
        "⏳ Testing whether the site accepts this session…"
    )
    try:
        test_opts = {
            "quiet": True, "no_warnings": True, "noplaylist": True,
            "skip_download": True, "socket_timeout": 20, "retries": 1,
            "extractor_retries": 1, "cookiefile": str(path),
        }
        info = await asyncio.to_thread(
            lambda: _extract_info_with_options(url, test_opts)
        )
        await status.edit_text(
            "✅ <b>Extraction test succeeded</b>\\n"
            f"Title: {html.escape(str(info.get('title') or 'Unknown')[:180])}\\n"
            "This confirms the extractor could access the URL; it does not guarantee every account-only feature works."
        )
    except Exception as exc:
        await status.edit_text(
            "⚠️ <b>Extraction test failed</b>\\n"
            + html.escape(str(exc)[:700])
            + "\\nCookies may be expired, unsupported by the site, or unnecessary for this URL."
        )

def _extract_info_with_options(url, opts):
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

@app.on_message(filters.private & filters.command("deletecookies"))
async def deletecookies_command(client, message):
    uid = message.from_user.id
    args = message.text.split(maxsplit=1)
    if len(args) != 2:
        await message.reply_text("Usage: <code>/deletecookies example.com</code>")
        return
    domain = normalize_domain(args[1])
    if not domain:
        await message.reply_text("Invalid domain.")
        return
    path = cookie_profile_dir(uid) / f"{domain}.txt"
    if not path.exists():
        await message.reply_text("No exact profile found for that domain.")
        return
    path.unlink()
    cookie_upload_targets.pop(uid, None)
    await message.reply_text(f"🗑 Deleted cookie profile for <code>{html.escape(domain)}</code>.")

@app.on_message(filters.private & filters.document)
async def cookie_upload_handler(client, message):
    uid = message.from_user.id
    target = cookie_upload_targets.get(uid)
    document = message.document
    filename = (document.file_name or "").lower()
    if not target:
        await message.reply_text(
            "To save cookies for a site, first send <code>/cookies example.com</code>, "
            "then upload the cookies.txt file."
        )
        return
    if not (filename.endswith(".txt") or filename.endswith(".cookies")):
        await message.reply_text("Please upload a Netscape-format .txt cookies file.")
        return
    if document.file_size and document.file_size > 5 * 1024 * 1024:
        await message.reply_text("Cookie file is too large (maximum 5 MB).")
        return
    temp = cookie_profile_dir(uid) / f".upload-{uuid.uuid4().hex}.tmp"
    try:
        await message.reply_text("🔎 Validating cookie file…")
        await client.download_media(message, file_name=str(temp))
        records = parse_cookie_file(temp)
        host_domains = sorted({r["domain"] for r in records})
        # The selected profile label is not used to broaden cookie scope;
        # actual Netscape cookie domains control where cookies are sent.
        profile_path = cookie_profile_dir(uid) / f"{target}.txt"
        os.replace(temp, profile_path)
        try:
            profile_path.chmod(0o600)
        except OSError:
            pass
        matched = sum(
            1 for r in records
            if cookie_domain_matches(r["domain"], r["include_subdomains"], target)
        )
        await message.reply_text(
            f"✅ <b>Cookies saved</b>\\nProfile: <code>{html.escape(target)}</code>\\n"
            f"Records: {len(records)} · Domains in file: {len(host_domains)}\\n"
            f"Records matching profile host: {matched}\\n"
            "Use /cookieinfo to inspect metadata or /checkcookies URL to test it. "
            "Cookie values are never shown."
        )
        cookie_upload_targets.pop(uid, None)
    except Exception as exc:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        await message.reply_text(
            "❌ Could not save cookies: " + html.escape(str(exc)[:600])
            + "\\nExport cookies in Netscape cookies.txt format and try again."
        )


@app.on_message(filters.private & filters.command(["start", "help"]))
async def start_handler(client: Client, message):
    await message.reply_text(
        "👋 <b>Welcome to ReClip Bot</b>\n\n"
        "Send a media link. I’ll fetch its title, thumbnail and available qualities, then let you choose MP4/MP3.\n\n"
        "• Inline quality buttons\n• Download progress, speed and ETA\n"
        "• Telegram upload progress through Pyrogram/MTProto\n"
        "• Large file uploads, up to about 2 GB where supported\n\n"
        "Use /cancel to clear your current link.\\nCookie commands: /cookies, /cookieinfo, /checkcookies URL, /deletecookies domain. Only use cookies from your own accounts and sites you are authorized to access.",
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
        info = await asyncio.to_thread(extract_info, url, cookie_options_for_url(uid, url))
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
