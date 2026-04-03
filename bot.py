import json
import os
import re
import base64
import logging
import shutil
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

MINI_APP_HOST = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "").strip()

COOKIES_FILE = Path("cookies.txt")
_COOKIES_B64 = os.environ.get("INSTAGRAM_COOKIES", "").strip()
if _COOKIES_B64 and not COOKIES_FILE.exists():
    try:
        COOKIES_FILE.write_bytes(base64.b64decode(_COOKIES_B64))
        logger.info("Wrote cookies.txt from INSTAGRAM_COOKIES env var")
    except Exception:
        logger.warning("Failed to decode INSTAGRAM_COOKIES — Instagram downloads will likely fail")

YT_COOKIES_FILE = Path("yt_cookies.txt")
_YT_COOKIES_B64 = os.environ.get("YOUTUBE_COOKIES", "").strip()
if _YT_COOKIES_B64 and not YT_COOKIES_FILE.exists():
    try:
        YT_COOKIES_FILE.write_bytes(base64.b64decode(_YT_COOKIES_B64))
        logger.info("Wrote yt_cookies.txt from YOUTUBE_COOKIES env var")
    except Exception:
        logger.warning("Failed to decode YOUTUBE_COOKIES — YouTube downloads may fail")

URL_PATTERN = re.compile(
    r"https?://(www\.)?"
    r"(tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com"
    r"|instagram\.com|instagr\.am"
    r"|youtube\.com|youtu\.be"
    r"|twitter\.com|x\.com|t\.co)"
    r"\S+",
    re.IGNORECASE,
)

VOLUME_STEPS = [10, 25, 50, 75, 100]  # 100 = replace original audio; <100 = mix on top
START_STEPS  = [0, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240]

# token -> {path: str, tmpdir: TemporaryDirectory, duration: float}
_audio_sessions: dict[str, dict] = {}
_web_runner: aio_web.AppRunner | None = None
_bot_app: Application | None = None  # set in post_init, used by web endpoints


# ---------------------------------------------------------------------------
# URL / video helpers
# ---------------------------------------------------------------------------

def extract_urls(text: str) -> list[str]:
    return [m.group() for m in URL_PATTERN.finditer(text)]


def is_instagram_url(url: str) -> bool:
    return "instagram.com" in url or "instagr.am" in url


def is_youtube_url(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


def downloading_message(url: str) -> str:
    if is_instagram_url(url):
        return "Downloading... (Instagram videos may take a bit longer)"
    if is_youtube_url(url):
        return "Downloading... (YouTube videos may take a moment)"
    return "Downloading..."


def build_ydl_opts(output_path: str, url: str, yt_cookies_path: str | None = None) -> dict:
    opts = {
        "outtmpl": output_path,
        "merge_output_format": "mp4",
        "quiet": False,
        "no_warnings": False,
    }
    if is_youtube_url(url):
        # tv_embedded only has combined streams (no separate video+audio), use best single stream
        opts["format"] = "best[height<=1080]/best"
        opts["extractor_args"] = {"youtube": {"player_client": ["tv_embedded", "web_creator"]}}
        if yt_cookies_path:
            opts["cookiefile"] = yt_cookies_path
        elif YT_COOKIES_FILE.exists():
            opts["cookiefile"] = str(YT_COOKIES_FILE)
    else:
        opts["format"] = (
            "bestvideo[vcodec^=avc][ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo[ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo+bestaudio/best"
        )
    if "tiktok.com" in url:
        opts["extractor_args"] = {"tiktok": {"download_without_watermark": True}}
    if is_instagram_url(url) and COOKIES_FILE.exists():
        opts["cookiefile"] = str(COOKIES_FILE)
    return opts


def reencode_h264(input_path: str) -> str:
    output_path = input_path.replace(".mp4", "_h264.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-i", input_path,
         "-vcodec", "libx264", "-crf", "23", "-preset", "fast",
         "-acodec", "aac", "-movflags", "+faststart", output_path],
        check=True, capture_output=True,
    )
    return output_path


