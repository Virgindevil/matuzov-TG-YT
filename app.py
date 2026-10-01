import os,time,uuid,shutil,threading,traceback,subprocess,json
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI,HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp

BASE=Path(__file__).resolve().parent
TMP=Path("/tmp/video_downloader"); TMP.mkdir(parents=True,exist_ok=True)
SECRET_YT_COOKIES=Path("/etc/secrets/youtube.txt")
WORK_YT_COOKIES=TMP/"youtube.txt"
SECRET_VLESS=Path("/etc/secrets/vless.txt")
XRAY_CONFIG=TMP/"xray.json"
XRAY_PROC=None
XRAY_ERROR=None

def parse_vless_uri(uri):
    from urllib.parse import urlsplit, parse_qs, unquote
    p=urlsplit(uri.strip())
    if p.scheme.lower()!="vless" or not p.username or not p.hostname or not p.port:
        raise ValueError("Некорректный VLESS URL")
    q={k:v[-1] for k,v in parse_qs(p.query).items()}
    stream=q.get("type","tcp")
    security=q.get("security","none")
    outbound={
        "protocol":"vless",
        "settings":{"vnext":[{"address":p.hostname,"port":p.port,"users":[{
            "id":unquote(p.username),"encryption":q.get("encryption","none"),
            **({"flow":q["flow"]} if q.get("flow") else {})
        }]}]},
        "streamSettings":{"network":stream,"security":security}
    }
    ss=outbound["streamSettings"]
    if security=="tls":
        ss["tlsSettings"]={"serverName":q.get("sni",p.hostname),"allowInsecure":q.get("allowInsecure","0")=="1"}
        if q.get("alpn"): ss["tlsSettings"]["alpn"]=q["alpn"].split(",")
        if q.get("fp"): ss["tlsSettings"]["fingerprint"]=q["fp"]
    elif security=="reality":
        ss["realitySettings"]={
            "serverName":q.get("sni",p.hostname),
            "fingerprint":q.get("fp","chrome"),
            "publicKey":q.get("pbk",""),
            "shortId":q.get("sid",""),
            "spiderX":unquote(q.get("spx",""))
        }
    if stream=="ws":
        ss["wsSettings"]={"path":unquote(q.get("path","/")),"headers":{"Host":q.get("host",q.get("sni",p.hostname))}}
    elif stream=="grpc":
        ss["grpcSettings"]={"serviceName":unquote(q.get("serviceName",""))}
    elif stream=="tcp" and q.get("headerType")=="http":
        ss["tcpSettings"]={"header":{"type":"http","request":{"path":[unquote(q.get("path","/"))],"headers":{"Host":[q.get("host",p.hostname)]}}}}
    return {
        "log":{"loglevel":"warning"},
        "inbounds":[{"listen":"127.0.0.1","port":1080,"protocol":"socks","settings":{"udp":True}}],
        "outbounds":[outbound,{"protocol":"freedom","tag":"direct"}]
    }

def start_xray():
    global XRAY_PROC,XRAY_ERROR
    if not SECRET_VLESS.is_file():
        XRAY_ERROR="vless.txt не найден"
        return False
    try:
        uri=SECRET_VLESS.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        cfg=parse_vless_uri(uri)
        XRAY_CONFIG.write_text(json.dumps(cfg,ensure_ascii=False),encoding="utf-8")
        XRAY_PROC=subprocess.Popen(["xray","run","-config",str(XRAY_CONFIG)],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
        time.sleep(1)
        if XRAY_PROC.poll() is not None:
            err=(XRAY_PROC.stderr.read() if XRAY_PROC.stderr else "")[-500:]
            XRAY_ERROR="Xray не запустился: "+err
            return False
        XRAY_ERROR=None
        return True
    except Exception as e:
        XRAY_ERROR=str(e)
        traceback.print_exc()
        return False

def xray_ready():
    return XRAY_PROC is not None and XRAY_PROC.poll() is None

def sync_youtube_cookies():
    if not SECRET_YT_COOKIES.is_file():
        return None
    try:
        shutil.copy2(SECRET_YT_COOKIES, WORK_YT_COOKIES)
        return WORK_YT_COOKIES
    except Exception:
        traceback.print_exc()
        return None
JOBS={}; LOCK=threading.Lock(); SEM=threading.Semaphore(int(os.getenv("MAX_JOBS","1")))
app=FastAPI(title="Video Downloader v5")

class URLIn(BaseModel): url:str
class DownloadIn(BaseModel): url:str; mode:str="best"

def valid(u):
    u=u.strip(); p=urlparse(u)
    if p.scheme not in ("http","https") or not p.netloc: raise HTTPException(400,"Некорректный URL")
    return u
def opts(url=""):
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
    }

    if "youtube.com" in url or "youtu.be" in url:
        cookies = sync_youtube_cookies()
        if cookies is not None and cookies.is_file():
            options["cookiefile"] = str(cookies)

        proxy = os.getenv("YOUTUBE_PROXY", "").strip()
        if proxy:
            options["proxy"] = proxy
        elif xray_ready():
            options["proxy"] = "socks5://127.0.0.1:1080"

    return options
