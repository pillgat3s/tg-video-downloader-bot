import os
import re
import base64
import logging
import tempfile
from pathlib import Path

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
# Instagram — instaloader
# ---------------------------------------------------------------------------

def _make_instaloader() -> instaloader.Instaloader:
    L = instaloader.Instaloader(
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
            L.login(IG_USERNAME, IG_PASSWORD)
            logger.info("Logged into Instagram as %s", IG_USERNAME)
        except Exception as e:
            logger.warning("Instagram login failed: %s", e)
    return L


# Module-level loader so we reuse the session across requests
_loader = _make_instaloader()


def download_instagram(url: str, output_dir: str) -> str:
    match = INSTAGRAM_SHORTCODE_RE.search(url)
    if not match:
        raise ValueError(f"Could not extract shortcode from Instagram URL: {url}")

    shortcode = match.group(1)
    post = instaloader.Post.from_shortcode(_loader.context, shortcode)

    _loader.dirname_pattern = output_dir
    _loader.download_post(post, target=output_dir)

    mp4_files = list(Path(output_dir).glob("*.mp4"))
    if not mp4_files:
        raise RuntimeError("No video file found after Instagram download")
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
            except (instaloader.exceptions.InstaloaderException, RuntimeError, ValueError) as e:
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
