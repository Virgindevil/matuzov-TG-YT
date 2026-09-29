import asyncio
import os
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
WORK_DIR = Path(os.getenv("WORK_DIR", "/tmp/ytdl-web"))
WORK_DIR.mkdir(parents=True, exist_ok=True)

R2_ENDPOINT = os.getenv("R2_ENDPOINT", "").strip().rstrip("/")
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET = os.getenv("R2_BUCKET", "").strip()
R2_LINK_TTL = int(os.getenv("R2_LINK_TTL", "21600"))
MAX_CONCURRENT_JOBS = max(1, int(os.getenv("MAX_CONCURRENT_JOBS", "1")))

QUALITIES = [480, 720, 1080, 1440, 2160]
jobs: dict[str, dict[str, Any]] = {}
job_slots = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

app = FastAPI(title="YouTube Downloader Web")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

class AnalyzeRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    height: int

def r2_configured() -> bool:
    return all([R2_ENDPOINT, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET])

def r2_client():
    if not r2_configured():
        raise RuntimeError("Cloudflare R2 is not configured")
    return boto3.client(
        "s3",
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 5, "mode": "standard"}),
    )

def clean_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        raise ValueError("Нужна полная http/https ссылка.")
    return url

def ydl_probe_opts():
    # ВАЖНО: format здесь намеренно отсутствует.
    return {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extract_flat": False,
    }

def probe(url: str) -> dict[str, Any]:
    with yt_dlp.YoutubeDL(ydl_probe_opts()) as ydl:
        info = ydl.extract_info(url, download=False)

    formats = info.get("formats") or []
    video_heights = sorted({
        int(f["height"]) for f in formats
        if f.get("height") and f.get("vcodec") not in (None, "none")
    })
    available = []
    for q in QUALITIES:
        # Показываем качество, если существует видеопоток такой высоты
        # или немного выше/ниже в пределах типичной маркировки YouTube.
        if any(abs(h - q) <= 8 for h in video_heights):
            available.append(q)

    # Если точных стандартных высот нет, показываем стандартные ступени
    # до максимальной реально доступной высоты.
    if not available and video_heights:
        max_h = max(video_heights)
        available = [q for q in QUALITIES if q <= max_h]

    return {
        "title": info.get("title") or "Без названия",
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel"),
        "available": available,
        "max_height": max(video_heights) if video_heights else None,
    }

def fmt_bytes(n):
    if n is None:
        return None
    n = float(n)
    units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    for u in units:
        if n < 1024 or u == units[-1]:
            return f"{n:.1f} {u}" if u != "Б" else f"{int(n)} {u}"
        n /= 1024

def set_job(job_id: str, **kwargs):
    if job_id in jobs:
        jobs[job_id].update(kwargs)
        jobs[job_id]["updated_at"] = time.time()

def find_result_file(folder: Path) -> Path:
    candidates = [
        p for p in folder.iterdir()
        if p.is_file()
        and not p.name.endswith((".part", ".ytdl", ".temp"))
        and p.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp")
    ]
    if not candidates:
        raise RuntimeError("yt-dlp завершился, но итоговый файл не найден.")
    return max(candidates, key=lambda p: p.stat().st_size)

