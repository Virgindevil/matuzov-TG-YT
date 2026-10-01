import os,time,uuid,shutil,threading,traceback
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI,HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp

BASE=Path(__file__).resolve().parent
TMP=Path("/tmp/video_downloader"); TMP.mkdir(parents=True,exist_ok=True)
JOBS={}; LOCK=threading.Lock(); SEM=threading.Semaphore(int(os.getenv("MAX_JOBS","1")))
MAX_MB=int(os.getenv("MAX_FILE_MB","750"))
app=FastAPI(title="Video Downloader v5")

class URLIn(BaseModel): url:str
class DownloadIn(BaseModel): url:str; mode:str="best"

def valid(u):
    u=u.strip(); p=urlparse(u)
    if p.scheme not in ("http","https") or not p.netloc: raise HTTPException(400,"Некорректный URL")
    return u
def opts(): return {"quiet":True,"no_warnings":True,"noplaylist":True,"socket_timeout":20,"retries":3,"fragment_retries":3}
def setj(j,**kw):
    with LOCK: JOBS[j].update(kw)

@app.get("/api/health")
def health(): return {"ok":True}

@app.post("/api/analyze")
def analyze(d:URLIn):
    u=valid(d.url)
    try:
        with yt_dlp.YoutubeDL(opts()|{"skip_download":True}) as y: i=y.extract_info(u,download=False)
        fs=i.get("formats") or []
        hs=sorted({int(f["height"]) for f in fs if f.get("height") and f.get("vcodec") not in (None,"none")})
        chosen=[]
        for h in hs:
            if not chosen or abs(h-chosen[-1])>8: chosen.append(h)
            elif h>chosen[-1]: chosen[-1]=h
        if len(chosen)>8:
            ids={round(x*(len(chosen)-1)/7) for x in range(8)}; chosen=[chosen[x] for x in sorted(ids)]
        q=[{"mode":f"video:{h}","label":"4K" if 2100<=h<=2200 else ("8K" if h>=4320 else f"{h}p")} for h in chosen]
        if any(f.get("acodec") not in (None,"none") for f in fs): q.append({"mode":"audio","label":"MP3"})
        q.append({"mode":"best","label":"Лучшее"})
        return {"title":i.get("title") or "Видео","thumbnail":i.get("thumbnail"),"source":i.get("extractor_key") or i.get("extractor") or urlparse(u).hostname,"qualities":q}
    except Exception as e: raise HTTPException(400,str(e))

@app.post("/api/jobs")
def create(d:DownloadIn):
    u=valid(d.url); jid=uuid.uuid4().hex
    JOBS[jid]={"status":"queued","progress":0,"stage":"В очереди","error":None,"file":None,"name":None,"created":time.time()}
    threading.Thread(target=worker,args=(jid,u,d.mode),daemon=True).start()
    return {"job_id":jid}

@app.get("/api/jobs/{jid}")
def status(jid:str):
    j=JOBS.get(jid)
    if not j: raise HTTPException(404,"Задание не найдено")
    return {k:v for k,v in j.items() if k!="file"}

@app.get("/api/jobs/{jid}/file")
def file(jid:str):
    j=JOBS.get(jid)
    if not j or j["status"]!="done": raise HTTPException(404,"Файл ещё не готов")
    p=Path(j["file"])
    if not p.is_file(): raise HTTPException(410,"Временный файл уже удалён")
    return FileResponse(p,filename=j["name"],media_type="application/octet-stream")

def worker(jid,u,mode):
    folder=TMP/jid; folder.mkdir(parents=True,exist_ok=True)
    try:
      with SEM:
        setj(jid,status="working",stage="Подготовка",progress=1)
        if mode=="audio": fmt="bestaudio/best"; merge=None; pp=[{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"0"}]
        elif mode.startswith("video:"):
            h=int(mode.split(":")[1]); fmt=f"bestvideo[height<={h}]+bestaudio/best[height<={h}]/bestvideo+bestaudio/best"; merge="mkv"; pp=[]
        else: fmt="bestvideo+bestaudio/best"; merge="mkv"; pp=[]
        peak=[1]
        def hook(d):
            if d.get("status")=="downloading":
                inf=d.get("info_dict") or {}; aud=inf.get("vcodec")=="none" and inf.get("acodec") not in (None,"none")
                base,span=(72,20) if aud else (2,70); total=d.get("total_bytes") or d.get("total_bytes_estimate") or 0; got=d.get("downloaded_bytes") or 0
                fi=d.get("fragment_index") or 0; fc=d.get("fragment_count") or 0; ratio=fi/fc if fc else (got/total if total else 0)
                p=max(peak[0],int(base+max(0,min(1,ratio))*span)); peak[0]=p
                setj(jid,progress=p,stage="Скачивание аудио" if aud else "Скачивание видео")
        def ph(d):
            if d.get("status")=="started": setj(jid,progress=94,stage="Обработка FFmpeg")
            elif d.get("status")=="finished": setj(jid,progress=98,stage="Завершение")
        o=opts()|{"format":fmt,"outtmpl":str(folder/"%(title).160B [%(id)s].%(ext)s"),"progress_hooks":[hook],"postprocessor_hooks":[ph],"postprocessors":pp}
        if merge:o["merge_output_format"]=merge
        with yt_dlp.YoutubeDL(o) as y:y.extract_info(u,download=True)
        files=[p for p in folder.iterdir() if p.is_file() and not p.name.endswith((".part",".ytdl"))]
        if not files: raise RuntimeError("Готовый файл не найден")
        final=max(files,key=lambda p:p.stat().st_mtime)
        if final.stat().st_size>MAX_MB*1024*1024: raise RuntimeError(f"Файл больше серверного лимита {MAX_MB} МБ")
        setj(jid,status="done",progress=100,stage="Готово",file=str(final),name=final.name)
    except Exception as e:
        traceback.print_exc(); setj(jid,status="error",stage="Ошибка",error=str(e))

def cleanup():
    while True:
        time.sleep(600); now=time.time()
        for jid,j in list(JOBS.items()):
            if now-j["created"]>3600:
                shutil.rmtree(TMP/jid,ignore_errors=True)
                with LOCK:JOBS.pop(jid,None)
threading.Thread(target=cleanup,daemon=True).start()
app.mount("/",StaticFiles(directory=BASE/"static",html=True),name="static")
