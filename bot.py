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

from aiohttp import web as aio_web
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    PicklePersistence,
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
    r"|twitter\.com|x\.com|t\.co"
    r"|facebook\.com|fb\.watch)"
    r"\S+",
    re.IGNORECASE,
)

VOLUME_STEPS = [10, 25, 50, 75, 100]  # 100 = replace original audio; <100 = mix on top
START_STEPS  = [0, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240]

# Text overlay options
# Fonts are relative to the repo root (bundled in fonts/)
_FONTS_DIR = Path(__file__).parent / "fonts"
TEXT_FONTS = [
    ("Classic",  str(_FONTS_DIR / "TikTokSans-SemiBold.ttf"), None),  # wght=600 — matches TikTok default
    ("Bold",     str(_FONTS_DIR / "TikTokSans-Bold.ttf"),     None),  # wght=700
    ("Heavy",    str(_FONTS_DIR / "TikTokSans-Black.ttf"),    None),  # wght=900
    ("Monospace", None, "DejaVu Sans Mono:Bold"),
    ("Serif",    None, "DejaVu Serif:Bold"),
]
TEXT_COLORS = [
    ("White",  "white"),
    ("Yellow", "yellow"),
    ("Pink",   "HotPink"),
    ("Black",  "black"),
]
TEXT_BORDER_COLORS = [
    ("Black",  "black@0.85"),
    ("White",  "white@0.85"),
    ("None",   "black@0.0"),
]
# Font size as fraction of video height — text is always wrapped to fit width
TEXT_SIZES = [
    ("Small",  0.05),
    ("Medium", 0.07),
    ("Large",  0.09),
]

def _find_font(fc_query: str) -> str | None:
    """Return the font file path for a fontconfig query, or None if unavailable."""
    try:
        r = subprocess.run(
            ["fc-match", "--format=%{file}", fc_query],
            capture_output=True, text=True, timeout=5,
        )
        path = r.stdout.strip()
        return path if path else None
    except Exception:
        return None

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
        return "Downloadin dis fi yuh, bredren... (Instagram videos tek likkle longer, no worry yaself) 📸"
    if is_youtube_url(url):
        return "Downloadin dis fi yuh, mon... (YouTube quality can vary — nuh expect pure perfection) 📺"
    return "Downloadin dis fi yuh, hol' tight... ⬇️"


def build_ydl_opts(output_path: str, url: str) -> dict:
    opts = {
        "outtmpl": output_path,
        # No codec restriction — grab the absolute best quality available
        "format": "bestvideo+bestaudio/best",
        "merge_output_format": "mp4",
        "quiet": False,
        "no_warnings": False,
    }
    if "tiktok.com" in url:
        opts["extractor_args"] = {"tiktok": {"download_without_watermark": True}}
    if is_instagram_url(url):
        # Instagram sessions are tied to the browser they were exported from;
        # a desktop-Chrome UA keeps the cookie session valid and avoids the
        # instant login-wall served to unknown clients.
        opts["http_headers"] = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            )
        }
        opts["extractor_retries"] = 3
        if COOKIES_FILE.exists():
            opts["cookiefile"] = str(COOKIES_FILE)
    return opts


def get_video_codec(path: str) -> str:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def reencode_h264(input_path: str) -> str:
    output_path = input_path.replace(".mp4", "_h264.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-i", input_path,
         "-vcodec", "libx264", "-crf", "23", "-preset", "fast",
         "-threads", "2", "-acodec", "aac", "-movflags", "+faststart", output_path],
        check=True, capture_output=True,
    )
    return output_path


def ensure_telegram_compatible(path: str) -> str:
    """Re-encode to H264/AAC if the video codec isn't already H264."""
    codec = get_video_codec(path)
    if codec and codec != "h264":
        logger.info("Re-encoding %s (codec: %s) to H264 for Telegram", path, codec)
        return reencode_h264(path)
    return path


def _wrap_text(text: str, max_chars: int = 22) -> str:
    words = text.split()
    lines, current = [], ""
    for word in words:
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= max_chars:
            current += " " + word
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return "\n".join(lines)


def overlay_text(video_path: str, text: str, font_idx: int, color: str, size_idx: int = 1, border_idx: int = 0) -> str:
    """Burn TikTok-style centred text onto a video, auto-wrapping to fit width."""
    out_path = video_path.replace(".mp4", "_text.mp4")

    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "csv=p=0", video_path],
        capture_output=True, text=True, check=True,
    )
    vid_w, vid_h = map(int, r.stdout.strip().split(","))
    _, size_factor = TEXT_SIZES[size_idx]
    font_px = vid_h * size_factor
    max_chars = max(8, int((vid_w * 0.9) / (font_px * 0.58)))

    lines = _wrap_text(text, max_chars).split("\n")

    _, bundled_path, fc_query = TEXT_FONTS[font_idx]
    if bundled_path and Path(bundled_path).exists():
        font_path = bundled_path
    elif fc_query:
        font_path = _find_font(fc_query)
    else:
        font_path = None

    _, border_color = TEXT_BORDER_COLORS[border_idx]

    # Render each line as its own drawtext so x=(w-text_w)/2 centers it individually
    line_h   = font_px * 1.18   # approximate line height for TikTok Sans
    spacing  = font_px * 0.12   # gap between lines
    total_h  = len(lines) * line_h + (len(lines) - 1) * spacing
    start_y  = (vid_h - total_h) / 2

    filters = []
    line_files = []
    for i, line in enumerate(lines):
        lf = video_path.replace(".mp4", f"_line{i}.txt")
        Path(lf).write_text(line, encoding="utf-8")
        line_files.append(lf)
        y = start_y + i * (line_h + spacing)
        f = (
            f"drawtext=textfile='{lf}'"
            f":fontsize={font_px:.1f}"
            f":fontcolor={color}"
            f":borderw={max(1, round(font_px * 0.04))}:bordercolor={border_color}"
            f":x=(w-text_w)/2:y={y:.1f}"
        )
        if font_path:
            f += f":fontfile='{font_path}'"
        filters.append(f)

    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path,
         "-vf", ",".join(filters),
         "-c:v", "libx264", "-crf", "23", "-preset", "fast",
         "-threads", "2", "-c:a", "copy", out_path],
        check=True, capture_output=True,
    )
    for lf in line_files:
        Path(lf).unlink(missing_ok=True)
    return out_path



def _download_youtube_pytubefix(url: str, output_dir: str, yt_cookies_path: str | None = None) -> tuple[str, dict]:
    from pytubefix import YouTube

    yt = YouTube(url)
    # Try progressive (pre-muxed) first
    stream = (
        yt.streams.filter(progressive=True, file_extension="mp4")
        .order_by("resolution").desc().first()
    )
    if not stream:
        # Fall back to highest-res video-only + best audio, merge with ffmpeg
        video_stream = yt.streams.filter(only_video=True).order_by("resolution").desc().first()
        audio_stream = yt.streams.filter(only_audio=True).order_by("abr").desc().first()
        if not video_stream:
            raise RuntimeError("No streams available via pytubefix")
        vpath = video_stream.download(output_path=output_dir, filename="yt_video")
        apath = audio_stream.download(output_path=output_dir, filename="yt_audio") if audio_stream else None
        merged = os.path.join(output_dir, "yt_merged.mp4")
        if apath:
            subprocess.run(
                ["ffmpeg", "-y", "-i", vpath, "-i", apath,
                 "-c:v", "copy", "-c:a", "aac", "-shortest", merged],
                check=True, capture_output=True,
            )
        else:
            merged = vpath
        path = merged
    else:
        path = stream.download(output_path=output_dir, filename="yt_video.mp4")

    path = reencode_h264(path)
    info = {"title": yt.title, "duration": yt.length, "width": None, "height": None}
    return path, info


