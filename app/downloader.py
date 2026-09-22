from __future__ import annotations

import re
import shutil
import threading
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.utils import DownloadError

from .config import DOWNLOAD_DIR, settings
from .jobs import update_job


_DOWNLOAD_SEMAPHORE = threading.BoundedSemaphore(settings.max_concurrent_downloads)
_SAFE_FILENAME_RE = re.compile(r"[^\w\-. ()\[\]\u0600-\u06FF]+", re.UNICODE)


class DownloadTooLarge(Exception):
    pass


class DownloadFailed(Exception):
    pass


def _clean_title(value: str | None) -> str:
    value = (value or "video").strip()
    value = _SAFE_FILENAME_RE.sub("_", value)
    return value[:90].strip(" ._") or "video"


def _select_output(job_dir: Path) -> Path:
    candidates = [
        p
        for p in job_dir.iterdir()
        if p.is_file() and p.suffix.lower() not in {".part", ".ytdl", ".json", ".jpg", ".jpeg", ".png", ".webp"}
    ]
    if not candidates:
        raise DownloadFailed("لم يتم العثور على ملف فيديو نهائي.")
    return max(candidates, key=lambda p: p.stat().st_size)


def download_video(job_id: str, url: str, platform: str) -> None:
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    last_progress = -1

    def hook(data: dict[str, Any]) -> None:
        nonlocal last_progress
        status = data.get("status")
        if status == "downloading":
            downloaded = int(data.get("downloaded_bytes") or 0)
            total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
            if downloaded > settings.max_video_bytes:
                raise DownloadTooLarge
            progress = min(94, int(downloaded * 94 / total)) if total else 10
            if progress >= last_progress + 2:
                last_progress = progress
                update_job(job_id, status="downloading", progress=progress)
        elif status == "finished":
            update_job(job_id, status="processing", progress=95)

    outtmpl = str(job_dir / "%(id)s.%(ext)s")
    max_height = settings.download_max_height
    ydl_opts: dict[str, Any] = {
        "outtmpl": outtmpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "restrictfilenames": False,
        "progress_hooks": [hook],
        "retries": 2,
        "fragment_retries": 2,
        "socket_timeout": 20,
        "concurrent_fragment_downloads": 2,
        "max_filesize": settings.max_video_bytes,
        "merge_output_format": "mp4",
        "format": (
            f"bv*[height<={max_height}][ext=mp4]+ba[ext=m4a]/"
            f"b[height<={max_height}][ext=mp4]/"
            f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b"
        ),
        "postprocessors": [
            {"key": "FFmpegMetadata", "add_metadata": False},
        ],
    }

    acquired = _DOWNLOAD_SEMAPHORE.acquire(timeout=120)
    if not acquired:
        update_job(job_id, status="error", error="الخادم مشغول حاليًا. حاول بعد قليل.")
        return

    try:
        update_job(job_id, status="preparing", progress=3)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            clean = ydl.sanitize_info(info)

        output = _select_output(job_dir)
        if output.stat().st_size > settings.max_video_bytes:
            raise DownloadTooLarge

        title = _clean_title(clean.get("title") if isinstance(clean, dict) else None)
        extension = output.suffix.lower() or ".mp4"
        final_name = f"{title}{extension}"
        final_path = job_dir / final_name
        if final_path != output:
            if final_path.exists():
                final_path.unlink()
            output.replace(final_path)

        update_job(
            job_id,
            status="ready",
            progress=100,
            platform=platform,
            title=title,
            filename=final_name,
            file_path=str(final_path),
            error=None,
        )
    except DownloadTooLarge:
        shutil.rmtree(job_dir, ignore_errors=True)
        update_job(job_id, status="error", error=f"حجم الفيديو يتجاوز الحد المسموح ({settings.max_video_mb} MB).")
    except DownloadError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        message = str(exc).lower()
        if any(token in message for token in ("login", "private", "sign in", "cookies", "age")):
            user_error = "الفيديو غير متاح كرابط عام، أو يحتاج تسجيل دخول."
        elif "unsupported url" in message:
            user_error = "هذا الرابط غير مدعوم حاليًا."
        else:
            user_error = "تعذر تجهيز الفيديو من هذا الرابط. قد تكون المنصة غيّرت طريقة الوصول إليه."
        update_job(job_id, status="error", error=user_error)
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        update_job(job_id, status="error", error="حدث خطأ غير متوقع أثناء تجهيز الفيديو.")
    finally:
        _DOWNLOAD_SEMAPHORE.release()