def download_video(url: str, output_path: str, yt_cookies_path: str | None = None) -> tuple[str, dict]:
    opts = build_ydl_opts(output_path, url, yt_cookies_path)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        p = Path(ydl.prepare_filename(info))
        if not p.exists():
            p = p.with_suffix(".mp4")
        if is_instagram_url(url) or is_youtube_url(url):
            p = Path(reencode_h264(str(p)))
        return str(p), info


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def get_audio_duration(path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, check=True,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def mix_audio_into_video(video_path: str, audio_path: str, volume: int, start_sec: float, loop: bool = False) -> str:
    output_path = video_path.replace(".mp4", "_mixed.mp4")
    vol = volume / 100.0
    loop_flags = ["-stream_loop", "-1"] if loop else []
    if volume == 100:
        cmd = [
            "ffmpeg", "-y", "-i", video_path,
            "-ss", str(start_sec), *loop_flags, "-i", audio_path,
            "-map", "0:v", "-map", "1:a",
            "-shortest", "-c:v", "copy", "-c:a", "aac", output_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y", "-i", video_path,
            "-ss", str(start_sec), *loop_flags, "-i", audio_path,
            "-filter_complex",
            f"[0:a]volume=1[oa];[1:a]volume={vol}[na];[oa][na]amix=inputs=2:duration=first:normalize=0[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-shortest", "-c:v", "copy", "-c:a", "aac", output_path,
        ]
    subprocess.run(cmd, check=True, capture_output=True)
    return output_path


def extract_audio_preview(audio_path: str, start_sec: float, tmpdir: str) -> str:
    preview_path = os.path.join(tmpdir, "preview.mp3")
    subprocess.run(
        ["ffmpeg", "-y", "-ss", str(start_sec), "-i", audio_path,
         "-t", "15", "-vn", "-c:a", "libmp3lame", "-q:a", "4", preview_path],
        check=True, capture_output=True,
    )
    return preview_path


# ---------------------------------------------------------------------------
# Start-position helpers
# ---------------------------------------------------------------------------

def prev_step(start: float) -> int:
    for s in reversed(START_STEPS):
        if s < start - 0.5:
            return s
    return 0


def next_step(start: float) -> int:
    for s in START_STEPS:
        if s > start + 0.5:
            return s
    return START_STEPS[-1]


def fmt_start(start: float) -> str:
    total = int(start)
    m, s = divmod(total, 60)
    frac = start - total
    if frac >= 0.05:
        return f"{m}:{s:02d}.{int(round(frac * 10))}"
    return f"{m}:{s:02d}"


# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------

def build_keyboard(volume: int, start: float, loop: bool = False, mini_app_url: str | None = None, show_loop: bool = False) -> InlineKeyboardMarkup:
    vi = VOLUME_STEPS.index(volume) if volume in VOLUME_STEPS else VOLUME_STEPS.index(100)
    vd = VOLUME_STEPS[max(0, vi - 1)]
    vu = VOLUME_STEPS[min(len(VOLUME_STEPS) - 1, vi + 1)]
    vol_label = "🔇 Replace" if volume == 100 else f"🔊 {volume}% (mix)"

    ps = prev_step(start)
    ns = next_step(start)

    third_row = (
        [InlineKeyboardButton("🎧 Precise selector", web_app=WebAppInfo(url=mini_app_url))]
        if mini_app_url
        else [InlineKeyboardButton("🎧 Preview 15s", callback_data="preview")]
    )

    loop_label = "🔁 Loop: ON" if loop else "➡️ Loop: OFF"
    rows = [
        [
            InlineKeyboardButton(f"◀ {vd}%", callback_data=f"vol:{vd}"),
            InlineKeyboardButton(vol_label, callback_data="noop"),
            InlineKeyboardButton(f"▶ {vu}%", callback_data=f"vol:{vu}"),
        ],
        [
            InlineKeyboardButton(f"⏮ {ps}s", callback_data=f"start:goto:{ps}"),
            InlineKeyboardButton("−1s", callback_data="start:sub1"),
            InlineKeyboardButton(f"▶ {fmt_start(start)}", callback_data="noop"),
            InlineKeyboardButton("+1s", callback_data="start:add1"),
            InlineKeyboardButton(f"⏭ {ns}s", callback_data=f"start:goto:{ns}"),
        ],
    ]
    if show_loop:
        rows.append([InlineKeyboardButton(loop_label, callback_data="toggle_loop")])
    rows.append(third_row)
    rows.append([
        InlineKeyboardButton("🎬 Mix & Send", callback_data="mix"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel_mix"),
    ])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# aiohttp web server
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
    return aio_web.FileResponse(Path(__file__).parent / "miniapp.html")


async def set_position(request: aio_web.Request) -> aio_web.Response:
    """Called by the Mini App when the user confirms a position (GET with query params)."""
    try:
        q          = request.rel_url.query
        token      = q["token"]
        chat_id    = int(q["chat_id"])
        user_id    = int(q["user_id"])
        message_id = int(q.get("message_id") or 0)
        start_sec  = round(float(q["start_sec"]), 2)
    except (KeyError, ValueError, TypeError) as e:
        logger.error("set_position bad params: %s", e)
        return aio_web.Response(status=400, text=f"Bad request: {e}")

    if not _bot_app:
        return aio_web.Response(status=503, text="Bot not ready")

    try:
        if token not in _audio_sessions:
            logger.warning("set_position: session %s not found (may have expired after redeploy)", token)
            # Don't reject — still update user_data and respond so mini app can close

        # Update user data
        if user_id not in _bot_app.user_data:
            _bot_app.user_data[user_id] = {}
        user_data = _bot_app.user_data[user_id]
        user_data["mix_start"] = start_sec
        user_data["edit_state"] = "configuring"

        volume    = user_data.get("mix_volume", 100)
        loop      = user_data.get("mix_loop", False)
        audio_dur = user_data.get("edit_audio_duration", float("inf"))
        video_dur = user_data.get("edit_video_duration", float("inf"))
        show_loop = audio_dur > 0 and audio_dur < video_dur
        mini_app_url = user_data.get("mini_app_url")

        total = int(start_sec)
        m, s  = divmod(total, 60)
        frac  = start_sec - total
        pos_str = f"{m}:{s:02d}.{int(round(frac * 10))}" if frac >= 0.05 else f"{m}:{s:02d}"

        # Try to edit the existing keyboard message in-place
        edited = False
        if message_id:
            try:
                await _bot_app.bot.edit_message_reply_markup(
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=build_keyboard(volume, start_sec, loop, mini_app_url, show_loop),
                )
                edited = True
            except Exception as e:
                logger.error("Failed to edit keyboard: %s", e)

        # Fall back to sending a new message if edit failed
        if not edited:
            try:
                await _bot_app.bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ Position set to {pos_str} — adjust volume then Mix & Send:",
                    reply_markup=build_keyboard(volume, start_sec, loop, None, show_loop),
                )
            except Exception as e:
                logger.error("Failed to send fallback message: %s", e)

        return aio_web.Response(status=200, text="OK")

    except Exception as e:
        logger.exception("set_position unhandled error: %s", e)
        return aio_web.Response(status=500, text=f"{type(e).__name__}: {e}")


async def post_init(application: Application) -> None:
    global _web_runner, _bot_app
    _bot_app = application

    await application.bot.set_my_commands([
        ("audio",      "Add music to a video — reply to a video"),
        ("stretch",    "Resize a video — reply to a video"),
        ("setcookies", "Set your YouTube cookies for restricted videos"),
        ("settings",   "View and adjust your preferences"),
        ("help",       "Show all commands and info"),
    ])

    if not MINI_APP_HOST:
        logger.info("RAILWAY_PUBLIC_DOMAIN not set — Mini App disabled")
        return
    aio_app = aio_web.Application()
    aio_app.router.add_get("/audio/{token}", serve_audio)
    aio_app.router.add_get("/miniapp", serve_miniapp)
    aio_app.router.add_get("/set-position", set_position)
    _web_runner = aio_web.AppRunner(aio_app)
    await _web_runner.setup()
    port = int(os.environ.get("PORT", 8080))
    await aio_web.TCPSite(_web_runner, "0.0.0.0", port).start()
    logger.info("Web server started on port %d", port)


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

async def handle_audio_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.video:
        await msg.reply_text("Reply to one of my videos with /audio to add music to it.")
        return
    video = msg.reply_to_message.video
    context.user_data["edit_video_file_id"]  = video.file_id
    context.user_data["edit_video_duration"] = video.duration
    context.user_data["edit_video_width"]    = video.width
    context.user_data["edit_video_height"]   = video.height
    context.user_data["edit_state"]          = "waiting_for_audio"
    context.user_data.setdefault("mix_volume", 100)
    context.user_data["mix_start"] = 0
    await msg.reply_text("🎵 Forward me an audio file to mix into this video.")


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("edit_state") != "waiting_for_audio":
        await update.message.reply_text(
            "Reply to one of my videos with /audio first, then forward an audio file."
        )
        return

    audio = update.message.audio or update.message.voice
    if not audio:
        await update.message.reply_text("Please send an audio file.")
        return

    status = await update.message.reply_text("⏳ Loading audio...")

    # Clean up any previous session
    _cleanup_session(context.user_data.pop("edit_token", None))

    # Download audio to a persistent temp dir
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
    token    = str(uuid.uuid4())
    _audio_sessions[token] = {"path": audio_path, "tmpdir": tmpdir, "duration": duration}

    context.user_data["edit_audio_file_id"]  = audio.file_id
    context.user_data["edit_token"]          = token
    context.user_data["edit_state"]          = "configuring"
    context.user_data["mix_start"]           = 0
    context.user_data["edit_audio_duration"] = duration
    context.user_data.setdefault("mix_loop", False)
    volume    = context.user_data.get("mix_volume", 100)
    loop      = context.user_data.get("mix_loop", False)
    video_dur = context.user_data.get("edit_video_duration", float("inf"))
    show_loop = duration > 0 and duration < video_dur

    await status.delete()

    # Send the keyboard message; we need its message_id to build the Mini App URL
    sent = await update.message.reply_text(
        "🎵 Audio ready! Use the buttons to set start position, or open the Precise Selector:",
        reply_markup=build_keyboard(volume, 0, loop, None, show_loop),  # placeholder — updated below
    )

    # Build Mini App URL now that we have the message_id
    mini_app_url = None
    if MINI_APP_HOST:
        chat_id = update.effective_chat.id
        user_id = update.effective_user.id
        mini_app_url = (
            f"https://{MINI_APP_HOST}/miniapp"
            f"?token={token}&duration={int(duration)}"
            f"&chat_id={chat_id}&user_id={user_id}&message_id={sent.message_id}"
        )
        context.user_data["mini_app_url"] = mini_app_url
        context.user_data["keyboard_message_id"] = sent.message_id
        # Edit keyboard to include the Mini App button with the correct URL
        await sent.edit_reply_markup(build_keyboard(volume, 0, loop, mini_app_url, show_loop))


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
                    "mini_app_url", "keyboard_message_id", "mix_loop", "edit_audio_duration"):
            context.user_data.pop(key, None)
        await query.edit_message_text("Mix cancelled.")
        return

    if data == "preview":
        token        = context.user_data.get("edit_token")
        start        = context.user_data.get("mix_start", 0)
        audio_file_id = context.user_data.get("edit_audio_file_id")
        session      = _audio_sessions.get(token) if token else None
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                if session and os.path.exists(session["path"]):
                    audio_path = session["path"]
                    preview_path = extract_audio_preview(audio_path, start, tmpdir)
                elif audio_file_id:
                    tg = await context.bot.get_file(audio_file_id)
                    audio_path = os.path.join(tmpdir, "audio")
                    await tg.download_to_drive(audio_path)
                    preview_path = extract_audio_preview(audio_path, start, tmpdir)
                else:
                    await query.answer("No audio found.", show_alert=True)
                    return
                m, s = divmod(int(start), 60)
                with open(preview_path, "rb") as f:
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

    # --- Handle state changes ---
    start     = context.user_data.get("mix_start", 0)
    volume    = context.user_data.get("mix_volume", 100)
    loop      = context.user_data.get("mix_loop", False)
    audio_dur = context.user_data.get("edit_audio_duration", float("inf"))
    video_dur = context.user_data.get("edit_video_duration", float("inf"))
    show_loop = audio_dur > 0 and audio_dur < video_dur

    if data.startswith("vol:"):
        context.user_data["mix_volume"] = int(data.split(":")[1])
        volume = context.user_data["mix_volume"]

    elif data == "start:sub1":
        context.user_data["mix_start"] = round(max(0.0, start - 1), 2)
        start = context.user_data["mix_start"]

    elif data == "start:add1":
        context.user_data["mix_start"] = round(start + 1, 2)
        start = context.user_data["mix_start"]

    elif data.startswith("start:goto:"):
        context.user_data["mix_start"] = float(data[len("start:goto:"):])
        start = context.user_data["mix_start"]

    elif data == "toggle_loop":
        context.user_data["mix_loop"] = not loop
        loop = context.user_data["mix_loop"]

    if data != "mix":
        mini_app_url = context.user_data.get("mini_app_url")
        await query.edit_message_reply_markup(build_keyboard(volume, start, loop, mini_app_url, show_loop))
        return

    # --- Do the mix ---
    video_file_id  = context.user_data.get("edit_video_file_id")
    audio_file_id  = context.user_data.get("edit_audio_file_id")
    token          = context.user_data.get("edit_token")
    volume         = context.user_data.get("mix_volume", 100)
    start          = context.user_data.get("mix_start", 0)
    loop           = context.user_data.get("mix_loop", False)

    if not video_file_id or not audio_file_id:
        await query.edit_message_text("Session expired. Reply to a video with /edit to start over.")
        return

    await query.edit_message_text("⏳ Mixing audio, please wait...")

    session = _audio_sessions.get(token) if token else None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            video_tg   = await context.bot.get_file(video_file_id)
            video_path = os.path.join(tmpdir, "video.mp4")
            await video_tg.download_to_drive(video_path)

            if session and os.path.exists(session["path"]):
                audio_path = session["path"]
            else:
                audio_tg   = await context.bot.get_file(audio_file_id)
                audio_path = os.path.join(tmpdir, "audio")
                await audio_tg.download_to_drive(audio_path)

            mixed_path = mix_audio_into_video(video_path, audio_path, volume, start, loop)

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
                        "edit_token", "mini_app_url", "keyboard_message_id",
                        "mix_loop", "edit_audio_duration"):
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
        await process_url(update, context, url)


