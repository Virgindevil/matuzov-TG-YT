import asyncio
import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv
from yt_dlp import YoutubeDL

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET = os.getenv("R2_BUCKET", "").strip()
R2_ENDPOINT = os.getenv("R2_ENDPOINT", "").strip().rstrip("/")
R2_LINK_TTL = int(os.getenv("R2_LINK_TTL", "21600"))  # 6 hours
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads"))
PORT = int(os.getenv("PORT", "10000"))
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

required = {
    "BOT_TOKEN": BOT_TOKEN,
    "R2_ACCESS_KEY_ID": R2_ACCESS_KEY_ID,
    "R2_SECRET_ACCESS_KEY": R2_SECRET_ACCESS_KEY,
    "R2_BUCKET": R2_BUCKET,
    "R2_ENDPOINT": R2_ENDPOINT,
}
missing = [name for name, value in required.items() if not value]
if missing:
    raise RuntimeError("Missing environment variables: " + ", ".join(missing))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
bot = Bot(BOT_TOKEN)
dp = Dispatcher()
requests: dict[int, dict] = {}
YOUTUBE_RE = re.compile(r"https?://(?:www\.)?(?:youtube\.com|youtu\.be)/\S+", re.I)
TARGET_HEIGHTS = (480, 720, 1080, 1440, 2160)

s3 = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name="auto",
)


def human_bytes(value: float | int | None) -> str:
    if value is None:
        return "—"
    n = float(value)
    units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    for unit in units:
        if n < 1024 or unit == units[-1]:
            return f"{n:.1f} {unit}" if unit != "Б" else f"{int(n)} {unit}"
        n /= 1024
    return f"{n:.1f} ТБ"


def progress_bar(percent: float) -> str:
    percent = max(0.0, min(100.0, percent))
    filled = int(percent // 10)
    return "█" * filled + "░" * (10 - filled)


async def safe_edit(message: Message, text: str, reply_markup=None):
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        pass


def extract_info(url: str) -> dict:
    with YoutubeDL({
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        # During inspection we want the complete format table, not yt-dlp's
        # default single-format selection. This avoids "Requested format is not
        # available" before the user has even chosen a quality.
        "format": "all",
    }) as ydl:
        return ydl.extract_info(url, download=False)


def available_heights(info: dict) -> list[int]:
    heights = set()
    for f in info.get("formats", []):
        if f.get("vcodec") in (None, "none"):
            continue
        h = f.get("height")
        if isinstance(h, int) and h in TARGET_HEIGHTS:
            heights.add(h)
    return sorted(heights)


def quality_keyboard(heights: list[int]) -> InlineKeyboardMarkup:
    labels = {480: "480p", 720: "720p HD", 1080: "1080p Full HD", 1440: "1440p 2K", 2160: "2160p 4K"}
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=labels[h], callback_data=f"q:{h}")] for h in heights
    ])


def download_video(url: str, height: int, job_dir: Path, loop, queue: asyncio.Queue) -> tuple[Path, dict]:
    job_dir.mkdir(parents=True, exist_ok=True)
    output = str(job_dir / "%(title).120B [%(id)s].%(ext)s")
    fmt = (
        f"bv[height<={height}][ext=mp4]+ba[ext=m4a]/"
        f"bv[height<={height}]+ba/"
        f"b[height<={height}]/best[height<={height}]"
    )
    last_update = 0.0

    def hook(d):
        nonlocal last_update
        now = time.monotonic()
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            percent = (downloaded / total * 100) if total else 0
            if now - last_update < 0.8 and percent < 100:
                return
            last_update = now
            loop.call_soon_threadsafe(queue.put_nowait, {
                "stage": "download", "percent": percent, "speed": d.get("speed"), "eta": d.get("eta"),
                "downloaded": downloaded, "total": total,
            })
        elif d.get("status") == "finished":
            loop.call_soon_threadsafe(queue.put_nowait, {"stage": "merge"})

    opts = {
        "format": fmt, "outtmpl": output, "merge_output_format": "mp4", "noplaylist": True,
        "quiet": True, "no_warnings": True, "progress_hooks": [hook],
    }
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        prepared = Path(ydl.prepare_filename(info))
        candidates = [prepared, prepared.with_suffix(".mp4"), prepared.with_suffix(".mkv"), prepared.with_suffix(".webm")]
        for item in info.get("requested_downloads") or []:
            if item.get("filepath"):
                candidates.append(Path(item["filepath"]))
    existing = [p for p in candidates if p.exists()]
    if not existing:
        existing = [p for p in job_dir.iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    if not existing:
        raise RuntimeError("Скачанный файл не найден")
    return max(existing, key=lambda p: p.stat().st_size), info


def upload_to_r2(path: Path, object_key: str, loop, queue: asyncio.Queue):
    total = path.stat().st_size
    transferred = 0
    last_emit = 0.0

    def callback(bytes_amount):
        nonlocal transferred, last_emit
        transferred += bytes_amount
        now = time.monotonic()
        if now - last_emit >= 0.7 or transferred >= total:
            last_emit = now
            percent = transferred / total * 100 if total else 100
            loop.call_soon_threadsafe(queue.put_nowait, {
                "stage": "upload", "percent": percent, "uploaded": transferred, "total": total,
            })

    # Multipart is important for multi-GB objects. boto3 handles individual parts and retries.
    config = TransferConfig(
        multipart_threshold=64 * 1024 * 1024,
        multipart_chunksize=64 * 1024 * 1024,
        max_concurrency=4,
        use_threads=True,
    )
    s3.upload_file(
        str(path), R2_BUCKET, object_key,
        ExtraArgs={"ContentType": "video/mp4", "ContentDisposition": f'attachment; filename="{object_key.rsplit("/", 1)[-1]}"'},
        Callback=callback,
        Config=config,
    )


def presigned_download(object_key: str) -> str:
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": R2_BUCKET, "Key": object_key},
        ExpiresIn=max(1, min(R2_LINK_TTL, 604800)),
    )


