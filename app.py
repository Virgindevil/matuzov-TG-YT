
import asyncio, os, re, shutil, time, uuid
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

BASE=Path(__file__).resolve().parent
WORK=Path(os.getenv("WORK_DIR","/tmp/ytdl-web")); WORK.mkdir(parents=True,exist_ok=True)
COOKIE_SOURCE=os.getenv("YOUTUBE_COOKIES_FILE","/etc/secrets/youtube_cookies.txt").strip()
COOKIE_FILE="/tmp/youtube_cookies.txt"

def prepare_cookie_copy():
    source=Path(COOKIE_SOURCE)
    target=Path(COOKIE_FILE)
    if not source.is_file() or source.stat().st_size <= 0:
        return False
    try:
        shutil.copyfile(source,target)
        os.chmod(target,0o600)
        return target.is_file() and target.stat().st_size > 0
    except Exception:
        return False

prepare_cookie_copy()
R2_ENDPOINT=os.getenv("R2_ENDPOINT","").strip().rstrip("/")
R2_ACCESS_KEY_ID=os.getenv("R2_ACCESS_KEY_ID","").strip()
R2_SECRET_ACCESS_KEY=os.getenv("R2_SECRET_ACCESS_KEY","").strip()
R2_BUCKET=os.getenv("R2_BUCKET","").strip()
R2_LINK_TTL=int(os.getenv("R2_LINK_TTL","21600"))
MAX_CONCURRENT_JOBS=max(1,int(os.getenv("MAX_CONCURRENT_JOBS","1")))
QUALITIES=[480,720,1080,1440,2160]
jobs:dict[str,dict[str,Any]]={}
slots=asyncio.Semaphore(MAX_CONCURRENT_JOBS)

app=FastAPI(title="YouTube Downloader Web")
app.mount("/static",StaticFiles(directory=BASE/"static"),name="static")

class AnalyzeRequest(BaseModel): url:str
class DownloadRequest(BaseModel): url:str; height:int

def cookies_ok():
    p=Path(COOKIE_FILE)
    if not (p.is_file() and p.stat().st_size > 0):
        prepare_cookie_copy()
    return p.is_file() and p.stat().st_size > 0

def r2_ok(): return all([R2_ENDPOINT,R2_ACCESS_KEY_ID,R2_SECRET_ACCESS_KEY,R2_BUCKET])

def client():
    return boto3.client("s3",endpoint_url=R2_ENDPOINT,aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,region_name="auto",
        config=Config(signature_version="s3v4",retries={"max_attempts":5,"mode":"standard"}))

def clean(url):
    url=url.strip()
    if not re.match(r"^https?://",url,re.I): raise ValueError("Нужна полная http/https ссылка.")
    return url

def common_ydl():
    prepare_cookie_copy()
    o={"quiet":True,"no_warnings":True,"noplaylist":True}
    if cookies_ok(): o["cookiefile"]=COOKIE_FILE
    return o

def probe(url):
    o=common_ydl()|{"skip_download":True,"extract_flat":False}
    with yt_dlp.YoutubeDL(o) as y:
        info=y.extract_info(url,download=False)
    fs=info.get("formats") or []
    hs=sorted({int(f["height"]) for f in fs if f.get("height") and f.get("vcodec") not in (None,"none")})
    av=[q for q in QUALITIES if any(abs(h-q)<=8 for h in hs)]
    if not av and hs: av=[q for q in QUALITIES if q<=max(hs)]
    return {"title":info.get("title") or "Без названия","thumbnail":info.get("thumbnail"),
            "duration":info.get("duration"),"uploader":info.get("uploader") or info.get("channel"),
            "available":av,"max_height":max(hs) if hs else None}

def update(j,**kw):
    if j in jobs: jobs[j].update(kw,updated_at=time.time())

def result_file(folder):
    a=[p for p in folder.iterdir() if p.is_file() and not p.name.endswith((".part",".ytdl",".temp"))
       and p.suffix.lower() not in (".jpg",".jpeg",".png",".webp")]
    if not a: raise RuntimeError("Итоговый файл не найден.")
    return max(a,key=lambda p:p.stat().st_size)