async def process_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    status_msg = await update.message.reply_text(downloading_message(url))

    # Write per-user YouTube cookies to a temp file if available
    yt_cookies_path = None
    yt_cookies_tmpdir = None
    if is_youtube_url(url) and context.user_data.get("yt_cookies"):
        yt_cookies_tmpdir = tempfile.mkdtemp()
        yt_cookies_path = os.path.join(yt_cookies_tmpdir, "yt_cookies.txt")
        Path(yt_cookies_path).write_text(context.user_data["yt_cookies"])

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_template = os.path.join(tmpdir, "video.%(ext)s")
            try:
                video_path, info = download_video(url, output_template, yt_cookies_path)
            except yt_dlp.utils.UnsupportedError:
                await status_msg.edit_text("Unsupported URL or platform.")
                return
            except yt_dlp.utils.DownloadError as e:
                err_str = str(e)
                logger.error("Download error for %s: %s", url, e)
                if is_youtube_url(url) and "sign in" in err_str.lower():
                    await status_msg.edit_text(
                        "❌ YouTube requires login to download this video.\n\n"
                        "Use /setcookies to provide your YouTube cookies.\n"
                        "⚠️ Cookies are session-only and need to be re-set after each bot update."
                    )
                else:
                    await status_msg.edit_text(
                        f"Download failed: {err_str.split(chr(10))[0][:200]}"
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
    finally:
        if yt_cookies_tmpdir:
            shutil.rmtree(yt_cookies_tmpdir, ignore_errors=True)


# ---------------------------------------------------------------------------
# Stretch
# ---------------------------------------------------------------------------

STRETCH_RATIOS = {
    "9_16":  ("📱 9:16 Portrait",  9,  16),
    "16_9":  ("🖥 16:9 Landscape", 16,  9),
    "1_1":   ("⬛ 1:1 Square",      1,  1),
}


def stretch_video(video_path: str, ratio_key: str) -> str:
    _, w_r, h_r = STRETCH_RATIOS[ratio_key]
    output_path = video_path.replace(".mp4", f"_stretched_{ratio_key}.mp4")
    # Force exact ratio while preserving approximate pixel count
    # new_w * new_h ≈ orig_w * orig_h   and   new_w / new_h = w_r / h_r
    # → new_w = sqrt(orig_pixels * w_r / h_r), rounded to even
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
         "-show_entries", "stream=width,height",
         "-of", "csv=p=0", video_path],
        capture_output=True, text=True, check=True,
    )
    orig_w, orig_h = map(int, result.stdout.strip().split(","))
    orig_pixels = orig_w * orig_h
    new_w = int((orig_pixels * w_r / h_r) ** 0.5) // 2 * 2
    new_h = int((orig_pixels * h_r / w_r) ** 0.5) // 2 * 2
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path,
         "-vf", f"scale={new_w}:{new_h},setsar=1",
         "-c:v", "libx264", "-crf", "23", "-preset", "fast",
         "-c:a", "copy", output_path],
        check=True, capture_output=True,
    )
    return output_path