def download_video(url: str, output_path: str, yt_cookies_path: str | None = None) -> tuple[str, dict]:
    import yt_dlp
    if is_youtube_url(url):
        output_dir = str(Path(output_path).parent)
        try:
            return _download_youtube_pytubefix(url, output_dir, yt_cookies_path)
        except Exception as e:
            logger.warning("pytubefix failed: %s: %s", type(e).__name__, e)
            raise

    opts = build_ydl_opts(output_path, url)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        p = Path(ydl.prepare_filename(info))
        if not p.exists():
            p = p.with_suffix(".mp4")
        p = Path(ensure_telegram_compatible(str(p)))
        return str(p), info


def compress_to_limit(video_path: str, tmpdir: str, target_bytes: int = 47 * 1024 * 1024) -> str:
    """Two-pass encode targeting just under Telegram's 50 MB limit.

    The bitrate is computed from the video's duration so quality is reduced
    only as much as needed to fit — no more.
    """
    out = os.path.join(tmpdir, "compressed.mp4")
    dur = get_audio_duration(video_path)
    if dur <= 0:
        dur = 60.0
    audio_kbps = 128
    total_kbps = int(target_bytes * 8 / dur / 1000)
    video_kbps = max(100, total_kbps - audio_kbps)
    passlog = os.path.join(tmpdir, "compress_passlog")
    base = [
        "ffmpeg", "-y", "-i", video_path,
        "-c:v", "libx264", "-preset", "fast", "-b:v", f"{video_kbps}k",
        "-passlogfile", passlog, "-threads", "2",
    ]
    subprocess.run(base + ["-pass", "1", "-an", "-f", "mp4", "/dev/null"],
                   check=True, capture_output=True)
    subprocess.run(base + ["-pass", "2", "-c:a", "aac", "-b:a", f"{audio_kbps}k",
                   "-movflags", "+faststart", out],
                   check=True, capture_output=True)
    return out


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
         "-t", "15", "-vn", "-c:a", "libmp3lame", "-q:a", "4", "-threads", "1", preview_path],
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
            InlineKeyboardButton(f"◄ {vd}%", callback_data=f"vol:{vd}"),
            InlineKeyboardButton(vol_label, callback_data="noop"),
            InlineKeyboardButton(f"► {vu}%", callback_data=f"vol:{vu}"),
        ],
        [
            InlineKeyboardButton(f"⏮ {ps}s", callback_data=f"start:goto:{ps}"),
            InlineKeyboardButton("−1s", callback_data="start:sub1"),
            InlineKeyboardButton(f"► {fmt_start(start)}", callback_data="noop"),
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

    # Restore Instagram cookies from persistence if env var not set
    if not _COOKIES_B64:
        ig = application.bot_data.get("ig_cookies")
        if ig:
            COOKIES_FILE.write_text(ig)
            logger.info("Restored cookies.txt from bot_data")

    await application.bot.set_my_commands([
        ("audio",   "Add music to a video — reply to a video"),
        ("text",    "Add text to a video — reply to a video"),
        ("stretch", "Resize a video — reply to a video"),
        ("crop",    "Remove black borders — reply to a video"),
        ("gif",     "Convert a video or sticker to GIF — reply to a video or sticker"),
        ("sticker", "Convert a video to a Telegram sticker — reply to a video"),
        ("mp4",     "Convert a GIF or sticker to MP4 — reply to a GIF or sticker"),
        ("getaudio","Extract audio — reply to a video"),
        ("settings","View and adjust your preferences"),
        ("help",    "Show all commands and info"),
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
    if not _chat_is_active(context, update.effective_chat): return
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.video:
        await msg.reply_text("Reply to one of mi videos wid /audio fi add music to it, bredren 🎵")
        return
    video = msg.reply_to_message.video
    context.user_data["edit_video_file_id"]  = video.file_id
    context.user_data["edit_video_duration"] = video.duration
    context.user_data["edit_video_width"]    = video.width
    context.user_data["edit_video_height"]   = video.height
    context.user_data["edit_state"]          = "waiting_for_audio"
    context.user_data.setdefault("mix_volume", 100)
    context.user_data["mix_start"] = 0
    await msg.reply_text("🎵 Forward mi an audio file fi mix inna dis video, bredren.")


async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _chat_is_active(context, update.effective_chat): return
    if context.user_data.get("edit_state") != "waiting_for_audio":
        await update.message.reply_text(
            "Reply to one of mi videos wid /audio first, den forward an audio file, star."
        )
        return

    audio = update.message.audio or update.message.voice
    if not audio:
        await update.message.reply_text("Send mi an audio file nuh, mon.")
        return

    status = await update.message.reply_text("⏳ Loadin di audio, hol' tight bredren...")

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
        await status.edit_text("❌ Couldn't download di audio, mon. Try again nuh.")
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
        "🎵 Audio ready, bredren! Use di buttons fi set di start position, or open di Precise Selector:",
        reply_markup=build_keyboard(volume, 0, loop, None, show_loop),  # placeholder — updated below
    )

    # Build Mini App URL now that we have the message_id.
    # web_app InlineKeyboardButton is only allowed in private chats (Telegram API restriction).
    mini_app_url = None
    if MINI_APP_HOST and update.effective_chat.type == "private":
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
        await query.edit_message_text("Mix cancelled, bredren.")
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
                    await query.answer("No audio found, mon.", show_alert=True)
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
            await context.bot.send_message(query.message.chat_id, "❌ Couldn't generate di preview, mon.")
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
        await query.edit_message_text("Session done expire, bredren. Reply to a video wid /audio fi start again.")
        return

    await query.edit_message_text("⏳ Mixin di audio, hol' tight bredren...")

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
                    f"Di mixed video too big ({size / 1024 / 1024:.1f} MB), mon. "
                    "Telegram only allows 50 MB uploads."
                )
                return

            await query.edit_message_text("📤 Sendin it now...")
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
        await query.edit_message_text("❌ Couldn't mix di audio, mon. Try again nuh.")
    except Exception:
        logger.exception("Unexpected error during mix")
        await query.edit_message_text("❌ Sumting unexpected happen, bredren. Try again.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _chat_is_active(context, update.effective_chat): return
    text = update.message.text or ""

    if context.user_data.get("waiting_for_stretch_ratio"):
        context.user_data.pop("waiting_for_stretch_ratio", None)
        import re as _re
        m = _re.match(r"^\s*(\d+)\s*[:/]\s*(\d+)\s*$", text)
        if not m:
            await update.message.reply_text("Invalid format, mon. Send something like 21:9 or 4:3 nuh.")
            return
        w_r, h_r = int(m.group(1)), int(m.group(2))
        if w_r <= 0 or h_r <= 0:
            await update.message.reply_text("Both numbers must be greater than 0, bredren.")
            return
        video_file_id = context.user_data.get("stretch_video_file_id")
        if not video_file_id:
            await update.message.reply_text("Session expire, mon. Reply to a video wid /stretch again.")
            return
        label = f"{w_r}:{h_r}"
        status_msg = await update.message.reply_text(f"⏳ Stretchin to {label}… hol' tight")
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                video_tg   = await context.bot.get_file(video_file_id)
                video_path = os.path.join(tmpdir, "video.mp4")
                await video_tg.download_to_drive(video_path)
                out_path = stretch_video(video_path, w_r, h_r, label)
                size = os.path.getsize(out_path)
                if size > MAX_SIZE_BYTES:
                    await status_msg.edit_text(f"Di stretched video too big ({size/1024/1024:.1f} MB), mon.")
                    return
                await status_msg.edit_text("📤 Sendin it now…")
                with open(out_path, "rb") as f:
                    await update.message.reply_video(
                        video=f,
                        supports_streaming=True,
                        duration=context.user_data.get("stretch_video_duration"),
                        read_timeout=120,
                        write_timeout=120,
                    )
                await status_msg.delete()
        except subprocess.CalledProcessError as e:
            logger.error("stretch ffmpeg error: %s", e.stderr)
            await status_msg.edit_text("❌ Couldn't stretch di video, mon.")
        except Exception:
            logger.exception("custom stretch error")
            await status_msg.edit_text("❌ Sumting unexpected happen, bredren.")
        finally:
            for key in ("stretch_video_file_id", "stretch_video_width",
                        "stretch_video_height", "stretch_video_duration"):
                context.user_data.pop(key, None)
        return

    urls = extract_urls(text)
    if not urls:
        await update.message.reply_text(
            "Send mi a TikTok, Instagram, YouTube, or X/Twitter link nuh, bredren 👌"
        )
        return
    for url in urls:
        await process_url(update, context, url)


async def process_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    status_msg = await update.message.reply_text(downloading_message(url))

    yt_cookies_path = None
    yt_cookies_tmpdir = None
    if is_youtube_url(url) and context.user_data.get("yt_cookies"):
        yt_cookies_tmpdir = tempfile.mkdtemp()
        yt_cookies_path = os.path.join(yt_cookies_tmpdir, "yt_cookies.txt")
        Path(yt_cookies_path).write_text(context.user_data["yt_cookies"])

    try:
        import yt_dlp
        with tempfile.TemporaryDirectory() as tmpdir:
            output_template = os.path.join(tmpdir, "video.%(ext)s")
            try:
                video_path, info = download_video(url, output_template, yt_cookies_path)
            except yt_dlp.utils.UnsupportedError:
                await status_msg.edit_text("Dis URL nuh supported, mon.")
                return
            except Exception as e:
                err_str = str(e)
                logger.error("Download error for %s: %s: %s", url, type(e).__name__, e)
                err_lower = err_str.lower()
                if is_instagram_url(url) and any(k in err_lower for k in ("login required", "rate-limit", "not available", "cookies", "checkpoint")):
                    if COOKIES_FILE.exists():
                        await status_msg.edit_text(
                            "❌ Instagram reject di download even though cookies set, mon — "
                            "dem probably expire or di session get logged out.\n"
                            "Re-export fresh cookies (log inna instagram.com inna yuh browser first) "
                            "an upload dem again wid /setigcookies, bredren."
                        )
                    else:
                        await status_msg.edit_text(
                            "❌ Instagram a block dis download, mon — login required.\n"
                            "Use /setigcookies fi upload yuh Instagram cookies and fix dis."
                        )
                else:
                    await status_msg.edit_text(
                        f"Download failed: {type(e).__name__}: {err_str.split(chr(10))[0][:180]}"
                    )
                return

            size = os.path.getsize(video_path)
            if size > MAX_SIZE_BYTES:
                token = uuid.uuid4().hex[:8]
                tokens: dict = context.chat_data.setdefault("compress_urls", {})
                tokens[token] = url
                if len(tokens) > 20:
                    del tokens[next(iter(tokens))]
                await status_msg.edit_text(
                    f"Di video too big ({size / 1024 / 1024:.1f} MB), mon — "
                    "Telegram only allows 50 MB uploads.\n"
                    "Yuh waan mi compress it fi fit? Quality only get reduce as "
                    "much as needed fi get unda di limit, bredren.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🗜 Yes, compress it", callback_data=f"compress:yes:{token}"),
                        InlineKeyboardButton("❌ No", callback_data=f"compress:no:{token}"),
                    ]]),
                )
                return

            await status_msg.edit_text("Sendin di video now...")
            with open(video_path, "rb") as f:
                sent = await update.message.reply_video(
                    video=f,
                    supports_streaming=True,
                    width=info.get("width"),
                    height=info.get("height"),
                    duration=int(info.get("duration") or 0) or None,
                    read_timeout=120,
                    write_timeout=120,
                )
            # Remember URL so /getaudio can re-download audio-only (bypasses 20 MB limit)
            urls: dict = context.chat_data.setdefault("video_urls", {})
            urls[sent.message_id] = url
            if len(urls) > 100:
                del urls[min(urls)]
            await status_msg.delete()

    except Exception:
        logger.exception("Unexpected error for %s", url)
        await status_msg.edit_text("Sumting unexpected happen, bredren. Try again nuh.")
    finally:
        if yt_cookies_tmpdir:
            shutil.rmtree(yt_cookies_tmpdir, ignore_errors=True)


