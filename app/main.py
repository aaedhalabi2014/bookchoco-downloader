from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import BASE_DIR, settings
from .downloader import download_video
from .jobs import create_job, delete_job, get_job, init_db, list_jobs, remove_job_file, update_job
from .security import URLValidationError, validate_public_media_url


STATIC_DIR = BASE_DIR / "static"
_RATE_BUCKETS: dict[str, deque[float]] = defaultdict(deque)
_RATE_LOCK = Lock()


class CreateJobRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


def _rate_limited(client_ip: str) -> bool:
    now = time.monotonic()
    cutoff = now - settings.rate_limit_window_seconds
    with _RATE_LOCK:
        bucket = _RATE_BUCKETS[client_ip]
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= settings.rate_limit_requests:
            return True
        bucket.append(now)
        return False


def _public_job(job) -> dict[str, object]:
    return {
        "id": job.id,
        "status": job.status,
        "progress": job.progress,
        "platform": job.platform,
        "title": job.title,
        "filename": job.filename,
        "error": job.error,
        "ready": job.status in {"ready", "served"},
        "download_url": f"/api/jobs/{job.id}/download" if job.status in {"ready", "served"} else None,
    }


async def cleanup_loop() -> None:
    while True:
        try:
            now = datetime.now(timezone.utc)
            for job in list_jobs():
                reference_time = job.updated_at if job.status == "served" else job.created_at
                try:
                    reference = datetime.fromisoformat(reference_time)
                    relevant_age = (now - reference).total_seconds() / 60
                except ValueError:
                    relevant_age = settings.job_ttl_minutes + 1
                ttl = 10 if job.status == "served" else settings.job_ttl_minutes
                if relevant_age > ttl:
                    remove_job_file(job)
                    delete_job(job.id)
        finally:
            await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    for job in list_jobs():
        if job.status not in {"ready", "error"}:
            update_job(job.id, status="error", error="توقفت عملية سابقة قبل اكتمالها. أعد المحاولة.")
    cleaner = asyncio.create_task(cleanup_loop())
    try:
        yield
    finally:
        cleaner.cancel()


app = FastAPI(
    title=settings.app_name,
    docs_url=None if settings.app_env == "production" else "/docs",
    redoc_url=None,
    openapi_url=None if settings.app_env == "production" else "/openapi.json",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response: Response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self'; manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    )
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/jobs", status_code=202)
def new_job(payload: CreateJobRequest, request: Request, background_tasks: BackgroundTasks):
    client_ip = request.client.host if request.client else "unknown"
    if _rate_limited(client_ip):
        raise HTTPException(status_code=429, detail="طلبات كثيرة خلال وقت قصير. حاول بعد دقيقة.")

    try:
        valid = validate_public_media_url(payload.url)
    except URLValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_id = uuid.uuid4().hex
    job = create_job(job_id, valid.platform)
    background_tasks.add_task(download_video, job_id, valid.url, valid.platform)
    return _public_job(job)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="انتهت صلاحية العملية أو لم تعد موجودة.")
    return _public_job(job)


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str):
    job = get_job(job_id)
    if not job or job.status not in {"ready", "served"} or not job.file_path:
        raise HTTPException(status_code=404, detail="الملف غير جاهز أو انتهت صلاحيته.")

    path = Path(job.file_path)
    if not path.exists() or not path.is_file():
        delete_job(job_id)
        raise HTTPException(status_code=410, detail="انتهت صلاحية الملف. أعد تجهيز الرابط.")

    update_job(job_id, status="served")

    return FileResponse(
        path,
        filename=job.filename or "video.mp4",
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store", "X-Download-Options": "noopen"},
    )


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