def build_stretch_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"stretch:{key}")]
        for key, (label, _, _) in STRETCH_RATIOS.items()
    ] + [[InlineKeyboardButton("❌ Cancel", callback_data="stretch:cancel")]])


async def handle_stretch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.video:
        await msg.reply_text("Reply to one of my videos with /stretch to resize it.")
        return
    video = msg.reply_to_message.video
    context.user_data["stretch_video_file_id"] = video.file_id
    context.user_data["stretch_video_width"]   = video.width
    context.user_data["stretch_video_height"]  = video.height
    context.user_data["stretch_video_duration"] = video.duration
    await msg.reply_text("↔️ Pick a format to stretch to:", reply_markup=build_stretch_keyboard())


async def handle_stretch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    ratio_key = query.data.split(":", 1)[1]

    if ratio_key == "cancel":
        for key in ("stretch_video_file_id", "stretch_video_width",
                    "stretch_video_height", "stretch_video_duration"):
            context.user_data.pop(key, None)
        await query.edit_message_text("Cancelled.")
        return

    video_file_id = context.user_data.get("stretch_video_file_id")
    if not video_file_id:
        await query.edit_message_text("Session expired. Reply to a video with /stretch.")
        return

    label, _, _ = STRETCH_RATIOS[ratio_key]
    await query.edit_message_text(f"⏳ Stretching to {label}…")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            video_tg   = await context.bot.get_file(video_file_id)
            video_path = os.path.join(tmpdir, "video.mp4")
            await video_tg.download_to_drive(video_path)
            out_path = stretch_video(video_path, ratio_key)
            size = os.path.getsize(out_path)
            if size > MAX_SIZE_BYTES:
                await query.edit_message_text(
                    f"Stretched video is too large ({size/1024/1024:.1f} MB)."
                )
                return
            await query.edit_message_text("📤 Sending…")
            with open(out_path, "rb") as f:
                await context.bot.send_video(
                    chat_id=query.message.chat_id,
                    video=f,
                    supports_streaming=True,
                    duration=context.user_data.get("stretch_video_duration"),
                    read_timeout=120,
                    write_timeout=120,
                )
            await query.delete_message()
    except subprocess.CalledProcessError as e:
        logger.error("stretch ffmpeg error: %s", e.stderr)
        await query.edit_message_text("❌ Failed to stretch video.")
    except Exception:
        logger.exception("stretch error")
        await query.edit_message_text("❌ An unexpected error occurred.")
    finally:
        for key in ("stretch_video_file_id", "stretch_video_width",
                    "stretch_video_height", "stretch_video_duration"):
            context.user_data.pop(key, None)