def setj(j,**kw):
    with LOCK: JOBS[j].update(kw)

@app.get("/api/health")
def health():
    work = sync_youtube_cookies()
    return {
        "ok": True,
        "youtube_secret_file": SECRET_YT_COOKIES.is_file(),
        "youtube_cookies": work is not None and work.is_file(),
        "youtube_cookies_writable": work is not None and os.access(work, os.W_OK),
        "youtube_cookies_size": work.stat().st_size if work is not None and work.is_file() else 0,
        "youtube_proxy_configured": bool(os.getenv("YOUTUBE_PROXY", "").strip()),
        "vless_secret_file": SECRET_VLESS.is_file(),
        "xray_running": xray_ready(),
        "xray_error": XRAY_ERROR,
    }

@app.post("/api/analyze")
def analyze(d:URLIn):
    u=valid(d.url)
    try:
        with yt_dlp.YoutubeDL(opts(u)|{"skip_download":True}) as y: i=y.extract_info(u,download=False)
        fs=i.get("formats") or []
        hs=sorted({int(f["height"]) for f in fs if f.get("height") and f.get("vcodec") not in (None,"none")})
        chosen=[]
        for h in hs:
            if not chosen or abs(h-chosen[-1])>8: chosen.append(h)
            elif h>chosen[-1]: chosen[-1]=h
        if len(chosen)>8:
            ids={round(x*(len(chosen)-1)/7) for x in range(8)}; chosen=[chosen[x] for x in sorted(ids)]
        duration=float(i.get("duration") or 0)
        def fsize(f):
            size=f.get("filesize") or f.get("filesize_approx")
            if size: return int(size)
            tbr=f.get("tbr")
            return int(float(tbr)*1000/8*duration) if tbr and duration else 0
        audios=[f for f in fs if f.get("acodec") not in (None,"none") and f.get("vcodec")=="none"]
        best_audio=max(audios,key=lambda f:(f.get("abr") or f.get("tbr") or 0),default=None)
        audio_size=fsize(best_audio) if best_audio else 0
        q=[]
        for h in chosen:
            vids=[f for f in fs if f.get("height") and int(f["height"])<=h and f.get("vcodec") not in (None,"none")]
            best_video=max(vids,key=lambda f:(int(f.get("height") or 0),f.get("tbr") or 0),default=None)
            size=(fsize(best_video) if best_video else 0)+audio_size
            q.append({"mode":f"video:{h}","label":"4K" if 2100<=h<=2200 else ("8K" if h>=4320 else f"{h}p"),"size":size or None})
        if audios: q.append({"mode":"audio","label":"MP3","size":audio_size or None})
        best_size=max((x.get("size") or 0 for x in q if x["mode"].startswith("video:")),default=0)
        q.append({"mode":"best","label":"Лучшее","size":best_size or None})
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
        o=opts(u)|{"format":fmt,"outtmpl":str(folder/"%(title).160B [%(id)s].%(ext)s"),"progress_hooks":[hook],"postprocessor_hooks":[ph],"postprocessors":pp}
        if merge:o["merge_output_format"]=merge
        with yt_dlp.YoutubeDL(o) as y:y.extract_info(u,download=True)
        files=[p for p in folder.iterdir() if p.is_file() and not p.name.endswith((".part",".ytdl"))]
        if not files: raise RuntimeError("Готовый файл не найден")
        final=max(files,key=lambda p:p.stat().st_mtime)
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
start_xray()
app.mount("/",StaticFiles(directory=BASE/"static",html=True),name="static")
