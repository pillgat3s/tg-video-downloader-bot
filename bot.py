import os
import re
import json
import logging
import tempfile
from pathlib import Path

import requests
import yt_dlp
import instaloader
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
IG_USERNAME = os.environ.get("INSTAGRAM_USERNAME")
IG_PASSWORD = os.environ.get("INSTAGRAM_PASSWORD")
MAX_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB

URL_PATTERN = re.compile(
    r"https?://(www\.)?"
    r"(tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com"
    r"|instagram\.com|instagr\.am)"
    r"\S+",
    re.IGNORECASE,
)

INSTAGRAM_SHORTCODE_RE = re.compile(r"/(?:reel|p|tv)/([A-Za-z0-9_-]+)")

# Looks like a real Chrome browser to Instagram
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
}


def extract_urls(text: str) -> list[str]:
    return [m.group() for m in URL_PATTERN.finditer(text)]


# ---------------------------------------------------------------------------
# TikTok — yt-dlp
# ---------------------------------------------------------------------------

def download_tiktok(url: str, output_path: str) -> str:
    opts = {
        "outtmpl": output_path,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": False,
        "no_warnings": False,
        "extractor_args": {
            "tiktok": {"download_without_watermark": True}
        },
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        p = Path(ydl.prepare_filename(info))
        if not p.exists():
            p = p.with_suffix(".mp4")
        return str(p)


# ---------------------------------------------------------------------------
# Instagram — embed scrape → instaloader fallback
# ---------------------------------------------------------------------------

def _scrape_instagram_embed(shortcode: str) -> str | None:
    """
    Fetch Instagram's public embed page and extract the video URL from the HTML.
    No auth required for public posts.
    """
    embed_url = f"https://www.instagram.com/p/{shortcode}/embed/captioned/"
    try:
        r = requests.get(embed_url, headers=BROWSER_HEADERS, timeout=15)
        r.raise_for_status()
        html = r.text

        # Instagram puts video data in a serialised JSON blob inside the embed HTML.
        # Try several patterns that have been observed across Instagram's embed versions.
        patterns = [
            r'"video_url"\s*:\s*"(https:[^"]+)"',
            r'video_url&quot;:&quot;(https:[^&]+)&quot;',
            r'"contentUrl"\s*:\s*"(https:[^"]+\.mp4[^"]*)"',
            r'<video[^>]+src="(https://[^"]+)"',
        ]
        for pattern in patterns:
            m = re.search(pattern, html)
            if m:
                url = m.group(1)
                # Unescape common encodings
                url = url.replace("\\u0026", "&").replace("\\/", "/").replace("&amp;", "&")
                logger.info("Found Instagram video URL via embed scrape")
                return url

        logger.warning("Embed page fetched but no video URL found (shortcode=%s)", shortcode)
    except requests.RequestException as e:
        logger.error("Failed to fetch Instagram embed page: %s", e)

    return None


def _download_direct(video_url: str, output_path: str) -> str:
    """Stream a video URL directly to disk."""
    r = requests.get(
        video_url,
        stream=True,
        timeout=60,
        headers={
            "User-Agent": BROWSER_HEADERS["User-Agent"],
            "Referer": "https://www.instagram.com/",
        },
    )
    r.raise_for_status()
    with open(output_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=65536):
            f.write(chunk)
    return output_path


# Module-level instaloader — reuses session across requests
_loader: instaloader.Instaloader | None = None

def _get_loader() -> instaloader.Instaloader:
    global _loader
    if _loader is None:
        _loader = instaloader.Instaloader(
            download_videos=True,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            post_metadata_txt_pattern="",
            filename_pattern="{shortcode}",
            quiet=False,
        )
        if IG_USERNAME and IG_PASSWORD:
            try:
                _loader.login(IG_USERNAME, IG_PASSWORD)
                logger.info("Logged into Instagram as %s", IG_USERNAME)
            except Exception as e:
                logger.warning("Instagram login failed: %s", e)
    return _loader


def download_instagram(url: str, output_dir: str) -> str:
    match = INSTAGRAM_SHORTCODE_RE.search(url)
    if not match:
        raise ValueError(f"Could not extract shortcode from URL: {url}")
    shortcode = match.group(1)

    # Strategy 1: embed page scraping — no auth needed for public posts
    video_url = _scrape_instagram_embed(shortcode)
    if video_url:
        out_path = os.path.join(output_dir, "video.mp4")
        return _download_direct(video_url, out_path)

    # Strategy 2: instaloader (works better with credentials for login-walled content)
    logger.info("Embed scrape failed, trying instaloader for %s", shortcode)
    L = _get_loader()
    post = instaloader.Post.from_shortcode(L.context, shortcode)
    L.dirname_pattern = output_dir
    L.download_post(post, target=output_dir)
    mp4_files = list(Path(output_dir).glob("*.mp4"))
    if not mp4_files:
        raise RuntimeError("No video file found after instaloader download")
    return str(mp4_files[0])


# ---------------------------------------------------------------------------
# Bot handlers
# ---------------------------------------------------------------------------

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text or ""
    urls = extract_urls(text)

    if not urls:
        await update.message.reply_text(
            "Please send a TikTok or Instagram URL (one per line for multiple videos)."
        )
        return

    for url in urls:
        await process_url(update, url)


async def process_url(update: Update, url: str) -> None:
    status_msg = await update.message.reply_text("Downloading...")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            is_instagram = "instagram.com" in url or "instagr.am" in url

            try:
                if is_instagram:
                    video_path = download_instagram(url, tmpdir)
                else:
                    output_template = os.path.join(tmpdir, "video.%(ext)s")
                    video_path = download_tiktok(url, output_template)
            except yt_dlp.utils.DownloadError as e:
                logger.error("yt-dlp error for %s: %s", url, e)
                await status_msg.edit_text(
                    "Download failed. The video may be private, deleted, or unavailable."
                )
                return
            except (instaloader.exceptions.InstaloaderException, RuntimeError, ValueError, requests.RequestException) as e:
                logger.error("Instagram error for %s: %s", url, e)
                await status_msg.edit_text(
                    "Download failed. The video may be private, deleted, or unavailable."
                )
                return

            size = os.path.getsize(video_path)
            if size > MAX_SIZE_BYTES:
                await status_msg.edit_text(
                    f"Video is too large ({size / 1024 / 1024:.1f} MB). "
                    "Telegram limits file uploads to 50 MB."
                )
                return

            await status_msg.edit_text("Sending video...")
            with open(video_path, "rb") as f:
                await update.message.reply_video(
                    video=f,
                    supports_streaming=True,
                    read_timeout=120,
                    write_timeout=120,
                )
            await status_msg.delete()

    except Exception:
        logger.exception("Unexpected error for %s", url)
        await status_msg.edit_text("An unexpected error occurred. Please try again.")


def main() -> None:
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot started. Polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