# ---------------------------------------------------------------------------
# YouTube cookies management
# ---------------------------------------------------------------------------

SETCOOKIES_TEXT = (
    "🍪 *How to give me your YouTube cookies:*\n\n"
    "1\\. Install the *Get cookies\\.txt LOCALLY* extension:\n"
    "   • [Chrome](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)\n"
    "   • [Firefox](https://addons.mozilla.org/firefox/addon/get-cookies-txt-locally/)\n\n"
    "2\\. Go to *youtube\\.com* while logged in\n\n"
    "3\\. Click the extension → *Export* → save as `cookies.txt`\n\n"
    "4\\. Send me that file here 👇\n\n"
    "⚠️ _Cookies are stored only for your session and are never shared\\. "
    "You'll need to re\\-run /setcookies whenever the bot gets updated or restarted\\._"
)


async def handle_set_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["waiting_for_yt_cookies"] = True
    await update.message.reply_text(SETCOOKIES_TEXT, parse_mode="MarkdownV2",
                                    disable_web_page_preview=True)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("waiting_for_yt_cookies"):
        return
    doc = update.message.document
    if not doc or not (doc.file_name or "").endswith(".txt"):
        await update.message.reply_text("Please send a .txt cookies file.")
        return
    tg_file = await context.bot.get_file(doc.file_id)
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        tmp_path = f.name
    try:
        await tg_file.download_to_drive(tmp_path)
        content = Path(tmp_path).read_text(errors="replace")
    finally:
        os.unlink(tmp_path)
    if "youtube.com" not in content and "youtu.be" not in content and "# Netscape" not in content:
        await update.message.reply_text(
            "⚠️ That doesn't look like a YouTube cookies file. "
            "Make sure you exported from youtube.com while logged in."
        )
        return
    context.user_data["yt_cookies"] = content
    context.user_data.pop("waiting_for_yt_cookies", None)
    await update.message.reply_text(
        "✅ YouTube cookies saved! Try sending a YouTube link now.\n\n"
        "⚠️ These are only stored for this session — you'll need to re-run /setcookies "
        "whenever the bot gets updated or restarted."
    )


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "📥 *Video Downloader Bot*\n\n"
    "*Send a link* from TikTok, Instagram, YouTube, or X/Twitter and I'll download it for you\\.\n\n"
    "━━━━━━━━━━━━━\n"
    "*Commands*\n\n"
    "/audio — Reply to one of my videos to add music to it\n"
    "/stretch — Reply to one of my videos to resize it \\(9:16, 16:9, 1:1\\)\n"
    "/setcookies — Provide your YouTube cookies for age\\-restricted or sign\\-in\\-required videos\n"
    "/settings — View and adjust your preferences\n"
    "/help — Show this message\n\n"
    "━━━━━━━━━━━━━\n"
    "*YouTube downloads*\n\n"
    "YouTube may require you to be signed in\\. If a download fails, run /setcookies "
    "and send your cookies\\.txt file \\(exported from your browser\\)\\.\n"
    "Cookies are stored only for your session and need to be re\\-set after each bot update\\."
)


