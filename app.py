import os, time, uuid, threading
from pathlib import Path
from typing import Optional, Any
from fastapi import FastAPI, HTTPException, Header
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl
import boto3
from botocore.config import Config

app = FastAPI(title="Universal Video Downloader API", version="1.0")

WORKER_TOKEN = os.getenv("WORKER_TOKEN", "").strip()
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID", "").strip()
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "").strip()
R2_BUCKET = os.getenv("R2_BUCKET", "").strip()
R2_ENDPOINT = os.getenv("R2_ENDPOINT", "").strip()
R2_LINK_TTL = int(os.getenv("R2_LINK_TTL", "86400"))
JOB_TTL_HOURS = int(os.getenv("JOB_TTL_HOURS", "48"))

lock = threading.RLock()
jobs: dict[str, dict[str, Any]] = {}

def now(): return int(time.time())

def public_job(j):
    keys = ("id","url","status","stage","progress","title","thumbnail","duration",
            "formats","selected","error","download_url","filename","created_at","updated_at",
            "worker_name")
    return {k:j.get(k) for k in keys if k in j}

def auth_worker(authorization: Optional[str]):
    if not WORKER_TOKEN:
        raise HTTPException(503, "WORKER_TOKEN is not configured")
    if authorization != f"Bearer {WORKER_TOKEN}":
        raise HTTPException(401, "Invalid worker token")

def r2():
    if not all([R2_ACCESS_KEY_ID,R2_SECRET_ACCESS_KEY,R2_BUCKET,R2_ENDPOINT]):
        raise RuntimeError("R2 is not configured")
    return boto3.client("s3", endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY_ID,
        aws_secret_access_key=R2_SECRET_ACCESS_KEY,
        config=Config(signature_version="s3v4"))

def presign(key):
    return r2().generate_presigned_url("get_object",
        Params={"Bucket":R2_BUCKET,"Key":key}, ExpiresIn=R2_LINK_TTL)

def cleanup():
    cutoff=now()-JOB_TTL_HOURS*3600
    with lock:
        dead=[jid for jid,j in jobs.items() if j.get("updated_at",0)<cutoff]
        for jid in dead: jobs.pop(jid,None)

class CreateJob(BaseModel):
    url: HttpUrl

class SelectFormat(BaseModel):
    format_id: str

class WorkerUpdate(BaseModel):
    status: Optional[str]=None
    stage: Optional[str]=None
    progress: Optional[float]=None
    title: Optional[str]=None
    thumbnail: Optional[str]=None
    duration: Optional[float]=None
    formats: Optional[list[dict]]=None
    filename: Optional[str]=None
    r2_key: Optional[str]=None
    error: Optional[str]=None
    worker_name: Optional[str]=None

@app.get("/health")
def health():
    return {"status":"ok","service":"universal-video-downloader",
            "worker_auth_configured":bool(WORKER_TOKEN),
            "r2_configured":all([R2_ACCESS_KEY_ID,R2_SECRET_ACCESS_KEY,R2_BUCKET,R2_ENDPOINT]),
            "queued":sum(1 for j in jobs.values() if j["status"] in ("queued","ready"))}

@app.post("/api/jobs")
def create_job(body: CreateJob):
    cleanup()
    jid=uuid.uuid4().hex
    j={"id":jid,"url":str(body.url),"status":"queued","stage":"Ожидание анализа",
       "progress":0,"created_at":now(),"updated_at":now(),"formats":[]}
    with lock: jobs[jid]=j
    return public_job(j)

@app.get("/api/jobs/{jid}")
def get_job(jid:str):
    with lock: j=jobs.get(jid)
    if not j: raise HTTPException(404,"Job not found")
    # Refresh a presigned URL if an R2 key exists.
    out=public_job(j)
    if j.get("r2_key") and j.get("status")=="done":
        try: out["download_url"]=presign(j["r2_key"])
        except Exception: pass
    return out

@app.post("/api/jobs/{jid}/select")
def select_format(jid:str, body:SelectFormat):
    with lock:
        j=jobs.get(jid)
        if not j: raise HTTPException(404,"Job not found")
        if j.get("status")!="awaiting_selection":
            raise HTTPException(409,"Job is not waiting for format selection")
        valid={str(x.get("id")) for x in j.get("formats",[])}
        if body.format_id not in valid:
            raise HTTPException(400,"Unknown format")
        j["selected"]=body.format_id
        j["status"]="ready"; j["stage"]="Ожидание скачивания"; j["updated_at"]=now()
        return public_job(j)

@app.get("/worker/next")
def worker_next(authorization: Optional[str]=Header(None), worker_name:str="worker"):
    auth_worker(authorization); cleanup()
    with lock:
        # Analysis jobs first, then selected download jobs.
        candidates=[j for j in jobs.values() if j["status"] in ("queued","ready")]
        candidates.sort(key=lambda x:x["created_at"])
        if not candidates: return {"job":None}
        j=candidates[0]
        j["status"]="analyzing" if j["status"]=="queued" else "downloading"
        j["worker_name"]=worker_name
        j["updated_at"]=now()
        return {"job":dict(j)}

@app.post("/worker/jobs/{jid}")
def worker_update(jid:str, body:WorkerUpdate, authorization: Optional[str]=Header(None)):
    auth_worker(authorization)
    with lock:
        j=jobs.get(jid)
        if not j: raise HTTPException(404,"Job not found")
        data=body.model_dump(exclude_none=True)
        if "progress" in data: data["progress"]=max(0,min(100,float(data["progress"])))
        j.update(data); j["updated_at"]=now()
        if j.get("r2_key") and j.get("status")=="done":
            try: j["download_url"]=presign(j["r2_key"])
            except Exception as e: j["error"]=f"R2 link error: {e}"
        return public_job(j)

static=Path(__file__).parent/"static"
app.mount("/static", StaticFiles(directory=static), name="static")

@app.get("/")
def index(): return FileResponse(static/"index.html")
