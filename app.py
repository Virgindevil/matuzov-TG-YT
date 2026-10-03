import os,time,uuid,shutil,threading,traceback,subprocess,json
from pathlib import Path
from urllib.parse import urlparse,quote
from fastapi import FastAPI,HTTPException,Query
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp

BASE=Path(__file__).resolve().parent
TMP=Path("/tmp/video_downloader"); TMP.mkdir(parents=True,exist_ok=True)
SECRET_YT_COOKIES=Path("/etc/secrets/youtube.txt")
WORK_YT_COOKIES=TMP/"youtube.txt"
SECRET_TW_COOKIES=Path("/etc/secrets/twitter.txt")
WORK_TW_COOKIES=TMP/"twitter.txt"
SECRET_VLESS=Path("/etc/secrets/vless.txt")
XRAY_CONFIG=TMP/"xray.json"
XRAY_PROC=None
XRAY_ERROR=None
COOKIE_LOCK=threading.Lock()
COOKIE_READY=False
TW_COOKIE_READY=False

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
        "inbounds":[{"listen":"127.0.0.1","port":1080,"protocol":"socks","settings":{"udp":True}},{"listen":"127.0.0.1","port":1081,"protocol":"http","settings":{}}],
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

def sync_youtube_cookies(force=False):
    global COOKIE_READY
    if not SECRET_YT_COOKIES.is_file():
        return None
    # The Render secret file is immutable. Copy it once to writable /tmp.
    # Multiple simultaneous yt-dlp requests must never overwrite the file
    # while another request is reading/updating it.
    with COOKIE_LOCK:
        if COOKIE_READY and WORK_YT_COOKIES.is_file() and not force:
            return WORK_YT_COOKIES
        try:
            tmp_cookie=WORK_YT_COOKIES.with_suffix(".tmp")
            shutil.copyfile(SECRET_YT_COOKIES,tmp_cookie)
            os.replace(tmp_cookie,WORK_YT_COOKIES)
            COOKIE_READY=True
            return WORK_YT_COOKIES
        except Exception:
            COOKIE_READY=False
            traceback.print_exc()
            return None
def sync_twitter_cookies(force=False):
    global TW_COOKIE_READY
    if not SECRET_TW_COOKIES.is_file():
        return None
    with COOKIE_LOCK:
        if TW_COOKIE_READY and WORK_TW_COOKIES.is_file() and not force:
            return WORK_TW_COOKIES
        try:
            tmp_cookie=WORK_TW_COOKIES.with_suffix(".tmp")
            shutil.copyfile(SECRET_TW_COOKIES,tmp_cookie)
            os.replace(tmp_cookie,WORK_TW_COOKIES)
            TW_COOKIE_READY=True
            return WORK_TW_COOKIES
        except Exception:
            TW_COOKIE_READY=False
            traceback.print_exc()
            return None

STREAM_SEM=threading.BoundedSemaphore(int(os.getenv("MAX_STREAMS","1")))
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

    elif "x.com" in url or "twitter.com" in url:
        cookies = sync_twitter_cookies()
        if cookies is not None and cookies.is_file():
            options["cookiefile"] = str(cookies)

    return options
@app.get("/api/health")
def health():
    work = sync_youtube_cookies()
    tw_work = sync_twitter_cookies()
    return {
        "ok": True,
        "youtube_secret_file": SECRET_YT_COOKIES.is_file(),
        "youtube_cookies": work is not None and work.is_file(),
        "youtube_cookies_writable": work is not None and os.access(work, os.W_OK),
        "youtube_cookies_size": work.stat().st_size if work is not None and work.is_file() else 0,
        "youtube_proxy_configured": bool(os.getenv("YOUTUBE_PROXY", "").strip()),
        "twitter_secret_file": SECRET_TW_COOKIES.is_file(),
        "twitter_cookies": tw_work is not None and tw_work.is_file(),
        "twitter_cookies_writable": tw_work is not None and os.access(tw_work, os.W_OK),
        "twitter_cookies_size": tw_work.stat().st_size if tw_work is not None and tw_work.is_file() else 0,
        "vless_secret_file": SECRET_VLESS.is_file(),
        "xray_running": xray_ready(),
        "xray_error": XRAY_ERROR,
    }


def _fmt_size(f,duration=0):
    size=f.get("filesize") or f.get("filesize_approx")
    if size: return int(size)
    tbr=f.get("tbr")
    return int(float(tbr)*1000/8*duration) if tbr and duration else 0