async def handle_compress_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, action, token = query.data.split(":", 2)
    except ValueError:
        return
    url = context.chat_data.get("compress_urls", {}).pop(token, None)

    if action == "no":
        await query.edit_message_text("Aright, mi a skip dis one, bredren.")
        return

    if not url:
        await query.edit_message_text("Session expire, mon. Send di link again nuh.")
        return

    await query.edit_message_text("⏳ Re-downloadin di video, hol' tight bredren…")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_template = os.path.join(tmpdir, "video.%(ext)s")
            video_path, info = download_video(url, output_template)
            await query.edit_message_text(
                "🗜 Compressin it fi fit unda 50 MB… dis can tek a few minutes, mon."
            )
            out_path = compress_to_limit(video_path, tmpdir)
            size = os.path.getsize(out_path)
            if size > MAX_SIZE_BYTES:
                await query.edit_message_text(
                    f"❌ Still too big afta compression ({size/1024/1024:.1f} MB), mon. "
                    "Dis video cyaan fit unda Telegram limit."
                )
                return
            await query.edit_message_text("📤 Sendin it now…")
            with open(out_path, "rb") as f:
                sent = await context.bot.send_video(
                    chat_id=query.message.chat_id,
                    video=f,
                    supports_streaming=True,
                    duration=int(info.get("duration") or 0) or None,
                    read_timeout=300,
                    write_timeout=300,
                )
            # Remember URL so /getaudio can re-download audio-only
            urls: dict = context.chat_data.setdefault("video_urls", {})
            urls[sent.message_id] = url
            if len(urls) > 100:
                del urls[min(urls)]
            await query.delete_message()
    except subprocess.CalledProcessError as e:
        logger.error("compress ffmpeg error: %s", e.stderr)
        await query.edit_message_text("❌ Couldn't compress di video, mon.")
    except Exception:
        logger.exception("compress error")
        await query.edit_message_text("❌ Sumting unexpected happen, bredren.")


