import json
import os
import re
import base64
import logging
import subprocess
import tempfile
import uuid
from pathlib import Path

import yt_dlp
from aiohttp import web as aio_web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application,
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

# Mini App web server — only available when RAILWAY_PUBLIC_DOMAIN is set.
MINI_APP_HOST = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()

# Cookies file written from INSTAGRAM_COOKIES env var (base64-encoded cookies.txt).
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

# token -> {path: str, tmpdir: TemporaryDirectory, duration: float}
_audio_sessions: dict[str, dict] = {}
_web_runner: aio_web.AppRunner | None = None


# ---------------------------------------------------------------------------
# URL / video helpers
# ---------------------------------------------------------------------------

def extract_urls(text: str) -> list[str]:
    return [m.group() for m in URL_PATTERN.finditer(text)]


def is_instagram_url(url: str) -> bool:
    return "instagram.com" in url or "instagr.am" in url


def build_ydl_opts(output_path: str, url: str) -> dict:
    is_instagram = is_instagram_url(url)
    is_tiktok = "tiktok.com" in url

    opts = {
        "outtmpl": output_path,
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
        opts["extractor_args"] = {"tiktok": {"download_without_watermark": True}}
    if is_instagram and COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    return opts


def reencode_h264(input_path: str) -> str:
    output_path = input_path.replace(".mp4", "_h264.mp4")
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", input_path,
            "-vcodec", "libx264", "-crf", "23", "-preset", "fast",
            "-acodec", "aac", "-movflags", "+faststart",
            output_path,
        ],
        check=True, capture_output=True,
    )
    return output_path


def download_video(url: str, output_path: str) -> tuple[str, dict]:
    opts = build_ydl_opts(output_path, url)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        p = Path(ydl.prepare_filename(info))
        if not p.exists():
            p = p.with_suffix(".mp4")
        if is_instagram_url(url):
            p = Path(reencode_h264(str(p)))
        return str(p), info


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def get_audio_duration(path: str) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True, text=True, check=True,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def mix_audio_into_video(video_path: str, audio_path: str, volume: int, start_sec: int) -> str:
    """Mix audio into video.
    volume=100 replaces the original audio; volume<100 mixes on top at that level.
    """
    output_path = video_path.replace(".mp4", "_mixed.mp4")
    vol = volume / 100.0

    if volume == 100:
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-ss", str(start_sec), "-i", audio_path,
            "-map", "0:v", "-map", "1:a",
            "-shortest", "-c:v", "copy", "-c:a", "aac",
            output_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-i", video_path,
            "-ss", str(start_sec), "-i", audio_path,
            "-filter_complex",
            f"[0:a]volume=1[oa];[1:a]volume={vol}[na];[oa][na]amix=inputs=2:duration=first[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-shortest", "-c:v", "copy", "-c:a", "aac",
            output_path,
        ]

    subprocess.run(cmd, check=True, capture_output=True)
    return output_path


def extract_audio_preview(audio_path: str, start_sec: int, tmpdir: str) -> str:
    preview_path = os.path.join(tmpdir, f"preview_{start_sec}.mp3")
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", str(start_sec), "-i", audio_path,
            "-t", "15", "-vn",
            "-c:a", "libmp3lame", "-q:a", "4",
            preview_path,
        ],
        check=True, capture_output=True,
    )
    return preview_path


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def build_volume_keyboard(volume: int, start_sec: int | None = None) -> InlineKeyboardMarkup:
    """Volume-only keyboard shown after position is set via Mini App."""
    vi = VOLUME_STEPS.index(volume) if volume in VOLUME_STEPS else VOLUME_STEPS.index(100)
    vd = VOLUME_STEPS[max(0, vi - 1)]
    vu = VOLUME_STEPS[min(len(VOLUME_STEPS) - 1, vi + 1)]
    vol_label = "🔇 Replace" if volume == 100 else f"🔊 {volume}% (mix)"

    rows = [
        [
            InlineKeyboardButton(f"◀ {vd}%", callback_data=f"vol:{vd}"),
            InlineKeyboardButton(vol_label, callback_data="noop"),
            InlineKeyboardButton(f"▶ {vu}%", callback_data=f"vol:{vu}"),
        ],
        [
            InlineKeyboardButton("🎬 Mix & Send", callback_data="mix"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_mix"),
        ],
    ]
    return InlineKeyboardMarkup(rows)


def build_full_keyboard(volume: int, start: int) -> InlineKeyboardMarkup:
    """Full keyboard with start-position controls (fallback when no Mini App)."""
    START_STEPS = [0, 5, 10, 15, 20, 30, 45, 60, 90, 120]
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
            InlineKeyboardButton(f"▶ {start}s", callback_data="noop"),
            InlineKeyboardButton(f"⏭ {su}s", callback_data=f"start:{su}"),
        ],
        [
            InlineKeyboardButton("🎧 Preview 15s", callback_data="preview"),
        ],
        [
            InlineKeyboardButton("🎬 Mix & Send", callback_data="mix"),
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_mix"),
        ],
    ])