def _pick_streams(info,mode):
    fs=info.get("formats") or []
    duration=float(info.get("duration") or 0)
    if mode=="audio":
        aud=[f for f in fs if f.get("url") and f.get("acodec") not in (None,"none") and f.get("vcodec")=="none"]
        a=max(aud,key=lambda f:(f.get("abr") or f.get("tbr") or 0),default=None)
        if not a: raise RuntimeError("Аудиопоток не найден")
        return None,a,_fmt_size(a,duration)
    if mode.startswith("video:"):
        h=int(mode.split(":",1)[1])
    else:
        h=max([int(f.get("height") or 0) for f in fs if f.get("vcodec") not in (None,"none")],default=0)
    vids=[f for f in fs if f.get("url") and f.get("vcodec") not in (None,"none") and int(f.get("height") or 0)<=h]
    if not vids: raise RuntimeError("Видеопоток не найден")
    # Prefer the requested height, then higher bitrate. Separate video-only is fine: audio is added below.
    v=max(vids,key=lambda f:(int(f.get("height") or 0),f.get("tbr") or 0))
    if v.get("acodec") not in (None,"none"):
        return v,None,_fmt_size(v,duration)
    aud=[f for f in fs if f.get("url") and f.get("acodec") not in (None,"none") and f.get("vcodec")=="none"]
    a=max(aud,key=lambda f:(f.get("abr") or f.get("tbr") or 0),default=None)
    return v,a,_fmt_size(v,duration)+(_fmt_size(a,duration) if a else 0)

def _safe_name(name):
    bad='<>:"/\\|?*'
    s="".join("_" if c in bad else c for c in (name or "video"))
    return s.strip(" .")[:150] or "video"

@app.get("/api/network-test")
def network_test():
    result={"xray_running":xray_ready()}
    if not xray_ready():
        result["ok"]=False
        result["error"]="Xray не запущен"
        return result
    try:
        p=subprocess.run(
            ["curl","-sS","-o","/dev/null","-w","%{http_code}",
             "--connect-timeout","10","--max-time","20",
             "--socks5-hostname","127.0.0.1:1080",
             "https://www.youtube.com/robots.txt"],
            capture_output=True,text=True,timeout=25
        )
        result["youtube_http"]=p.stdout.strip()
        result["curl_exit"]=p.returncode
        result["ok"]=p.returncode==0 and p.stdout.strip().startswith(("2","3"))
        if p.returncode!=0:
            result["error"]=(p.stderr or "Ошибка соединения через VLESS")[-300:]
    except Exception as e:
        result["ok"]=False
        result["error"]=str(e)
    return result

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
        q=[]
        for h in chosen:
            try:
                _,_,size=_pick_streams(i,f"video:{h}")
            except Exception:
                size=0
            q.append({"mode":f"video:{h}","label":"4K" if 2100<=h<=2200 else ("8K" if h>=4320 else f"{h}p"),"size":size or None})
        try:
            _,_,audio_size=_pick_streams(i,"audio")
            q.append({"mode":"audio","label":"MP3","size":audio_size or None})
        except Exception:
            pass
        try:
            _,_,best_size=_pick_streams(i,"best")
        except Exception:
            best_size=0
        q.append({"mode":"best","label":"Лучшее","size":best_size or None})
        return {"title":i.get("title") or "Видео","thumbnail":i.get("thumbnail"),"source":i.get("extractor_key") or i.get("extractor") or urlparse(u).hostname,"qualities":q}
    except Exception as e: raise HTTPException(400,str(e))

def _ytdlp_pipe_cmd(u,format_id):
    cmd=["yt-dlp","--quiet","--no-warnings","--no-playlist",
         "--retries","10","--fragment-retries","10","--concurrent-fragments","1",
         "--retry-sleep","fragment:1:5","--socket-timeout","30",
         "-f",str(format_id)]
    cmd+=["-o","-",u]
    if "youtube.com" in u or "youtu.be" in u:
        cookies=sync_youtube_cookies()
        if cookies is not None and cookies.is_file():
            cmd[1:1]=["--cookies",str(cookies)]
        proxy=os.getenv("YOUTUBE_PROXY","").strip()
        if proxy:
            cmd[1:1]=["--proxy",proxy]
        elif xray_ready():
            cmd[1:1]=["--proxy","socks5://127.0.0.1:1080"]
    elif "x.com" in u or "twitter.com" in u:
        cookies=sync_twitter_cookies()
        if cookies is not None and cookies.is_file():
            cmd[1:1]=["--cookies",str(cookies)]
    return cmd

@app.head("/api/download")
def direct_download_head(url:str=Query(...),mode:str=Query("best")):
    u=valid(url)
    if mode not in ("best","audio") and not mode.startswith("video:"):
        raise HTTPException(400,"Некорректный режим")
    ext="mp3" if mode=="audio" else "mkv"
    media="audio/mpeg" if mode=="audio" else "video/x-matroska"
    return StreamingResponse(iter(()),media_type=media,headers={"Cache-Control":"no-store","X-Accel-Buffering":"no"})

