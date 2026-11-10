import os,time,threading,traceback,subprocess,json,re,tempfile
import shutil
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import yt_dlp

BASE=Path(__file__).resolve().parent
TMP=(Path(tempfile.gettempdir()) / "video_downloader"); TMP.mkdir(parents=True,exist_ok=True)
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
    from urllib.parse import parse_qs, urlsplit, parse_qs, unquote
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
        ss["grpcSettings"] = {
            "serviceName": q.get("serviceName", ""),
            "multiMode": q.get("mode") == "multi",
        }
        if q.get("authority"):
            ss["grpcSettings"]["authority"] = q["authority"]
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

app = FastAPI(title="Video Downloader v5.1")
ANALYSIS_SLOTS = threading.BoundedSemaphore(2)
NO_STORE = {"Cache-Control": "no-store"}

# Only accept individual video pages on the two supported services.
YOUTUBE_ID = re.compile(r"[A-Za-z0-9_-]{11}")
STATUS_PATH = re.compile(r"/(?:[A-Za-z0-9_]+/status|i/web/status)/(\d+)(/(?:video|photo)/\d+)?/?")

class URLIn(BaseModel):
    url: str

def video_url(raw):
    from urllib.parse import parse_qs

    if len(raw) > 2048:
        raise HTTPException(400, "Ссылка слишком длинная.")
    try:
        parsed = urlsplit(raw.strip())
        host = parsed.hostname
        if (
            parsed.scheme in ("http", "https")
            and parsed.netloc.lower() == host
        ):
            parts = parsed.path.strip("/").split("/")
            video_id = ""
            if host == "youtu.be" and len(parts) == 1:
                video_id = parts[0]
            elif host in ("youtube.com", "www.youtube.com", "m.youtube.com") and parsed.path == "/watch":
                video_id = parse_qs(parsed.query).get("v", [""])[0]
            elif host in ("youtube.com", "www.youtube.com", "m.youtube.com", "www.youtube-nocookie.com"):
                if len(parts) == 2 and parts[0] in ("shorts", "live", "embed", "v"):
                    video_id = parts[1]
            if YOUTUBE_ID.fullmatch(video_id):
                return f"https://www.youtube.com/watch?v={video_id}", "YouTube"
            if host in ("x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"):
                match = STATUS_PATH.fullmatch(parsed.path)
                if match:
                    return f"https://x.com/i/web/status/{match[1]}{match[2] or ''}", "X"
    except ValueError:
        pass
    raise HTTPException(400, "Вставьте ссылку на ролик YouTube или запись Twitter/X.")

def opts(source):
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 15,
        "extractor_retries": 1,
        "js_runtimes": {"node": {}},
    }
    if source == "YouTube":
        options["allowed_extractors"] = ["youtube"]
        cookies = sync_youtube_cookies()
        proxy = os.getenv("YOUTUBE_PROXY", "").strip()
        if proxy:
            options["proxy"] = proxy
        elif xray_ready():
            options["proxy"] = "socks5://127.0.0.1:1080"
    else:
        options["allowed_extractors"] = ["twitter"]
        cookies = sync_twitter_cookies()
    if cookies is not None and cookies.is_file():
        options["cookiefile"] = str(cookies)
    return options

@app.get("/api/health")
def health():
    return {"ok": True, "delivery": "direct"}

def probe_youtube_connection(*routing):
    probe = subprocess.run(
        ["curl", "-q", "-sS", "-o", os.devnull, "-w", "%{http_code}",
         "--connect-timeout", "8", "--max-time", "10", *routing,
         "https://www.youtube.com/robots.txt"],
        capture_output=True, text=True, timeout=12,
    )
    status = probe.stdout.strip()
    return {
        "youtube_http": status,
        "curl_exit": probe.returncode,
        "ok": probe.returncode == 0 and status.startswith(("2", "3")),
    }

