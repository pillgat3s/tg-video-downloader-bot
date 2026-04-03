# TikTok & Instagram Video Downloader Bot

A Telegram bot that downloads and sends videos from TikTok and Instagram links using `yt-dlp`.

## Features

- Send a single URL → bot replies with the video
- Send multiple URLs (one per line) → bot sends all videos in order
- TikTok: downloads without watermark
- Instagram: supports cookies for Reels or private content
- Skips videos over 50 MB with a clear message

## Setup

### 1. Get a bot token

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the token you receive (looks like `123456789:ABCdef...`)

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env and set TELEGRAM_BOT_TOKEN=your_token_here
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. (Optional) Instagram cookies

To download Reels or content that requires a login, export your Instagram session cookies as `cookies.txt` in Netscape format and place it in the project root.

You can export cookies using a browser extension such as **Get cookies.txt LOCALLY** (Chrome/Firefox).

### 5. Run locally

```bash
python bot.py
```

---

## Deploy on Railway

1. Push this repo to GitHub.
2. Create a new project on [Railway](https://railway.app) and connect the repo.
3. Add the environment variable `TELEGRAM_BOT_TOKEN` in the Railway dashboard under **Variables**.
4. Railway will auto-detect `railway.toml` and run `python bot.py`.

If you need Instagram cookie support on Railway, add `cookies.txt` to the repo (make sure it doesn't contain sensitive sessions you can't rotate) or use Railway's volume/file service.

---

## Usage

Send any supported URL to the bot:

```
https://www.tiktok.com/@user/video/123456789
https://www.instagram.com/reel/ABC123/
```

Or multiple links in one message:

```
https://www.tiktok.com/@user/video/111
https://www.instagram.com/reel/222/
```
