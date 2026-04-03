import os
import re
import base64
import logging
import subprocess
import tempfile
from pathlib import Path

import yt_dlp
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
MAX_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB

# Cookies file written from INSTAGRAM_COOKIES env var (base64-encoded cookies.txt).
# Required for Instagram on server deployments — Instagram blocks datacenter IPs
# unless requests carry a valid logged-in session cookie.
COOKIES_FILE = Path("cookies.txt")
_COOKIES_B64 = os.environ.get("INSTAGRAM_COOKIES", "").strip()
if _COOKIES_B64 and not COOKIES_FILE.exists():
    try:
        COOKIES_FILE.write_bytes(base64.b64decode(_COOKIES_B64))
        logger.info("Wrote cookies.txt from INSTAGRAM_COOKIES env var")
    except Exception:
        logger.warning("Failed to decode INSTAGRAM_COOKIES — Instagram downloads will likely fail")

URL_PATTERN = re.compile(
    r"https?://(www\.)?"
    r"(tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com"
    r"|instagram\.com|instagr\.am)"
    r"\S+",
    re.IGNORECASE,
)


def extract_urls(text: str) -> list[str]:
    return [m.group() for m in URL_PATTERN.finditer(text)]


def build_ydl_opts(output_path: str, url: str) -> dict:
    is_instagram = "instagram.com" in url or "instagr.am" in url
    is_tiktok = "tiktok.com" in url

    opts = {
        "outtmpl": output_path,
        # Prefer h264 (avc) — Telegram can't play h265/HEVC inline.
        # Falls back to any mp4, then anything if needed.
        "format": (
            "bestvideo[vcodec^=avc][ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo[ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo+bestaudio/best"
        ),
        "merge_output_format": "mp4",
        "quiet": False,
        "no_warnings": False,
    }

    if is_tiktok:
        opts["extractor_args"] = {
            "tiktok": {"download_without_watermark": True}
        }

    if is_instagram and COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)

    return opts


def reencode_h264(input_path: str) -> str:
    """Re-encode to h264/aac mp4. Returns path to new file."""
    output_path = input_path.replace(".mp4", "_h264.mp4")
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", input_path,
            "-vcodec", "libx264", "-crf", "23", "-preset", "fast",
            "-acodec", "aac",
            "-movflags", "+faststart",
            output_path,
        ],
        check=True,
        capture_output=True,
    )
    return output_path


def download_video(url: str, output_path: str) -> tuple[str, dict]:
    opts = build_ydl_opts(output_path, url)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        p = Path(ydl.prepare_filename(info))
        if not p.exists():
            p = p.with_suffix(".mp4")

        # Re-encode Instagram videos to h264 — Instagram serves HEVC/h265
        # which Telegram and QuickTime can't play inline.
        if "instagram.com" in url or "instagr.am" in url:
            p = Path(reencode_h264(str(p)))

        return str(p), info


def is_instagram_url(url: str) -> bool:
    return "instagram.com" in url or "instagr.am" in url


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
            output_template = os.path.join(tmpdir, "video.%(ext)s")
            try:
                video_path, info = download_video(url, output_template)
            except yt_dlp.utils.UnsupportedError:
                await status_msg.edit_text("Unsupported URL or platform.")
                return
            except yt_dlp.utils.DownloadError as e:
                logger.error("Download error for %s: %s", url, e)
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
                    width=info.get("width"),
                    height=info.get("height"),
                    duration=int(info.get("duration") or 0) or None,
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