# ---------------------------------------------------------------------------
# aiohttp web server (Mini App backend)
# ---------------------------------------------------------------------------

async def serve_audio(request: aio_web.Request) -> aio_web.Response:
    token = request.match_info["token"]
    session = _audio_sessions.get(token)
    if not session or not os.path.exists(session["path"]):
        raise aio_web.HTTPNotFound()
    return aio_web.FileResponse(
        session["path"],
        headers={"Cache-Control": "no-cache", "Access-Control-Allow-Origin": "*"},
    )


async def serve_miniapp(request: aio_web.Request) -> aio_web.Response:
    html_path = Path(__file__).parent / "miniapp.html"
    return aio_web.FileResponse(html_path)


async def post_init(application: Application) -> None:
    global _web_runner
    if not MINI_APP_HOST:
        logger.info("RAILWAY_PUBLIC_DOMAIN not set — Mini App disabled")
        return
    aio_app = aio_web.Application()
    aio_app.router.add_get("/audio/{token}", serve_audio)
    aio_app.router.add_get("/miniapp", serve_miniapp)
    _web_runner = aio_web.AppRunner(aio_app)
    await _web_runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = aio_web.TCPSite(_web_runner, "0.0.0.0", port)
    await site.start()
    logger.info("Mini App web server started on port %d", port)


async def post_shutdown(application: Application) -> None:
    if _web_runner:
        await _web_runner.cleanup()


def _cleanup_session(token: str | None) -> None:
    if not token:
        return
    session = _audio_sessions.pop(token, None)
    if session:
        try:
            session["tmpdir"].cleanup()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Bot handlers
# ---------------------------------------------------------------------------

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
    context.user_data["mix_start"] = 0

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

    status = await update.message.reply_text("⏳ Loading audio...")

    # Clean up any previous session for this user
    _cleanup_session(context.user_data.pop("edit_token", None))

    # Download to a persistent temp dir so the Mini App can serve it
    tmpdir = tempfile.TemporaryDirectory()
    audio_path = os.path.join(tmpdir.name, "audio")
    try:
        tg_file = await context.bot.get_file(audio.file_id)
        await tg_file.download_to_drive(audio_path)
    except Exception:
        tmpdir.cleanup()
        logger.exception("Failed to download audio")
        await status.edit_text("❌ Failed to download audio. Please try again.")
        return

    duration = get_audio_duration(audio_path)
    token = str(uuid.uuid4())
    _audio_sessions[token] = {"path": audio_path, "tmpdir": tmpdir, "duration": duration}

    context.user_data["edit_audio_file_id"] = audio.file_id
    context.user_data["edit_token"] = token
    context.user_data["edit_audio_duration"] = duration
    context.user_data["mix_start"] = 0

    await status.delete()

    if MINI_APP_HOST:
        context.user_data["edit_state"] = "selecting_position"
        mini_app_url = (
            f"https://{MINI_APP_HOST}/miniapp"
            f"?token={token}&duration={int(duration)}"
        )
        await update.message.reply_text(
            "🎵 Audio loaded! Open the selector to pick your start position:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "🎧 Select start position",
                    web_app=WebAppInfo(url=mini_app_url),
                )
            ]]),
        )
    else:
        # No Mini App — fall back to button-based keyboard
        context.user_data["edit_state"] = "configuring"
        context.user_data["keyboard_type"] = "full"
        volume = context.user_data.get("mix_volume", 100)
        await update.message.reply_text(
            "🎵 Audio ready! Adjust settings then hit Mix & Send:",
            reply_markup=build_full_keyboard(volume, 0),
        )


