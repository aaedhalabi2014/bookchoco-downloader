from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import httpx

from .config import BASE_DIR, settings
from .downloader import download_video
from .jobs import (
    create_analysis,
    create_job,
    delete_analysis,
    delete_job,
    get_analysis,
    get_job,
    init_db,
    list_analyses,
    list_jobs,
    remove_job_file,
    update_job,
)
from .security import URLValidationError, validate_public_media_url
from .resolver import AnalyzeFailed, analyze_media, validate_extracted_url


STATIC_DIR = BASE_DIR / "static"
_RATE_BUCKETS: dict[str, deque[float]] = defaultdict(deque)
_RATE_LOCK = Lock()


class CreateJobRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class ResolveRequest(BaseModel):
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


def _file_size(job) -> int:
    if not job.file_path:
        return 0
    try:
        path = Path(job.file_path)
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _public_job(job) -> dict[str, object]:
    ready = job.status in {"ready", "served"}
    return {
        "id": job.id,
        "status": job.status,
        "progress": job.progress,
        "platform": job.platform,
        "title": job.title,
        "filename": job.filename,
        "error": job.error,
        "phase": job.phase,
        "downloaded_bytes": job.downloaded_bytes,
        "total_bytes": job.total_bytes,
        "speed_bps": job.speed_bps,
        "eta_seconds": job.eta_seconds,
        "file_size_bytes": _file_size(job) if ready else 0,
        "ready": ready,
        "download_url": f"/api/jobs/{job.id}/download" if ready else None,
    }