# ---------------------------------------------------------------------------
# Stretch
# ---------------------------------------------------------------------------

STRETCH_RATIOS = {
    "9_16":  ("📱 9:16 Portrait",  9,  16),
    "16_9":  ("🖥 16:9 Landscape", 16,  9),
    "1_1":   ("⬛ 1:1 Square",      1,  1),
}


def stretch_video(video_path: str, w_r: int, h_r: int, label: str) -> str:
    output_path = video_path.replace(".mp4", f"_stretched.mp4")
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
         "-threads", "2", "-c:a", "copy", output_path],
        check=True, capture_output=True,
    )
    return output_path


def build_stretch_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(label, callback_data=f"stretch:{key}")]
        for key, (label, _, _) in STRETCH_RATIOS.items()
    ] + [
        [InlineKeyboardButton("✏️ Custom ratio", callback_data="stretch:custom")],
        [InlineKeyboardButton("❌ Cancel", callback_data="stretch:cancel")],
    ])


async def handle_stretch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _chat_is_active(context, update.effective_chat): return
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.video:
        await msg.reply_text("Reply to one of mi videos wid /stretch fi resize it, bredren.")
        return
    video = msg.reply_to_message.video
    context.user_data["stretch_video_file_id"] = video.file_id
    context.user_data["stretch_video_width"]   = video.width
    context.user_data["stretch_video_height"]  = video.height
    context.user_data["stretch_video_duration"] = video.duration
    await msg.reply_text("↔️ Pick a format fi stretch to, bredren:", reply_markup=build_stretch_keyboard())


async def handle_stretch_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    ratio_key = query.data.split(":", 1)[1]

    if ratio_key == "cancel":
        for key in ("stretch_video_file_id", "stretch_video_width",
                    "stretch_video_height", "stretch_video_duration"):
            context.user_data.pop(key, None)
        await query.edit_message_text("Cancelled, bredren.")
        return

    if ratio_key == "custom":
        context.user_data["waiting_for_stretch_ratio"] = True
        await query.edit_message_text("Send yuh ratio (e.g. 21:9 or 4:3), mon:")
        return

    video_file_id = context.user_data.get("stretch_video_file_id")
    if not video_file_id:
        await query.edit_message_text("Session expire, bredren. Reply to a video wid /stretch.")
        return

    label, w_r, h_r = STRETCH_RATIOS[ratio_key]
    await query.edit_message_text(f"⏳ Stretchin to {label}… hol' tight")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            video_tg   = await context.bot.get_file(video_file_id)
            video_path = os.path.join(tmpdir, "video.mp4")
            await video_tg.download_to_drive(video_path)
            out_path = stretch_video(video_path, w_r, h_r, label)
            size = os.path.getsize(out_path)
            if size > MAX_SIZE_BYTES:
                await query.edit_message_text(
                    f"Di stretched video too big ({size/1024/1024:.1f} MB), mon."
                )
                return
            await query.edit_message_text("📤 Sendin it now…")
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
        await query.edit_message_text("❌ Couldn't stretch di video, mon.")
    except Exception:
        logger.exception("stretch error")
        await query.edit_message_text("❌ Sumting unexpected happen, bredren.")
    finally:
        for key in ("stretch_video_file_id", "stretch_video_width",
                    "stretch_video_height", "stretch_video_duration"):
            context.user_data.pop(key, None)


# ---------------------------------------------------------------------------
# /setigcookies — Instagram cookies management
# ---------------------------------------------------------------------------

SETIGCOOKIES_TEXT = (
    "🍪 *How to give mi yuh Instagram cookies, bredren:*\n\n"
    "1\\. Install di *Get cookies\\.txt LOCALLY* extension:\n"
    "   • [Chrome](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)\n"
    "   • [Firefox](https://addons.mozilla.org/firefox/addon/get-cookies-txt-locally/)\n\n"
    "2\\. Go to *instagram\\.com* while logged in, mon\n\n"
    "3\\. Click di extension → *Export* → save as `cookies.txt`\n\n"
    "4\\. Send mi dat file right here 👇\n\n"
    "⚠️ _Cookies stored persistent and survive restarts, bredren\\. "
    "Update dem if Instagram start blocking again\\._"
)


async def handle_set_ig_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["waiting_for_ig_cookies"] = True
    await update.message.reply_text(SETIGCOOKIES_TEXT, parse_mode="MarkdownV2",
                                    disable_web_page_preview=True)


# /setcookies — YouTube cookies management
# ---------------------------------------------------------------------------

SETCOOKIES_TEXT = (
    "🍪 *How to give mi yuh YouTube cookies, bredren:*\n\n"
    "1\\. Install di *Get cookies\\.txt LOCALLY* extension:\n"
    "   • [Chrome](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)\n"
    "   • [Firefox](https://addons.mozilla.org/firefox/addon/get-cookies-txt-locally/)\n\n"
    "2\\. Go to *youtube\\.com* while logged in, mon\n\n"
    "3\\. Click di extension → *Export* → save as `cookies.txt`\n\n"
    "4\\. Send mi dat file right here 👇\n\n"
    "⚠️ _Cookies only stored fi yuh session and nuh shared wid nobody\\. "
    "Yuh haffi re\\-run /setcookies whenever di bot restart, bredren\\._"
)