async def handle_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="MarkdownV2",
                                    disable_web_page_preview=True)


# ---------------------------------------------------------------------------
# /settings
# ---------------------------------------------------------------------------

def build_settings_keyboard(user_data: dict) -> InlineKeyboardMarkup:
    vol = user_data.get("mix_volume", 100)
    vi  = VOLUME_STEPS.index(vol) if vol in VOLUME_STEPS else len(VOLUME_STEPS) - 1
    vol_label = "🔇 Replace audio" if vol == 100 else f"🔊 Mix at {vol}%"
    has_yt = bool(user_data.get("yt_cookies"))
    rows = [
        [InlineKeyboardButton("🎚 Default mix volume", callback_data="settings:noop")],
        [
            InlineKeyboardButton("◀", callback_data="settings:vol_down"),
            InlineKeyboardButton(vol_label, callback_data="settings:noop"),
            InlineKeyboardButton("▶", callback_data="settings:vol_up"),
        ],
        [InlineKeyboardButton(
            "🗑 Clear YouTube cookies" if has_yt else "🍪 No YouTube cookies set",
            callback_data="settings:clear_yt" if has_yt else "settings:noop",
        )],
        [InlineKeyboardButton("✅ Done", callback_data="settings:close")],
    ]
    return InlineKeyboardMarkup(rows)