async def cleanup_loop() -> None:
    while True:
        try:
            now = datetime.now(timezone.utc)
            for analysis in list_analyses():
                try:
                    created = datetime.fromisoformat(analysis.created_at)
                    if (now - created).total_seconds() > 15 * 60:
                        delete_analysis(analysis.id)
                except ValueError:
                    delete_analysis(analysis.id)

            for job in list_jobs():
                reference_time = job.updated_at if job.status == "served" else job.created_at
                try:
                    reference = datetime.fromisoformat(reference_time)
                    relevant_age = (now - reference).total_seconds() / 60
                except ValueError:
                    relevant_age = settings.job_ttl_minutes + 1
                ttl = 30 if job.status == "served" else settings.job_ttl_minutes
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
            update_job(job.id, status="error", phase="error", error="توقفت عملية سابقة قبل اكتمالها. أعد المحاولة.")
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
    allow_methods=["GET", "HEAD", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Range"],
    expose_headers=["Content-Length", "Content-Disposition", "Accept-Ranges", "Content-Range"],
    max_age=86400,
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



@app.post("/api/resolve")
def resolve_media(payload: ResolveRequest, request: Request):
    client_ip = request.client.host if request.client else "unknown"
    if _rate_limited(client_ip):
        raise HTTPException(status_code=429, detail="طلبات كثيرة خلال وقت قصير. حاول بعد دقيقة.")

    try:
        valid = validate_public_media_url(payload.url)
        analyzed = analyze_media(valid.url, valid.platform)
    except URLValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AnalyzeFailed as exc:
        raise HTTPException(status_code=422, detail="تعذر تحليل هذا الرابط بسرعة؛ استخدم التجهيز الكامل.") from exc

    analysis_id = uuid.uuid4().hex
    stored = json.dumps(analyzed.get("formats") or [], ensure_ascii=False, separators=(",", ":"))
    create_analysis(
        analysis_id,
        valid.url,
        valid.platform,
        str(analyzed.get("title") or "video"),
        stored,
    )
    public_formats = []
    for item in analyzed.get("formats") or []:
        public_formats.append(
            {
                "id": item["id"],
                "label": item["label"],
                "height": item["height"],
                "width": item["width"],
                "fps": item["fps"],
                "filesize": item["filesize"],
                "ext": item["ext"],
            }
        )

    return {
        "id": analysis_id,
        "platform": valid.platform,
        "title": str(analyzed.get("title") or "video"),
        "formats": public_formats,
        "fallback": len(public_formats) == 0,
    }


def _safe_filename(value: str, ext: str = "mp4") -> str:
    name = re.sub(r"[^\w\-. ()\[\]\u0600-\u06FF]+", "_", value, flags=re.UNICODE)
    name = name[:90].strip(" ._") or "video"
    return f"{name}.{ext}"


@app.get("/api/resolve/{analysis_id}/download/{format_id}")
def resolved_download(analysis_id: str, format_id: str, request: Request, preview: bool = False):
    analysis = get_analysis(analysis_id)
    if not analysis:
        raise HTTPException(status_code=404, detail="انتهت صلاحية خيارات التحميل. أعد تحليل الرابط.")

    try:
        created = datetime.fromisoformat(analysis.created_at)
        if (datetime.now(timezone.utc) - created).total_seconds() > 15 * 60:
            delete_analysis(analysis_id)
            raise HTTPException(status_code=410, detail="انتهت صلاحية خيارات التحميل. أعد تحليل الرابط.")
    except ValueError as exc:
        delete_analysis(analysis_id)
        raise HTTPException(status_code=410, detail="انتهت صلاحية خيارات التحميل.") from exc

    try:
        formats = json.loads(analysis.formats_json)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail="بيانات الجودة غير صالحة.") from exc

    selected = next((item for item in formats if str(item.get("id")) == format_id), None)
    if not selected:
        raise HTTPException(status_code=404, detail="هذه الجودة لم تعد متاحة.")

    upstream_url = validate_extracted_url(str(selected.get("url") or ""))
    upstream_headers = {
        str(k): str(v)
        for k, v in (selected.get("headers") or {}).items()
        if isinstance(k, str) and isinstance(v, str)
    }
    if request.headers.get("range"):
        upstream_headers["range"] = request.headers["range"]

    client = httpx.Client(follow_redirects=True, timeout=httpx.Timeout(30.0, read=120.0))
    try:
        cm = client.stream("GET", upstream_url, headers=upstream_headers)
        upstream = cm.__enter__()
    except Exception as exc:
        client.close()
        raise HTTPException(status_code=502, detail="تعذر فتح ملف الفيديو من المصدر.") from exc

    if upstream.status_code not in {200, 206}:
        upstream.close()
        cm.__exit__(None, None, None)
        client.close()
        raise HTTPException(status_code=502, detail="المصدر رفض تنزيل هذه الجودة.")

    response_headers = {
        "Cache-Control": "private, no-store",
        "Accept-Ranges": upstream.headers.get("accept-ranges", "bytes"),
    }
    for header in ("content-length", "content-range", "etag", "last-modified"):
        if upstream.headers.get(header):
            response_headers[header.title()] = upstream.headers[header]

    ext = str(selected.get("ext") or "mp4").lower()
    filename = _safe_filename(analysis.title, ext)
    disposition = "inline" if preview else "attachment"
    response_headers["Content-Disposition"] = f'{disposition}; filename="{filename.encode("ascii", "ignore").decode() or "video.mp4"}"'

    def iterator():
        try:
            for chunk in upstream.iter_bytes(256 * 1024):
                if chunk:
                    yield chunk
        finally:
            upstream.close()
            cm.__exit__(None, None, None)
            client.close()

    return StreamingResponse(
        iterator(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type") or "video/mp4",
        headers=response_headers,
    )

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
def download(job_id: str, preview: bool = False):
    job = get_job(job_id)
    if not job or job.status not in {"ready", "served"} or not job.file_path:
        raise HTTPException(status_code=404, detail="الملف غير جاهز أو انتهت صلاحيته.")

    path = Path(job.file_path)
    if not path.exists() or not path.is_file():
        delete_job(job_id)
        raise HTTPException(status_code=410, detail="انتهت صلاحية الملف. أعد تجهيز الرابط.")

    update_job(job_id, status="served", phase="ready")

    suffix = path.suffix.lower()
    media_type = {
        ".mp4": "video/mp4",
        ".mov": "video/quicktime",
        ".m4v": "video/x-m4v",
        ".webm": "video/webm",
    }.get(suffix, "application/octet-stream")

    headers = {"Cache-Control": "private, no-store", "Accept-Ranges": "bytes"}
    if not preview:
        headers["X-Download-Options"] = "noopen"

    return FileResponse(
        path,
        filename=job.filename or "video.mp4",
        media_type=media_type,
        content_disposition_type="inline" if preview else "attachment",
        headers=headers,
    )


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