def download_sync(job_id: str, url: str, height: int, folder: Path):
    def hook(d):
        if d.get("status") == "downloading":
            downloaded = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            percent = (downloaded / total * 100) if total else None
            set_job(
                job_id,
                stage="download",
                progress=round(percent, 1) if percent is not None else None,
                downloaded_bytes=downloaded,
                total_bytes=total,
                speed=d.get("speed"),
                eta=d.get("eta"),
                message=f"Скачивание с YouTube · до {height}p",
            )
        elif d.get("status") == "finished":
            set_job(job_id, stage="merge", progress=None, message="Скачивание завершено. FFmpeg объединяет видео и звук…")

    # Не требуем строго format_id. Берём лучший video <= выбранной высоты + лучший audio.
    selector = (
        f"bestvideo[height<={height}]+bestaudio/"
        f"best[height<={height}]/"
        f"bestvideo+bestaudio/best"
    )
    opts = {
        "format": selector,
        "outtmpl": str(folder / "%(title).180B [%(id)s].%(ext)s"),
        "merge_output_format": "mkv",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    return find_result_file(folder)

def upload_sync(job_id: str, path: Path, key: str):
    client = r2_client()
    size = path.stat().st_size
    state = {"seen": 0, "last_emit": 0.0}

    def callback(bytes_amount):
        state["seen"] += bytes_amount
        now = time.time()
        if now - state["last_emit"] >= 0.5 or state["seen"] >= size:
            state["last_emit"] = now
            set_job(
                job_id,
                stage="upload",
                progress=round(state["seen"] / size * 100, 1) if size else 100,
                downloaded_bytes=state["seen"],
                total_bytes=size,
                message="Загрузка в Cloudflare R2",
            )

    config = TransferConfig(
        multipart_threshold=64 * 1024 * 1024,
        multipart_chunksize=64 * 1024 * 1024,
        max_concurrency=4,
        use_threads=True,
    )
    client.upload_file(str(path), R2_BUCKET, key, Callback=callback, Config=config)
    url = client.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": R2_BUCKET,
            "Key": key,
            "ResponseContentDisposition": f'attachment; filename="{path.name.replace(chr(34), "")}"',
        },
        ExpiresIn=R2_LINK_TTL,
    )
    return url

async def run_job(job_id: str, url: str, height: int):
    folder = WORK_DIR / job_id
    folder.mkdir(parents=True, exist_ok=True)
    async with job_slots:
        try:
            set_job(job_id, status="running", stage="download", message="Запускаю yt-dlp…")
            path = await asyncio.to_thread(download_sync, job_id, url, height, folder)
            size = path.stat().st_size
            set_job(job_id, stage="upload", progress=0, file_size=size, message="Загружаю результат в Cloudflare R2…")
            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", path.name)[-180:]
            key = f"downloads/{time.strftime('%Y/%m/%d')}/{job_id}/{safe_name}"
            signed_url = await asyncio.to_thread(upload_sync, job_id, path, key)
            set_job(
                job_id,
                status="done",
                stage="done",
                progress=100,
                file_size=size,
                download_url=signed_url,
                expires_in=R2_LINK_TTL,
                message="Видео готово.",
            )
        except Exception as e:
            set_job(job_id, status="error", stage="error", message=f"{type(e).__name__}: {e}")
        finally:
            shutil.rmtree(folder, ignore_errors=True)

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "youtube-downloader-web",
        "r2_configured": r2_configured(),
        "max_concurrent_jobs": MAX_CONCURRENT_JOBS,
    }

@app.post("/api/analyze")
async def analyze(req: AnalyzeRequest):
    try:
        url = clean_url(req.url)
        data = await asyncio.to_thread(probe, url)
        if not data["available"]:
            raise HTTPException(422, "Не удалось найти поддерживаемые качества 480p–2160p.")
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"{type(e).__name__}: {e}")

@app.post("/api/download")
async def create_download(req: DownloadRequest):
    if req.height not in QUALITIES:
        raise HTTPException(400, "Неподдерживаемое качество.")
    if not r2_configured():
        raise HTTPException(503, "R2 не настроен на сервере.")
    try:
        url = clean_url(req.url)
    except ValueError as e:
        raise HTTPException(400, str(e))

    job_id = uuid.uuid4().hex
    jobs[job_id] = {
        "id": job_id,
        "status": "queued",
        "stage": "queued",
        "progress": 0,
        "height": req.height,
        "message": "Задача поставлена в очередь.",
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    asyncio.create_task(run_job(job_id, url, req.height))
    return {"job_id": job_id}

@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Задача не найдена.")
    return job