async def handle_set_cookies(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["waiting_for_yt_cookies"] = True
    await update.message.reply_text(SETCOOKIES_TEXT, parse_mode="MarkdownV2",
                                    disable_web_page_preview=True)


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    waiting_yt = context.user_data.get("waiting_for_yt_cookies")
    waiting_ig = context.user_data.get("waiting_for_ig_cookies")
    if not waiting_yt and not waiting_ig:
        return
    doc = update.message.document
    if not doc or not (doc.file_name or "").endswith(".txt"):
        await update.message.reply_text("Send mi a .txt cookies file nuh, mon.")
        return
    tg_file = await context.bot.get_file(doc.file_id)
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        tmp_path = f.name
    try:
        await tg_file.download_to_drive(tmp_path)
        content = Path(tmp_path).read_text(errors="replace")
    finally:
        os.unlink(tmp_path)

    if waiting_ig:
        if "instagram.com" not in content and "# Netscape" not in content:
            await update.message.reply_text(
                "⚠️ Dat nuh look like Instagram cookies, mon. "
                "Make sure yuh exported from instagram.com while logged in."
            )
            return
        context.bot_data["ig_cookies"] = content
        COOKIES_FILE.write_text(content)
        context.user_data.pop("waiting_for_ig_cookies", None)
        await update.message.reply_text(
            "✅ Instagram cookies saved, bredren! Stored persistent and survive restarts.\n"
            "Try sendin an Instagram link now."
        )
    else:
        if "youtube.com" not in content and "youtu.be" not in content and "# Netscape" not in content:
            await update.message.reply_text(
                "⚠️ Dat nuh look like YouTube cookies, mon. "
                "Make sure yuh exported from youtube.com while logged in."
            )
            return
        context.user_data["yt_cookies"] = content
        context.user_data.pop("waiting_for_yt_cookies", None)
        await update.message.reply_text(
            "✅ YouTube cookies saved, bredren! Try sendin a YouTube link now.\n\n"
            "⚠️ Dem only stored fi dis session — yuh haffi re-run /setcookies whenever di bot restart."
        )


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "📥 *Video Downloader Bot*\n\n"
    "*Send a link* from TikTok, Instagram, YouTube, or X/Twitter and mi go download it fi yuh, bredren\\.\n\n"
    "⚠️ _YouTube downloads work but quality might nuh be di greatest due to server limitations\\._\n\n"
    "━━━━━━━━━━━━━\n"
    "*Commands*\n\n"
    "/audio — Reply to one of mi videos fi add music to it\n"
    "/text — Reply to one of mi videos fi add text to it\n"
    "/stretch — Reply to one of mi videos fi resize it \\(9:16, 16:9, 1:1\\)\n"
    "/crop — Reply to one of mi videos fi remove black borders\n"
    "/gif — Reply to one of mi videos or a video sticker fi convert it to a GIF\n"
    "/sticker — Reply to one of mi videos fi convert it to a Telegram sticker\n"
    "/mp4 — Reply to a GIF or video sticker fi convert it to a proper video\n"
    "/getaudio — Reply to one of mi videos fi extract di audio\n"
    "/setcookies — Give mi yuh YouTube cookies fi better access\n"
    "/settings — View and adjust yuh preferences\n"
    "/help — Show dis message"
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

    fi          = user_data.get("text_font",   0)
    ci          = user_data.get("text_color",  0)
    si          = user_data.get("text_size",   1)
    bi          = user_data.get("text_border", 0)
    font_label   = TEXT_FONTS[fi][0]
    color_label  = TEXT_COLORS[ci][0]
    size_label   = TEXT_SIZES[si][0]
    border_label = TEXT_BORDER_COLORS[bi][0]

    rows = [
        [InlineKeyboardButton("🏚 Default mix volume", callback_data="settings:noop")],
        [
            InlineKeyboardButton("◄", callback_data="settings:vol_down"),
            InlineKeyboardButton(vol_label, callback_data="settings:noop"),
            InlineKeyboardButton("►", callback_data="settings:vol_up"),
        ],
        [InlineKeyboardButton("🔤 Text font", callback_data="settings:noop")],
        [
            InlineKeyboardButton("◄", callback_data="settings:font_prev"),
            InlineKeyboardButton(font_label, callback_data="settings:noop"),
            InlineKeyboardButton("►", callback_data="settings:font_next"),
        ],
        [InlineKeyboardButton("🎨 Text color", callback_data="settings:noop")],
        [
            InlineKeyboardButton("◄", callback_data="settings:color_prev"),
            InlineKeyboardButton(color_label, callback_data="settings:noop"),
            InlineKeyboardButton("►", callback_data="settings:color_next"),
        ],
        [InlineKeyboardButton("🖊 Text border", callback_data="settings:noop")],
        [
            InlineKeyboardButton("◄", callback_data="settings:border_prev"),
            InlineKeyboardButton(border_label, callback_data="settings:noop"),
            InlineKeyboardButton("►", callback_data="settings:border_next"),
        ],
        [InlineKeyboardButton("🕡 Text size", callback_data="settings:noop")],
        [
            InlineKeyboardButton("◄", callback_data="settings:size_prev"),
            InlineKeyboardButton(size_label, callback_data="settings:noop"),
            InlineKeyboardButton("►", callback_data="settings:size_next"),
        ],
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
        await query.edit_message_text("⚙️ Settings saved, bredren.")
        return

    vol = context.user_data.get("mix_volume", 100)
    vi  = VOLUME_STEPS.index(vol) if vol in VOLUME_STEPS else len(VOLUME_STEPS) - 1

    if action == "vol_down":
        context.user_data["mix_volume"] = VOLUME_STEPS[max(0, vi - 1)]
    elif action == "vol_up":
        context.user_data["mix_volume"] = VOLUME_STEPS[min(len(VOLUME_STEPS) - 1, vi + 1)]

    fi = context.user_data.get("text_font", 0)
    ci = context.user_data.get("text_color", 0)
    si = context.user_data.get("text_size",   1)
    bi = context.user_data.get("text_border", 0)
    if action == "font_prev":
        context.user_data["text_font"] = (fi - 1) % len(TEXT_FONTS)
    elif action == "font_next":
        context.user_data["text_font"] = (fi + 1) % len(TEXT_FONTS)
    elif action == "color_prev":
        context.user_data["text_color"] = (ci - 1) % len(TEXT_COLORS)
    elif action == "color_next":
        context.user_data["text_color"] = (ci + 1) % len(TEXT_COLORS)
    elif action == "border_prev":
        context.user_data["text_border"] = (bi - 1) % len(TEXT_BORDER_COLORS)
    elif action == "border_next":
        context.user_data["text_border"] = (bi + 1) % len(TEXT_BORDER_COLORS)
    elif action == "size_prev":
        context.user_data["text_size"] = max(0, si - 1)
    elif action == "size_next":
        context.user_data["text_size"] = min(len(TEXT_SIZES) - 1, si + 1)
    await query.edit_message_reply_markup(build_settings_keyboard(context.user_data))


async def handle_text_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _chat_is_active(context, update.effective_chat): return
    msg = update.message
    text = " ".join(context.args or []).strip()
    if not text:
        await msg.reply_text("Usage: reply to a video wid /text yuh message here, bredren")
        return
    if not msg.reply_to_message or not msg.reply_to_message.video:
        await msg.reply_text("Reply to one of mi videos wid /text fi add text to it, mon.")
        return
    video    = msg.reply_to_message.video
    font_idx   = context.user_data.get("text_font",   0)
    col_idx    = context.user_data.get("text_color",  0)
    size_idx   = context.user_data.get("text_size",   1)
    border_idx = context.user_data.get("text_border", 0)
    _, color = TEXT_COLORS[col_idx]

    status_msg = await msg.reply_text("Addin di text… hol' tight")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tg_file    = await context.bot.get_file(video.file_id)
            video_path = os.path.join(tmpdir, "video.mp4")
            await tg_file.download_to_drive(video_path)
            out_path = overlay_text(video_path, text, font_idx, color, size_idx, border_idx)
            size = os.path.getsize(out_path)
            if size > MAX_SIZE_BYTES:
                await status_msg.edit_text(f"Di result too big ({size/1024/1024:.1f} MB), mon.")
                return
            await status_msg.edit_text("📤 Sendin it now…")
            with open(out_path, "rb") as f:
                await msg.reply_video(
                    video=f,
                    supports_streaming=True,
                    width=video.width,
                    height=video.height,
                    duration=video.duration,
                    read_timeout=120,
                    write_timeout=120,
                )
            await status_msg.delete()
    except subprocess.CalledProcessError as e:
        logger.error("text overlay ffmpeg error: %s", e.stderr)
        await status_msg.edit_text("❌ Couldn't add di text, mon.")
    except Exception:
        logger.exception("text overlay error")
        await status_msg.edit_text("❌ Sumting unexpected happen, bredren.")


async def handle_crop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _chat_is_active(context, update.effective_chat): return
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.video:
        await msg.reply_text("Reply to one of mi videos wid /crop fi remove black borders, bredren.")
        return
    video = msg.reply_to_message.video
    status_msg = await msg.reply_text("Detectin di black borders… hol' tight")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tg_file    = await context.bot.get_file(video.file_id)
            video_path = os.path.join(tmpdir, "video.mp4")
            await tg_file.download_to_drive(video_path)

            # Run cropdetect over the whole video to find the tightest crop
            r = subprocess.run(
                ["ffmpeg", "-i", video_path, "-vf", "cropdetect=limit=16:round=2:reset=0",
                 "-f", "null", "-"],
                capture_output=True, text=True,
            )
            crops = [l for l in (r.stdout + r.stderr).splitlines() if "crop=" in l]
            if not crops:
                await status_msg.edit_text("No black borders detected, bredren.")
                return
            # Take the last (most stable) crop value
            crop_val = crops[-1].split("crop=")[-1].split()[0]
            cw, ch, cx, cy = map(int, crop_val.split(":"))

            # If crop would remove less than 1% of pixels, skip
            orig_px = video.width * video.height
            if cw * ch > orig_px * 0.99:
                await status_msg.edit_text("No significant black borders found, mon.")
                return

            await status_msg.edit_text("Croppin di video…")
            out_path = os.path.join(tmpdir, "cropped.mp4")
            subprocess.run(
                ["ffmpeg", "-y", "-i", video_path,
                 "-vf", f"crop={cw}:{ch}:{cx}:{cy}",
                 "-c:v", "libx264", "-crf", "23", "-preset", "fast",
                 "-threads", "2", "-c:a", "copy", out_path],
                check=True, capture_output=True,
            )
            size = os.path.getsize(out_path)
            if size > MAX_SIZE_BYTES:
                await status_msg.edit_text(f"Di cropped video too big ({size/1024/1024:.1f} MB), mon.")
                return
            await status_msg.edit_text("📤 Sendin it now…")
            with open(out_path, "rb") as f:
                await msg.reply_video(
                    video=f,
                    supports_streaming=True,
                    width=cw, height=ch,
                    duration=video.duration,
                    read_timeout=120, write_timeout=120,
                )
            await status_msg.delete()
    except subprocess.CalledProcessError as e:
        logger.error("crop error: %s", e.stderr)
        await status_msg.edit_text("❌ Couldn't crop di video, mon.")
    except Exception:
        logger.exception("crop handler error")
        await status_msg.edit_text("❌ Sumting unexpected happen, bredren.")


async def handle_getaudio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _chat_is_active(context, update.effective_chat): return
    msg = update.message
    reply = msg.reply_to_message
    if not reply:
        await msg.reply_text("Reply to one of mi videos wid /getaudio fi extract di audio, bredren.")
        return
    # Accept both video messages and video files sent as documents
    if reply.video:
        file_id   = reply.video.file_id
        duration  = reply.video.duration
        file_size = reply.video.file_size or 0
    elif reply.document and (reply.document.mime_type or "").startswith("video/"):
        file_id   = reply.document.file_id
        duration  = None
        file_size = reply.document.file_size or 0
    else:
        await msg.reply_text("Reply to one of mi videos wid /getaudio fi extract di audio, bredren.")
        return

    # Check if we know the original URL for this video (stored when bot sent it).
    # If so, re-download audio-only via yt-dlp — much faster and bypasses the
    # 20 MB Telegram getFile limit entirely.
    original_url = context.chat_data.get("video_urls", {}).get(reply.message_id)

    status_msg = await msg.reply_text("Extractin di audio fi yuh, hol' tight bredren…")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = None

            if original_url:
                ydl_opts = {
                    "outtmpl": os.path.join(tmpdir, "audio.%(ext)s"),
                    "format": "bestaudio/best",
                    "quiet": True,
                    "no_warnings": True,
                }
                if "tiktok.com" in original_url:
                    ydl_opts["extractor_args"] = {"tiktok": {"download_without_watermark": True}}
                if is_instagram_url(original_url) and COOKIES_FILE.exists():
                    ydl_opts["cookiefile"] = str(COOKIES_FILE)
                yt_cookies = context.user_data.get("yt_cookies")
                if yt_cookies and is_youtube_url(original_url):
                    yt_ck_path = os.path.join(tmpdir, "yt_cookies.txt")
                    Path(yt_ck_path).write_text(yt_cookies)
                    ydl_opts["cookiefile"] = yt_ck_path
                try:
                    import yt_dlp
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([original_url])
                    # Find whatever file yt-dlp produced
                    candidates = [f for f in Path(tmpdir).iterdir() if f.name.startswith("audio.")]
                    if candidates:
                        audio_path = str(candidates[0])
                except Exception as e:
                    logger.warning("getaudio yt-dlp failed (%s), falling back to Telegram download", e)

            if audio_path is None:
                # Fall back: download from Telegram, then extract audio with codec copy
                # (avoids re-encoding; only re-encodes if copy fails)
                if file_size > 20 * 1024 * 1024:
                    await status_msg.edit_text(
                        f"❌ Dis video too big ({file_size / 1024 / 1024:.0f} MB), mon.\n"
                        "Audio extraction only possible fi videos under 20 MB."
                    )
                    return
                tg_file    = await context.bot.get_file(file_id)
                video_path = os.path.join(tmpdir, "video.mp4")
                await tg_file.download_to_drive(video_path)
                # Try codec copy first (lossless, instant)
                copy_path = os.path.join(tmpdir, "audio.m4a")
                result = subprocess.run(
                    ["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "copy", copy_path],
                    capture_output=True,
                )
                if result.returncode == 0 and os.path.getsize(copy_path) > 0:
                    audio_path = copy_path
                else:
                    # Fallback re-encode (handles edge cases like opus-in-mp4)
                    enc_path = os.path.join(tmpdir, "audio.mp3")
                    result = subprocess.run(
                        ["ffmpeg", "-y", "-i", video_path,
                         "-vn", "-acodec", "libmp3lame", "-q:a", "0", "-threads", "1", enc_path],
                        capture_output=True,
                    )
                    if result.returncode != 0:
                        stderr = result.stderr.decode("utf-8", errors="replace")
                        logger.error("getaudio ffmpeg error: %s", stderr)
                        if "Output file #0 does not contain" in stderr:
                            await status_msg.edit_text("❌ Dis video nuh have no audio track, mon.")
                        else:
                            await status_msg.edit_text("❌ Couldn't extract di audio, bredren.")
                        return
                    audio_path = enc_path

            # Convert to MP3 if the source came out in another format (m4a, webm, etc.)
            if audio_path and not audio_path.endswith(".mp3"):
                mp3_path = os.path.join(tmpdir, "audio_final.mp3")
                result = subprocess.run(
                    ["ffmpeg", "-y", "-i", audio_path,
                     "-vn", "-acodec", "libmp3lame", "-q:a", "0", "-threads", "1", mp3_path],
                    capture_output=True,
                )
                if result.returncode == 0 and os.path.getsize(mp3_path) > 0:
                    audio_path = mp3_path

            size = os.path.getsize(audio_path)
            if size > MAX_SIZE_BYTES:
                await status_msg.edit_text(f"Di audio too big ({size/1024/1024:.1f} MB), mon.")
                return
            await status_msg.edit_text("📤 Sendin di audio now…")
            with open(audio_path, "rb") as f:
                await msg.reply_audio(
                    audio=f,
                    duration=duration,
                    read_timeout=120,
                    write_timeout=120,
                )
            await status_msg.delete()
    except Exception:
        logger.exception("getaudio handler error")
        await status_msg.edit_text("❌ Sumting unexpected happen, bredren.")


# ---------------------------------------------------------------------------
# Per-chat on/off (persisted across restarts)
# ---------------------------------------------------------------------------

def _chat_is_active(context: ContextTypes.DEFAULT_TYPE, chat) -> bool:
    """Always active in private chats; respects on/off toggle in groups."""
    if getattr(chat, "type", None) == "private":
        return True
    return chat.id not in context.bot_data.get("disabled_chats", set())


async def _is_group_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    chat = update.effective_chat
    if chat.type == "private":
        return True
    member = await context.bot.get_chat_member(chat.id, update.effective_user.id)
    return member.status in ("administrator", "creator")


def _command_targets_me(context: ContextTypes.DEFAULT_TYPE, args: list[str]) -> bool:
    """Return False if a @username arg is present and doesn't match this bot."""
    if args and args[0].startswith("@"):
        return args[0].lstrip("@").lower() == context.bot.username.lower()
    return True


async def handle_on(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _command_targets_me(context, context.args or []): return
    if update.effective_chat.type == "private":
        await update.message.reply_text("Dis command only work in group chats, mon.")
        return
    if not await _is_group_admin(update, context):
        await update.message.reply_text("Only group admins can do dat, bredren.")
        return
    disabled: set = context.bot_data.setdefault("disabled_chats", set())
    disabled.discard(update.effective_chat.id)
    await update.message.reply_text("✅ Bot now active in dis chat, bredren 🇯🇲")


async def handle_off(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _command_targets_me(context, context.args or []): return
    if update.effective_chat.type == "private":
        await update.message.reply_text("Dis command only work in group chats, mon.")
        return
    if not await _is_group_admin(update, context):
        await update.message.reply_text("Only group admins can do dat, bredren.")
        return
    disabled: set = context.bot_data.setdefault("disabled_chats", set())
    disabled.add(update.effective_chat.id)
    await update.message.reply_text("⏸ Bot paused in dis chat, mon. Use /on fi reactivate.")


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Wah gwaan! Send mi a link fi download or a video yuh want fi edit, bredren 🇯🇲"
    )


def convert_to_gif(video_path: str, tmpdir: str, max_width: int = 480, fps: int = 15) -> str:
    palette = os.path.join(tmpdir, "palette.png")
    out = os.path.join(tmpdir, "output.gif")
    vf = f"fps={fps},scale={max_width}:-1:flags=lanczos"
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-vf", f"{vf},palettegen", palette],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-i", palette,
         "-lavfi", f"{vf}[x];[x][1:v]paletteuse", out],
        check=True, capture_output=True,
    )
    return out


def convert_to_sticker(video_path: str, tmpdir: str) -> str:
    out = os.path.join(tmpdir, "sticker.webm")
    vf = (
        "scale=512:512:force_original_aspect_ratio=decrease,"
        "pad=512:512:(ow-iw)/2:(oh-ih)/2:color=black@0"
    )
    # Probe actual duration (capped at 3s) to calculate a bitrate that fits
    # under Telegram's 256 KB video sticker limit.
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        capture_output=True, text=True,
    )
    try:
        dur = min(float(r.stdout.strip()), 3.0)
    except Exception:
        dur = 3.0
    target_kbps = int(200 * 1024 * 8 / dur / 1000)  # 200 KB budget, well under 256 KB
    passlog = os.path.join(tmpdir, "passlog")
    base_cmd = [
        "ffmpeg", "-y", "-i", video_path, "-t", "3", "-vf", vf,
        "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
        "-b:v", f"{target_kbps}k", "-row-mt", "1", "-threads", "2",
        "-passlogfile", passlog, "-an",
    ]
    subprocess.run(base_cmd + ["-pass", "1", "-f", "webm", "/dev/null"],
                   check=True, capture_output=True)
    subprocess.run(base_cmd + ["-pass", "2", out],
                   check=True, capture_output=True)
    return out


async def handle_gif_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _chat_is_active(context, update.effective_chat): return
    msg = update.message
    reply = msg.reply_to_message

    if reply and reply.sticker:
        sticker = reply.sticker
        if sticker.is_animated:
            await msg.reply_text("❌ Animated stickers (.tgs) can't be converted — only video stickers work, mon.")
            return
        if not sticker.is_video:
            await msg.reply_text("❌ Only video stickers can be converted to GIF, mon.")
            return
        if (sticker.file_size or 0) > 20 * 1024 * 1024:
            await msg.reply_text("❌ Dis sticker too big fi download, mon.")
            return
        status_msg = await msg.reply_text("⏳ Turnin di sticker into a GIF, hol' tight bredren…")
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                in_path = os.path.join(tmpdir, "input.webm")
                tg_file = await context.bot.get_file(sticker.file_id)
                await tg_file.download_to_drive(in_path)
                out_path = convert_to_gif(in_path, tmpdir)
                size = os.path.getsize(out_path)
                if size > 50 * 1024 * 1024:
                    await status_msg.edit_text("❌ Di GIF too massive, mon. Try a shorter clip.")
                    return
                await status_msg.edit_text("📤 Sendin di GIF now…")
                with open(out_path, "rb") as f:
                    await msg.reply_animation(animation=f, read_timeout=120, write_timeout=120)
                await status_msg.delete()
        except subprocess.CalledProcessError as e:
            logger.error("GIF conversion error: %s", e.stderr)
            await status_msg.edit_text("❌ Couldn't convert di sticker to GIF, mon.")
        except Exception:
            logger.exception("gif_cmd sticker handler error")
            await status_msg.edit_text("❌ Sumting unexpected happen, bredren.")
        return

    if not reply or not reply.video:
        await msg.reply_text("Reply to one ah mi videos or a video sticker wid /gif, bredren 🇯🇲")
        return
    video = reply.video
    if video.duration and video.duration > 15:
        await msg.reply_text(
            f"❌ Dat video {video.duration}s long, mon — GIFs get massive past 15 seconds. Try a shorter clip, bredren."
        )
        return
    if (video.file_size or 0) > 20 * 1024 * 1024:
        await msg.reply_text("❌ Dis video too big fi download (20 MB limit fi dis one), mon.")
        return
    status_msg = await msg.reply_text("⏳ Turnin dis into a GIF, hol' tight bredren…")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            in_path = os.path.join(tmpdir, "input.mp4")
            tg_file = await context.bot.get_file(video.file_id)
            await tg_file.download_to_drive(in_path)
            out_path = convert_to_gif(in_path, tmpdir)
            size = os.path.getsize(out_path)
            if size > 50 * 1024 * 1024:
                await status_msg.edit_text(
                    f"❌ Di GIF too massive ({size/1024/1024:.1f} MB), mon. Try a shorter or lower-res clip."
                )
                return
            await status_msg.edit_text("📤 Sendin di GIF now…")
            with open(out_path, "rb") as f:
                await msg.reply_animation(animation=f, read_timeout=120, write_timeout=120)
            await status_msg.delete()
    except subprocess.CalledProcessError as e:
        logger.error("GIF conversion error: %s", e.stderr)
        await status_msg.edit_text("❌ Couldn't convert di video to GIF, mon.")
    except Exception:
        logger.exception("gif_cmd handler error")
        await status_msg.edit_text("❌ Sumting unexpected happen, bredren.")


async def handle_sticker_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _chat_is_active(context, update.effective_chat): return
    msg = update.message
    if not msg.reply_to_message or not msg.reply_to_message.video:
        await msg.reply_text("Reply to one ah mi videos wid /sticker fi convert it to a Telegram sticker, bredren 🇯🇲")
        return
    video = msg.reply_to_message.video
    if (video.file_size or 0) > 20 * 1024 * 1024:
        await msg.reply_text("❌ Dis video too big fi download (20 MB limit), mon.")
        return
    status_msg = await msg.reply_text("⏳ Convertin to sticker, hol' tight bredren…")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            in_path = os.path.join(tmpdir, "input.mp4")
            tg_file = await context.bot.get_file(video.file_id)
            await tg_file.download_to_drive(in_path)
            out_path = convert_to_sticker(in_path, tmpdir)
            await status_msg.edit_text("📤 Sendin di sticker…")
            with open(out_path, "rb") as f:
                await msg.reply_sticker(sticker=f, read_timeout=120, write_timeout=120)
            await status_msg.delete()
            await msg.reply_text(
                "✅ Deh yah! Forward dat sticker to @Stickers bot fi add it to yuh sticker set, bredren 🇯🇲"
            )
    except subprocess.CalledProcessError as e:
        logger.error("Sticker conversion error: %s", e.stderr)
        await status_msg.edit_text("❌ Couldn't convert di video to sticker, mon.")
    except Exception:
        logger.exception("sticker_cmd handler error")
        await status_msg.edit_text("❌ Sumting unexpected happen, bredren.")


async def handle_mp4_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _chat_is_active(context, update.effective_chat): return
    msg = update.message
    reply = msg.reply_to_message
    if not reply or (not reply.animation and not reply.sticker):
        await msg.reply_text("Reply to a GIF or video sticker wid /mp4 fi convert it to a proper video, bredren 🇯🇲")
        return

    if reply.sticker:
        sticker = reply.sticker
        if sticker.is_animated:
            await msg.reply_text("❌ Animated stickers (.tgs) can't be converted — only video stickers work, mon.")
            return
        if not sticker.is_video:
            await msg.reply_text("❌ Only video stickers can be converted to MP4, mon.")
            return
        status_msg = await msg.reply_text("⏳ Convertin di sticker to MP4… hol' tight")
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                in_path  = os.path.join(tmpdir, "input.webm")
                out_path = os.path.join(tmpdir, "output.mp4")
                tg_file  = await context.bot.get_file(sticker.file_id)
                await tg_file.download_to_drive(in_path)
                subprocess.run(
                    ["ffmpeg", "-y", "-i", in_path,
                     "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                     "-c:v", "libx264", "-crf", "23", "-preset", "fast",
                     "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                     "-threads", "2", "-an", out_path],
                    check=True, capture_output=True,
                )
                size = os.path.getsize(out_path)
                if size > MAX_SIZE_BYTES:
                    await status_msg.edit_text(f"❌ Di converted file too big ({size/1024/1024:.1f} MB), mon.")
                    return
                await status_msg.edit_text("📤 Sendin di video now…")
                with open(out_path, "rb") as f:
                    await msg.reply_video(
                        video=f,
                        supports_streaming=True,
                        width=sticker.width,
                        height=sticker.height,
                        read_timeout=120,
                        write_timeout=120,
                    )
                await status_msg.delete()
        except subprocess.CalledProcessError as e:
            logger.error("MP4 conversion error: %s", e.stderr)
            await status_msg.edit_text("❌ Couldn't convert di sticker to MP4, mon.")
        except Exception:
            logger.exception("mp4_cmd sticker handler error")
            await status_msg.edit_text("❌ Sumting unexpected happen, bredren.")
        return

    animation = reply.animation
    status_msg = await msg.reply_text("⏳ Convertin di GIF to MP4… hol' tight")
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            in_path  = os.path.join(tmpdir, "input.gif")
            out_path = os.path.join(tmpdir, "output.mp4")
            tg_file  = await context.bot.get_file(animation.file_id)
            await tg_file.download_to_drive(in_path)
            subprocess.run(
                ["ffmpeg", "-y", "-i", in_path,
                 "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                 "-c:v", "libx264", "-crf", "23", "-preset", "fast",
                 "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                 "-threads", "2", "-an", out_path],
                check=True, capture_output=True,
            )
            size = os.path.getsize(out_path)
            if size > MAX_SIZE_BYTES:
                await status_msg.edit_text(
                    f"❌ Di converted file too big ({size/1024/1024:.1f} MB), mon."
                )
                return
            await status_msg.edit_text("📤 Sendin di video now…")
            with open(out_path, "rb") as f:
                await msg.reply_video(
                    video=f,
                    supports_streaming=True,
                    width=animation.width,
                    height=animation.height,
                    duration=animation.duration,
                    read_timeout=120,
                    write_timeout=120,
                )
            await status_msg.delete()
    except subprocess.CalledProcessError as e:
        logger.error("MP4 conversion error: %s", e.stderr)
        await status_msg.edit_text("❌ Couldn't convert di GIF to MP4, mon.")
    except Exception:
        logger.exception("mp4_cmd handler error")
        await status_msg.edit_text("❌ Sumting unexpected happen, bredren.")


def main() -> None:
    persistence = PicklePersistence(filepath="bot_data")
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .persistence(persistence)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    app.add_handler(CommandHandler("start",      handle_start))
    app.add_handler(CommandHandler("on",         handle_on))
    app.add_handler(CommandHandler("off",        handle_off))
    app.add_handler(CommandHandler("audio",      handle_audio_cmd))
    app.add_handler(CommandHandler("text",       handle_text_cmd))
    app.add_handler(CommandHandler("stretch",    handle_stretch))
    app.add_handler(CommandHandler("crop",       handle_crop))
    app.add_handler(CommandHandler("getaudio",   handle_getaudio))
    app.add_handler(CommandHandler("gif",        handle_gif_cmd))
    app.add_handler(CommandHandler("sticker",    handle_sticker_cmd))
    app.add_handler(CommandHandler("mp4",        handle_mp4_cmd))
    app.add_handler(CommandHandler("setcookies",   handle_set_cookies))
    app.add_handler(CommandHandler("setigcookies", handle_set_ig_cookies))
    app.add_handler(CommandHandler("settings",   handle_settings))
    app.add_handler(CommandHandler("help",       handle_help))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.AUDIO | filters.VOICE, handle_audio))
    app.add_handler(CallbackQueryHandler(handle_stretch_callback,  pattern=r"^stretch:"))
    app.add_handler(CallbackQueryHandler(handle_settings_callback, pattern=r"^settings:"))
    app.add_handler(CallbackQueryHandler(handle_compress_callback, pattern=r"^compress:"))
    app.add_handler(CallbackQueryHandler(handle_mix_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot started. Polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
