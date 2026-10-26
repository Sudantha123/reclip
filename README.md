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

## Telegram Bot Mode

The Telegram bot is an optional interface alongside the original web UI. It fetches a link preview (title, creator, duration and thumbnail), then provides inline buttons for **MP4 / MP3** and available video resolutions. It reports yt-dlp download percentage, speed and ETA, followed by multipart upload progress while sending the finished file to Telegram.

### Run with Docker Compose

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy its token.
2. In the repository directory, set the token and start both services:

```bash
export TELEGRAM_BOT_TOKEN="123456:replace-with-your-token"
docker compose up -d --build
docker compose logs -f reclip-telegram
```

The web UI remains at **http://YOUR_SERVER_IP:8899** and the Telegram bot starts polling automatically. Keep the token private.

Optional environment variables:

- `TELEGRAM_MAX_FILE_MB` — maximum file size to attempt uploading (defaults to 49 MB for compatibility with the standard hosted Telegram Bot API).
- `BOT_MAX_CONCURRENT` — maximum simultaneous downloads across users (defaults to 2).

### Run the bot directly

Install FFmpeg, install the Python requirements, then run:

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="123456:replace-with-your-token"
python telegram_bot.py
```

For larger Telegram uploads, configure a self-hosted [Local Bot API Server](https://core.telegram.org/bots/api#using-a-local-bot-api-server) and adapt the upload endpoint in `telegram_bot.py` to that server. Downloading and conversion use yt-dlp/FFmpeg; only download media you are authorized to save.

## Disclaimer

This tool is intended for personal use only. Please respect copyright laws and the terms of service of the platforms you download from. The developers are not responsible for any misuse of this tool.

## License

[MIT](LICENSE)