@app.get("/api/download")
def direct_download(url:str=Query(...),mode:str=Query("best")):
    u=valid(url)
    if mode not in ("best","audio") and not mode.startswith("video:"):
        raise HTTPException(400,"Некорректный режим")
    if not STREAM_SEM.acquire(blocking=False):
        raise HTTPException(429,"Уже идёт другая загрузка. Дождитесь её завершения.")
    slot_held=True
    try:
        # yt-dlp handles the remote YouTube/CDN connection and retries.
        # FFmpeg only reads local OS pipes, so a googlevideo redirect cannot break FFmpeg.
        with yt_dlp.YoutubeDL(opts(u)|{"skip_download":True}) as y:
            info=y.extract_info(u,download=False)
        v,a,_=_pick_streams(info,mode)
        title=_safe_name(info.get("title"))
        if mode=="audio":
            selected=[a]
            ext="mp3"; media="audio/mpeg"
        else:
            selected=[v]+([a] if a else [])
            ext="mkv"; media="video/x-matroska"

        ascii_title="".join(c if ord(c)<128 and c not in '"\\' else "_" for c in title).strip(" ._") or "video"
        unicode_name=quote(f"{title}.{ext}",safe="")
        headers={
            "Content-Disposition":f'attachment; filename="{ascii_title}.{ext}"; filename*=UTF-8\'\'{unicode_name}',
            "Cache-Control":"no-store",
            "X-Accel-Buffering":"no"
        }
        # Let yt-dlp own format downloading and merging. Piping individual
        # YouTube formats into our own ffmpeg loses timestamps for some formats.
        if mode=="audio":
            fmt=str(a["format_id"])
            cmd=_ytdlp_pipe_cmd(u,fmt)
            # yt-dlp writes the selected audio stream to stdout; transcode to MP3 here.
            ytdlp=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=None,bufsize=0)
            ff=subprocess.Popen(
                ["ffmpeg","-hide_banner","-loglevel","warning","-i","pipe:0",
                 "-vn","-c:a","libmp3lame","-q:a","0","-f","mp3","pipe:1"],
                stdin=ytdlp.stdout,stdout=subprocess.PIPE,stderr=None,bufsize=0
            )
            ytdlp.stdout.close()
            producer=ff
            helpers=[ytdlp]
        else:
            # Ask yt-dlp to merge video+audio itself and stream the final Matroska
            # file to stdout. This preserves yt-dlp's native timestamp handling.
            if a:
                fmt=f'{v["format_id"]}+{a["format_id"]}'
            else:
                fmt=str(v["format_id"])
            cmd=_ytdlp_pipe_cmd(u,fmt)
            # Diagnostic: yt-dlp normally runs with --quiet, which hides the real
            # ffmpeg failure and leaves only "exited with code 183". Remove quiet
            # for this subprocess and enable verbose output so Render logs contain
            # the exact ffmpeg error/command. Secrets/cookie contents are not printed.
            cmd=[x for x in cmd if x!="--quiet"]
            # ffmpeg cannot use the SOCKS proxy that yt-dlp normally uses.
            # The signed YouTube URLs were created through Xray, but ffmpeg then
            # fetched them directly from Render, so Google returned 403. For the
            # ffmpeg downloader use Xray's HTTP inbound instead, keeping the same
            # exit IP for extraction and media requests.
            if xray_ready():
                try:
                    pi=cmd.index("--proxy")
                    if pi+1 < len(cmd):
                        cmd[pi+1]="http://127.0.0.1:1081"
                except ValueError:
                    cmd[1:1]=["--proxy","http://127.0.0.1:1081"]
            cmd[1:1]=["--verbose","--downloader","ffmpeg","--merge-output-format","mkv"]
            producer=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=None,bufsize=0)
            helpers=[]

        def body():
            try:
                while True:
                    chunk=producer.stdout.read(1024*256)
                    if not chunk: break
                    yield chunk
            finally:
                if producer.poll() is None: producer.terminate()
                for p in helpers:
                    if p.poll() is None: p.terminate()
                try: producer.wait(timeout=3)
                except Exception: producer.kill()
                for p in helpers:
                    try: p.wait(timeout=2)
                    except Exception:
                        try: p.kill()
                        except Exception: pass
                try: STREAM_SEM.release()
                except ValueError: pass
        return StreamingResponse(body(),media_type=media,headers=headers)
    except Exception as e:
        if locals().get("slot_held",False):
            try: STREAM_SEM.release()
            except ValueError: pass
        traceback.print_exc()
        raise HTTPException(400,str(e))

sync_youtube_cookies(force=True)
sync_twitter_cookies(force=True)
start_xray()
app.mount("/",StaticFiles(directory=BASE/"static",html=True),name="static")