async def progress_worker(message: Message, height: int, queue: asyncio.Queue, done: asyncio.Event):
    while not done.is_set() or not queue.empty():
        try:
            d = await asyncio.wait_for(queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            continue
        stage = d.get("stage")
        if stage == "merge":
            await safe_edit(message, f"🔧 {height}p скачано\n\nFFmpeg объединяет видео и аудио…")
        elif stage == "download":
            p = d.get("percent", 0)
            speed = human_bytes(d.get("speed")) + "/с" if d.get("speed") else "—"
            eta = f"{d['eta']} сек." if d.get("eta") is not None else "—"
            total = d.get("total")
            size = human_bytes(d.get("downloaded")) + (f" / {human_bytes(total)}" if total else "")
            await safe_edit(message, f"⬇️ YouTube • {height}p\n\n{progress_bar(p)}  {p:.1f}%\n📦 {size}\n🚀 {speed}\n⏱ {eta}")
        elif stage == "upload":
            p = d.get("percent", 0)
            await safe_edit(message, f"☁️ Загружаю в Cloudflare R2\n\n{progress_bar(p)}  {p:.1f}%\n📦 {human_bytes(d.get('uploaded'))} / {human_bytes(d.get('total'))}")


@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Привет! Пришли ссылку на YouTube. Я покажу доступные качества от 480p до 4K, "
        "скачаю выбранное видео и дам временную ссылку Cloudflare R2.\n\n"
        "Скачивай только контент, который тебе разрешено сохранять."
    )


@dp.message(F.text)
async def receive_url(message: Message):
    match = YOUTUBE_RE.search((message.text or "").strip())
    if not match:
        await message.answer("Пришли ссылку на видео YouTube.")
        return
    status = await message.answer("🔎 Проверяю доступные качества…")
    try:
        info = await asyncio.to_thread(extract_info, match.group(0))
        heights = available_heights(info)
        if not heights:
            await status.edit_text("Не нашёл вариантов 480p–4K для этого видео.")
            return
        requests[message.from_user.id] = {"url": match.group(0), "title": info.get("title", "Видео"), "heights": heights}
        await status.edit_text(f"🎬 {info.get('title', 'Видео')}\n\nВыбери качество:", reply_markup=quality_keyboard(heights))
    except Exception as e:
        logging.exception("Inspect failed")
        await status.edit_text(f"❌ Не удалось прочитать видео.\n\n{type(e).__name__}: {e}")


@dp.callback_query(F.data.startswith("q:"))
async def choose_quality(callback: CallbackQuery):
    await callback.answer()
    req = requests.get(callback.from_user.id)
    if not req:
        await callback.message.answer("Ссылка устарела. Пришли её ещё раз.")
        return
    height = int(callback.data.split(":", 1)[1])
    if height not in req["heights"]:
        return

    status = await callback.message.answer(f"⬇️ Подготавливаю {height}p…")
    job_id = uuid.uuid4().hex
    job_dir = DOWNLOAD_DIR / job_id
    queue = asyncio.Queue()
    done = asyncio.Event()
    progress = asyncio.create_task(progress_worker(status, height, queue, done))
    try:
        loop = asyncio.get_running_loop()
        path, info = await asyncio.to_thread(download_video, req["url"], height, job_dir, loop, queue)
        size = path.stat().st_size
        object_key = f"videos/{job_id}/{height}p-{info.get('id', job_id)}.mp4"
        await safe_edit(status, f"☁️ Начинаю загрузку в Cloudflare R2…\n📦 {human_bytes(size)}")
        await asyncio.to_thread(upload_to_r2, path, object_key, loop, queue)
        url = presigned_download(object_key)
        done.set()
        await progress
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⬇️ Скачать видео", url=url)
        ]])
        hours = max(1, R2_LINK_TTL // 3600)
        await safe_edit(
            status,
            f"✅ Видео готово!\n\n🎬 {info.get('title', 'Видео')}\n📺 {height}p\n📦 {human_bytes(size)}\n\n"
            f"Ссылка действует примерно {hours} ч.",
            reply_markup=keyboard,
        )
    except Exception as e:
        done.set()
        if not progress.done():
            await progress
        logging.exception("Job failed")
        await safe_edit(status, f"❌ Ошибка\n\n{type(e).__name__}: {e}")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


async def health(_request):
    return web.json_response({"status": "ok", "service": "youtube-telegram-r2-bot", "r2_configured": True})


async def run_web_server():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    logging.info("Health server listening on 0.0.0.0:%s", PORT)
    return runner


async def main():
    runner = await run_web_server()
    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
