import os
import re
import base64
import logging
import subprocess
import tempfile
from pathlib import Path

import yt_dlp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

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

VOLUME_STEPS = [10, 25, 50, 75, 100]  # 100 = replace original audio; <100 = mix on top
START_STEPS = [0, 5, 10, 15, 20, 30, 45, 60, 90, 120]


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


def mix_audio_into_video(
    video_path: str, audio_path: str, volume: int, start_sec: int
) -> str:
    """Mix audio into video.

    volume=100 replaces the original audio entirely.
    volume<100 mixes the new audio on top of the original at that volume level.
    """
    output_path = video_path.replace(".mp4", "_mixed.mp4")
    vol = volume / 100.0

    if volume == 100:
        # Replace: drop original audio, use new track at full volume
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-ss", str(start_sec), "-i", audio_path,
            "-map", "0:v",
            "-map", "1:a",
            "-shortest",
            "-c:v", "copy",
            "-c:a", "aac",
            output_path,
        ]
    else:
        # Mix on top: keep original audio, layer new audio at reduced volume
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-ss", str(start_sec), "-i", audio_path,
            "-filter_complex",
            f"[0:a]volume=1[oa];[1:a]volume={vol}[na];[oa][na]amix=inputs=2:duration=first[aout]",
            "-map", "0:v",
            "-map", "[aout]",
            "-shortest",
            "-c:v", "copy",
            "-c:a", "aac",
            output_path,
        ]

    subprocess.run(cmd, check=True, capture_output=True)
    return output_path


def build_mix_keyboard(volume: int, start: int) -> InlineKeyboardMarkup:
    vi = VOLUME_STEPS.index(volume) if volume in VOLUME_STEPS else VOLUME_STEPS.index(100)
    si = START_STEPS.index(start) if start in START_STEPS else 0

    vd = VOLUME_STEPS[max(0, vi - 1)]
    vu = VOLUME_STEPS[min(len(VOLUME_STEPS) - 1, vi + 1)]
    sd = START_STEPS[max(0, si - 1)]
    su = START_STEPS[min(len(START_STEPS) - 1, si + 1)]

    vol_label = "🔇 Replace" if volume == 100 else f"🔊 {volume}% (mix)"

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"◀ {vd}%", callback_data=f"vol:{vd}"),
            InlineKeyboardButton(vol_label, callback_data="noop"),
            InlineKeyboardButton(f"▶ {vu}%", callback_data=f"vol:{vu}"),
        ],
        [
            InlineKeyboardButton(f"⏮ {sd}s", callback_data=f"start:{sd}"),
            InlineKeyboardButton(f"▶ Start: {start}s", callback_data="noop"),
            InlineKeyboardButton(f"⏭ {su}s", callback_data=f"start:{su}"),
        ],
        [
            InlineKeyboardButton("🎬 Mix & Send", callback_data="mix"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_mix"),
        ],
    ])


async def handle_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.video:
        await msg.reply_text("Reply to one of my videos with /edit to add music to it.")
        return

    video = msg.reply_to_message.video
    context.user_data["edit_video_file_id"] = video.file_id
    context.user_data["edit_video_duration"] = video.duration
    context.user_data["edit_video_width"] = video.width
    context.user_data["edit_video_height"] = video.height
    context.user_data["edit_state"] = "waiting_for_audio"
    context.user_data.setdefault("mix_volume", 100)
    context.user_data.setdefault("mix_start", 0)

    await msg.reply_text("🎵 Forward me an audio file to mix into this video.")


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("edit_state") != "waiting_for_audio":
        await update.message.reply_text(
            "Reply to one of my videos with /edit first, then forward an audio file."
        )
        return

    audio = update.message.audio or update.message.voice
    if not audio:
        await update.message.reply_text("Please send an audio file.")
        return

    context.user_data["edit_audio_file_id"] = audio.file_id
    context.user_data["edit_state"] = "configuring"

    volume = context.user_data["mix_volume"]
    start = context.user_data["mix_start"]

    await update.message.reply_text(
        "🎵 Audio ready! Adjust settings then hit Mix & Send:",
        reply_markup=build_mix_keyboard(volume, start),
    )


async def handle_mix_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "noop":
        return

    if data == "cancel_mix":
        for key in ("edit_state", "edit_video_file_id", "edit_audio_file_id",
                    "edit_video_duration", "edit_video_width", "edit_video_height"):
            context.user_data.pop(key, None)
        await query.edit_message_text("Mix cancelled.")
        return

    if data.startswith("vol:"):
        context.user_data["mix_volume"] = int(data.split(":")[1])
    elif data.startswith("start:"):
        context.user_data["mix_start"] = int(data.split(":")[1])

    if data != "mix":
        volume = context.user_data["mix_volume"]
        start = context.user_data["mix_start"]
        await query.edit_message_reply_markup(build_mix_keyboard(volume, start))
        return

    # --- Do the mix ---
    video_file_id = context.user_data.get("edit_video_file_id")
    audio_file_id = context.user_data.get("edit_audio_file_id")
    volume = context.user_data.get("mix_volume", 100)
    start = context.user_data.get("mix_start", 0)

    if not video_file_id or not audio_file_id:
        await query.edit_message_text("Session expired. Reply to a video with /edit to start over.")
        return

    await query.edit_message_text("⏳ Mixing audio, please wait...")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            video_tg = await context.bot.get_file(video_file_id)
            audio_tg = await context.bot.get_file(audio_file_id)

            video_path = os.path.join(tmpdir, "video.mp4")
            audio_path = os.path.join(tmpdir, "audio")

            await video_tg.download_to_drive(video_path)
            await audio_tg.download_to_drive(audio_path)

            mixed_path = mix_audio_into_video(video_path, audio_path, volume, start)

            size = os.path.getsize(mixed_path)
            if size > MAX_SIZE_BYTES:
                await query.edit_message_text(
                    f"Mixed video is too large ({size / 1024 / 1024:.1f} MB). "
                    "Telegram limits uploads to 50 MB."
                )
                return

            await query.edit_message_text("📤 Sending...")
            with open(mixed_path, "rb") as f:
                await context.bot.send_video(
                    chat_id=query.message.chat_id,
                    video=f,
                    supports_streaming=True,
                    width=context.user_data.get("edit_video_width"),
                    height=context.user_data.get("edit_video_height"),
                    duration=context.user_data.get("edit_video_duration"),
                    read_timeout=120,
                    write_timeout=120,
                )
            await query.delete_message()

            for key in ("edit_state", "edit_video_file_id", "edit_audio_file_id",
                        "edit_video_duration", "edit_video_width", "edit_video_height"):
                context.user_data.pop(key, None)

    except subprocess.CalledProcessError as e:
        logger.error("ffmpeg error: %s", e.stderr)
        await query.edit_message_text("❌ Failed to mix audio. Please try again.")
    except Exception:
        logger.exception("Unexpected error during mix")
        await query.edit_message_text("❌ An unexpected error occurred.")


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
    downloading_msg = (
        "Downloading... (Instagram videos may take a bit longer)"
        if is_instagram_url(url)
        else "Downloading..."
    )
    status_msg = await update.message.reply_text(downloading_msg)

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
    app.add_handler(CommandHandler("edit", handle_edit))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, handle_audio))
    app.add_handler(CallbackQueryHandler(handle_mix_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot started. Polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