def download_sync(j,url,h,folder):
    def hook(d):
        if d.get("status")=="downloading":
            got=d.get("downloaded_bytes") or 0; total=d.get("total_bytes") or d.get("total_bytes_estimate")
            update(j,stage="download",progress=round(got/total*100,1) if total else None,
                   downloaded_bytes=got,total_bytes=total,speed=d.get("speed"),eta=d.get("eta"),
                   message=f"Скачивание с YouTube · до {h}p")
        elif d.get("status")=="finished":
            update(j,stage="merge",progress=None,message="FFmpeg объединяет видео и звук…")
    o=common_ydl()|{
        "format":f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/bestvideo+bestaudio/best",
        "outtmpl":str(folder/"%(title).180B [%(id)s].%(ext)s"),"merge_output_format":"mkv",
        "progress_hooks":[hook],"retries":10,"fragment_retries":10,"concurrent_fragment_downloads":4}
    with yt_dlp.YoutubeDL(o) as y: y.download([url])
    return result_file(folder)

def upload_sync(j,path,key):
    c=client(); size=path.stat().st_size; state={"n":0,"t":0}
    def cb(n):
        state["n"]+=n
        if time.time()-state["t"]>.5 or state["n"]>=size:
            state["t"]=time.time()
            update(j,stage="upload",progress=round(state["n"]/size*100,1),downloaded_bytes=state["n"],
                   total_bytes=size,message="Загрузка в Cloudflare R2")
    cfg=TransferConfig(multipart_threshold=64*1024**2,multipart_chunksize=64*1024**2,max_concurrency=4,use_threads=True)
    c.upload_file(str(path),R2_BUCKET,key,Callback=cb,Config=cfg)
    return c.generate_presigned_url("get_object",Params={"Bucket":R2_BUCKET,"Key":key},ExpiresIn=R2_LINK_TTL)

async def worker(j,url,h):
    folder=WORK/j; folder.mkdir(parents=True,exist_ok=True)
    async with slots:
        try:
            update(j,status="running",stage="download",message="Запускаю yt-dlp…")
            p=await asyncio.to_thread(download_sync,j,url,h,folder)
            size=p.stat().st_size; update(j,stage="upload",progress=0,file_size=size,message="Загружаю в R2…")
            key=f"downloads/{time.strftime('%Y/%m/%d')}/{j}/{re.sub(r'[^A-Za-z0-9._-]+','_',p.name)[-180:]}"
            link=await asyncio.to_thread(upload_sync,j,p,key)
            update(j,status="done",stage="done",progress=100,file_size=size,download_url=link,
                   expires_in=R2_LINK_TTL,message="Видео готово.")
        except Exception as e: update(j,status="error",stage="error",message=f"{type(e).__name__}: {e}")
        finally: shutil.rmtree(folder,ignore_errors=True)

@app.get("/")
async def index(): return FileResponse(BASE/"static"/"index.html")

@app.get("/health")
async def health():
    return {"status":"ok","service":"youtube-downloader-web","r2_configured":r2_ok(),
            "youtube_cookie_secret":Path(COOKIE_SOURCE).is_file(),
            "youtube_cookies":cookies_ok(),"max_concurrent_jobs":MAX_CONCURRENT_JOBS}

@app.post("/api/analyze")
async def analyze(req:AnalyzeRequest):
    try:
        data=await asyncio.to_thread(probe,clean(req.url))
        if not data["available"]: raise HTTPException(422,"Не найдено поддерживаемых качеств 480p–2160p.")
        return data
    except HTTPException: raise
    except Exception as e: raise HTTPException(400,f"{type(e).__name__}: {e}")

@app.post("/api/download")
async def make(req:DownloadRequest):
    if req.height not in QUALITIES: raise HTTPException(400,"Неподдерживаемое качество.")
    if not r2_ok(): raise HTTPException(503,"R2 не настроен.")
    try: url=clean(req.url)
    except ValueError as e: raise HTTPException(400,str(e))
    j=uuid.uuid4().hex
    jobs[j]={"id":j,"status":"queued","stage":"queued","progress":0,"height":req.height,
             "message":"Задача поставлена в очередь.","created_at":time.time(),"updated_at":time.time()}
    asyncio.create_task(worker(j,url,req.height)); return {"job_id":j}

@app.get("/api/jobs/{j}")
async def status(j:str):
    if j not in jobs: raise HTTPException(404,"Задача не найдена.")
    return jobs[j]
