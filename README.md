# ReClip

A self-hosted, open-source video and audio downloader with a clean web UI. Paste links from YouTube, TikTok, Instagram, Twitter/X, and 1000+ other sites — download as MP4 or MP3.

![Python](https://img.shields.io/badge/python-3.8+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

https://github.com/user-attachments/assets/419d3e50-c933-444b-8cab-a9724986ba05

![ReClip MP3 Mode](assets/preview-mp3.png)

## Features

- Download videos from 1000+ supported sites (via [yt-dlp](https://github.com/yt-dlp/yt-dlp))
- MP4 video or MP3 audio extraction
- Quality/resolution picker
- Bulk downloads — paste multiple URLs at once
- Automatic URL deduplication
- Clean, responsive UI — no frameworks, no build step
- Single Python file backend (~150 lines)

## Quick Start

```bash
brew install yt-dlp ffmpeg    # or apt install ffmpeg && pip install yt-dlp
git clone https://github.com/averygan/reclip.git
cd reclip
./reclip.sh
```

Open **http://localhost:8899**.

Or with Docker:

```bash
docker build -t reclip . && docker run -p 8899:8899 reclip
```

## Usage

1. Paste one or more video URLs into the input box
2. Choose **MP4** (video) or **MP3** (audio)
3. Click **Fetch** to load video info and thumbnails
4. Select quality/resolution if available
5. Click **Download** on individual videos, or **Download All**

## Supported Sites

Anything [yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), including:

YouTube, TikTok, Instagram, Twitter/X, Reddit, Facebook, Vimeo, Twitch, Dailymotion, SoundCloud, Loom, Streamable, Pinterest, Tumblr, Threads, LinkedIn, and many more.

## Stack

- **Backend:** Python + Flask (~150 lines)
- **Frontend:** Vanilla HTML/CSS/JS (single file, no build step)
- **Download engine:** [yt-dlp](https://github.com/yt-dlp/yt-dlp) + [ffmpeg](https://ffmpeg.org/)
- **Dependencies:** 2 (Flask, yt-dlp)

## Telegram Bot Mode (Pyrogram / MTProto)

The optional Pyrogram bot keeps the original ReClip web UI and adds Telegram inline buttons for **MP4 / MP3**, video quality selection, link metadata/thumbnail preview, live yt-dlp download percentage/speed/ETA, and Telegram upload percentage. Uploads use Telegram's MTProto API through Pyrogram instead of the HTTP Bot API. The configured target is about **2 GB per file**, subject to Telegram's current account/API limits.

### Configure and run with Docker Compose

1. Create a bot with [@BotFather](https://t.me/BotFather).
2. Get `API_ID` and `API_HASH` from [my.telegram.org](https://my.telegram.org).
3. Copy the environment template and fill in your credentials:

```bash
cp .env.example .env
nano .env
docker compose up -d --build
docker compose logs -f reclip-telegram
```

Keep `.env` and your bot credentials private. Do not commit real tokens to GitHub.

- Web UI: **http://YOUR_SERVER_IP:8899**
- Bot: start it by opening the bot in Telegram and sending `/start`.

### Configuration

- `API_ID`, `API_HASH`, `TELEGRAM_BOT_TOKEN`: required Telegram credentials.
- `MAX_FILE_SIZE_MB=2000`: target maximum output size (~2 GB). This is a local guard, not a way to bypass Telegram's server-side limits.
- `BOT_MAX_CONCURRENT=2`: maximum simultaneous downloads on the VPS.
- `PROGRESS_INTERVAL=2`: minimum interval between progress message edits, in seconds.

### Run the bot directly

Install FFmpeg and Python dependencies, then configure the same variables from `.env.example` in your shell:

```bash
pip install -r requirements.txt
export API_ID="your_api_id"
export API_HASH="your_api_hash"
export TELEGRAM_BOT_TOKEN="your_bot_token"
python telegram_bot.py
```

Pyrogram uses MTProto for large file uploads. Availability and exact file-size limits can depend on Telegram's current rules and the bot/account context; if a 2 GB upload is rejected, check the returned Telegram error. Downloading/conversion uses yt-dlp and FFmpeg. Only download media you are authorized to save.

## Disclaimer

This tool is intended for personal use only. Please respect copyright laws and the terms of service of the platforms you download from. The developers are not responsible for any misuse of this tool.

## License

[MIT](LICENSE)