async def handle_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        data = json.loads(update.message.web_app_data.data)
        start_sec = round(float(data["start_sec"]), 2)
    except (json.JSONDecodeError, KeyError, ValueError):
        logger.error("Invalid webapp data: %s", update.message.web_app_data.data)
        await update.message.reply_text("Something went wrong. Please try /edit again.")
        return

    context.user_data["mix_start"] = start_sec
    context.user_data["edit_state"] = "configuring"
    context.user_data["keyboard_type"] = "volume_only"
    volume = context.user_data.get("mix_volume", 100)

    total_s = int(start_sec)
    m, s = divmod(total_s, 60)
    frac = start_sec - total_s
    pos_str = f"{m}:{s:02d}.{int(frac * 10)}"
    await update.message.reply_text(
        f"✅ Start position set to {pos_str}\n\nAdjust volume then hit Mix & Send:",
        reply_markup=build_volume_keyboard(volume),
    )


async def handle_mix_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "noop":
        return

    if data == "cancel_mix":
        _cleanup_session(context.user_data.pop("edit_token", None))
        for key in ("edit_state", "edit_video_file_id", "edit_audio_file_id",
                    "edit_video_duration", "edit_video_width", "edit_video_height",
                    "keyboard_type"):
            context.user_data.pop(key, None)
        await query.edit_message_text("Mix cancelled.")
        return

    if data == "preview":
        token = context.user_data.get("edit_token")
        start = context.user_data.get("mix_start", 0)
        session = _audio_sessions.get(token) if token else None
        audio_file_id = context.user_data.get("edit_audio_file_id")
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                if session and os.path.exists(session["path"]):
                    audio_path = session["path"]
                    preview_path = extract_audio_preview(audio_path, start, tmpdir)
                elif audio_file_id:
                    audio_tg = await context.bot.get_file(audio_file_id)
                    audio_path = os.path.join(tmpdir, "audio")
                    await audio_tg.download_to_drive(audio_path)
                    preview_path = extract_audio_preview(audio_path, start, tmpdir)
                else:
                    await query.answer("No audio found.", show_alert=True)
                    return
                with open(preview_path, "rb") as f:
                    m, s = divmod(start, 60)
                    await context.bot.send_audio(
                        chat_id=query.message.chat_id,
                        audio=f,
                        title=f"Preview from {m}:{s:02d}",
                        duration=15,
                    )
        except subprocess.CalledProcessError as e:
            logger.error("Preview ffmpeg error: %s", e.stderr)
            await context.bot.send_message(query.message.chat_id, "❌ Failed to generate preview.")
        except Exception:
            logger.exception("Preview error")
        return

    if data.startswith("vol:"):
        context.user_data["mix_volume"] = int(data.split(":")[1])
    elif data.startswith("start:"):
        context.user_data["mix_start"] = int(data.split(":")[1])

    if data != "mix":
        volume = context.user_data["mix_volume"]
        start = context.user_data["mix_start"]
        if context.user_data.get("keyboard_type") == "volume_only":
            await query.edit_message_reply_markup(build_volume_keyboard(volume))
        else:
            await query.edit_message_reply_markup(build_full_keyboard(volume, start))
        return

    # --- Do the mix ---
    video_file_id = context.user_data.get("edit_video_file_id")
    audio_file_id = context.user_data.get("edit_audio_file_id")
    token = context.user_data.get("edit_token")
    volume = context.user_data.get("mix_volume", 100)
    start = context.user_data.get("mix_start", 0)

    if not video_file_id or not audio_file_id:
        await query.edit_message_text("Session expired. Reply to a video with /edit to start over.")
        return

    await query.edit_message_text("⏳ Mixing audio, please wait...")

    session = _audio_sessions.get(token) if token else None

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            video_tg = await context.bot.get_file(video_file_id)
            video_path = os.path.join(tmpdir, "video.mp4")
            await video_tg.download_to_drive(video_path)

            # Use pre-downloaded audio if available (Mini App flow), else re-download
            if session and os.path.exists(session["path"]):
                audio_path = session["path"]
            else:
                audio_tg = await context.bot.get_file(audio_file_id)
                audio_path = os.path.join(tmpdir, "audio")
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

            _cleanup_session(token)
            for key in ("edit_state", "edit_video_file_id", "edit_audio_file_id",
                        "edit_video_duration", "edit_video_width", "edit_video_height",
                        "edit_token", "keyboard_type"):
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("edit", handle_edit))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, handle_audio))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, handle_webapp_data))
    app.add_handler(CallbackQueryHandler(handle_mix_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot started. Polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