async def handle_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "⚙️ *Settings*",
        parse_mode="Markdown",
        reply_markup=build_settings_keyboard(context.user_data),
    )


async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    action = query.data.split(":", 1)[1]

    if action == "noop":
        return

    if action == "close":
        await query.edit_message_text("⚙️ Settings saved.")
        return

    vol = context.user_data.get("mix_volume", 100)
    vi  = VOLUME_STEPS.index(vol) if vol in VOLUME_STEPS else len(VOLUME_STEPS) - 1

    if action == "vol_down":
        context.user_data["mix_volume"] = VOLUME_STEPS[max(0, vi - 1)]
    elif action == "vol_up":
        context.user_data["mix_volume"] = VOLUME_STEPS[min(len(VOLUME_STEPS) - 1, vi + 1)]
    elif action == "clear_yt":
        context.user_data.pop("yt_cookies", None)

    await query.edit_message_reply_markup(build_settings_keyboard(context.user_data))


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
    app.add_handler(CommandHandler("audio",      handle_audio_cmd))
    app.add_handler(CommandHandler("stretch",    handle_stretch))
    app.add_handler(CommandHandler("setcookies", handle_set_cookies))
    app.add_handler(CommandHandler("settings",   handle_settings))
    app.add_handler(CommandHandler("help",       handle_help))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, handle_audio))
    app.add_handler(MessageHandler(filters.Document.TXT, handle_document))
    app.add_handler(CallbackQueryHandler(handle_stretch_callback,  pattern=r"^stretch:"))
    app.add_handler(CallbackQueryHandler(handle_settings_callback, pattern=r"^settings:"))
    app.add_handler(CallbackQueryHandler(handle_mix_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot started. Polling...")
    app.run_polling()


if __name__ == "__main__":
    main()