@app.get("/api/network-test")
def network_test():
    result = {
        "revision": os.getenv("RENDER_GIT_COMMIT", "")[:7],
        "xray_running": xray_ready(),
        "youtube_proxy_configured": bool(os.getenv("YOUTUBE_PROXY", "").strip()),
    }
    try:
        result["direct"] = probe_youtube_connection("--noproxy", "*")
    except (OSError, subprocess.TimeoutExpired):
        result["direct"] = {"ok": False, "error": "Проверка соединения не завершилась"}
    if not xray_ready():
        result["ok"]=False
        result["error"]="Xray не запущен"
        return result
    try:
        result.update(probe_youtube_connection("--noproxy", "", "--socks5-hostname", "127.0.0.1:1080"))
    except (OSError, subprocess.TimeoutExpired):
        result.update(ok=False, error="Проверка соединения через Xray не завершилась")
    return result

def direct_formats(info, source):
    domain = "googlevideo.com" if source == "YouTube" else "video.twimg.com"
    by_height = {}
    for fmt in info.get("formats") or []:
        if (
            fmt.get("ext") != "mp4"
            or fmt.get("protocol") != "https"
            or fmt.get("has_drm")
            or fmt.get("vcodec") == "none"
            or fmt.get("acodec") == "none"
        ):
            continue
        if fmt.get("vcodec") is None or fmt.get("acodec") is None:
            # X's progressive video variants omit codec metadata.
            if source != "X" or not re.fullmatch(r"http(?:-\d+)?", fmt.get("format_id") or ""):
                continue
        link = fmt.get("url") or ""
        try:
            parsed = urlsplit(link)
            host = parsed.hostname or ""
            if parsed.scheme != "https" or parsed.netloc.lower() != host:
                continue
        except ValueError:
            continue
        if host != domain and not host.endswith("." + domain):
            continue
        height = fmt.get("height") or 0
        if height not in by_height or (fmt.get("tbr") or 0) > by_height[height][0]:
            by_height[height] = (fmt.get("tbr") or 0, {
                "mode": f"video:{height}",
                "label": f"{height}p" if height else "MP4",
                "url": link,
            })
    return [by_height[h][1] for h in sorted(by_height, reverse=True)]

def analyze_video(raw_url):
    url, source = video_url(raw_url)
    if not ANALYSIS_SLOTS.acquire(blocking=False):
        raise HTTPException(429, "Сервис занят. Повторите запрос через несколько секунд.")
    try:
        options = opts(source)
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError:
            if source != "YouTube" or options.get("proxy") != "socks5://127.0.0.1:1080" or os.getenv("YOUTUBE_PROXY", "").strip():
                raise
            direct_options = options | {"proxy": ""}
            direct_options.pop("cookiefile", None)
            with yt_dlp.YoutubeDL(direct_options) as ydl:
                info = ydl.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError:
        raise HTTPException(422, "Источник не отдал информацию о видео. Проверьте, что ролик доступен публично.")
    finally:
        ANALYSIS_SLOTS.release()
    if not info or info.get("_type") in ("playlist", "multi_video"):
        raise HTTPException(422, "Нужна ссылка на один ролик.")
    qualities = direct_formats(info, source)
    if not qualities:
        raise HTTPException(422, "У источника нет прямого MP4 со звуком. Для этого ролика используйте Windows-приложение.")
    return {
        "title": info.get("title") or "Видео",
        "source": source,
        "qualities": qualities,
    }

@app.post("/api/analyze")
def analyze(data: URLIn):
    return JSONResponse(analyze_video(data.url), headers=NO_STORE)

@app.get("/api/download")
def direct_download(url: str, mode: str = "best"):
    if mode != "best" and not re.fullmatch(r"video:[0-9]{1,5}", mode):
        raise HTTPException(400, "Доступно только прямое скачивание MP4 со звуком.")
    qualities = analyze_video(url)["qualities"]
    selected = qualities[0] if mode == "best" else next(
        (quality for quality in qualities if quality["mode"] == mode), None
    )
    if selected is None:
        raise HTTPException(422, "Для этого разрешения нет прямого MP4 со звуком.")
    return RedirectResponse(selected["url"], status_code=302, headers=NO_STORE)

sync_youtube_cookies(force=True)
sync_twitter_cookies(force=True)
start_xray()
app.mount("/",StaticFiles(directory=BASE/"static",html=True),name="static")
