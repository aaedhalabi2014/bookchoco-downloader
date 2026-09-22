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
_POT_PROVIDER_URL = "http://127.0.0.1:4416"


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


def _reset_job_dir(job_dir: Path) -> None:
    shutil.rmtree(job_dir, ignore_errors=True)
    job_dir.mkdir(parents=True, exist_ok=True)


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

    max_height = settings.download_max_height

    def base_opts() -> dict[str, Any]:
        return {
            "outtmpl": str(job_dir / "%(id)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": False,
            "progress_hooks": [hook],
            "retries": 2,
            "fragment_retries": 2,
            "socket_timeout": 25,
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

    attempts: list[dict[str, Any]] = [base_opts()]

    if platform == "YouTube":
        # Current yt-dlp guidance recommends mweb + a PO-token provider.
        # bgutil is running locally on 127.0.0.1:4416 in the same container.
        pot = base_opts()
        pot["extractor_args"] = {
            "youtube": {
                "player_client": ["mweb"],
            },
            "youtubepot-bgutilhttp": {
                "base_url": [_POT_PROVIDER_URL],
            },
        }

        # If the provider/client combination is temporarily affected by a
        # YouTube experiment, fall back to yt-dlp's default clients next.
        default_clients = base_opts()

        # Final fallback tries clients that sometimes expose different playback
        # paths, including HLS on Safari.
        hls = base_opts()
        hls["extractor_args"] = {
            "youtube": {
                "player_client": ["web_safari", "android_vr", "web_embedded"],
            },
            "youtubepot-bgutilhttp": {
                "base_url": [_POT_PROVIDER_URL],
            },
        }
        hls["format"] = (
            f"b[protocol^=m3u8][height<={max_height}]/"
            f"bv*[protocol^=m3u8][height<={max_height}]+ba[protocol^=m3u8]/"
            f"b[height<={max_height}]/b"
        )

        attempts = [pot, default_clients, hls]

    acquired = _DOWNLOAD_SEMAPHORE.acquire(timeout=120)
    if not acquired:
        update_job(job_id, status="error", error="الخادم مشغول حاليًا. حاول بعد قليل.")
        return

    try:
        clean: dict[str, Any] | None = None
        last_error: DownloadError | None = None

        for index, ydl_opts in enumerate(attempts):
            if index:
                _reset_job_dir(job_dir)
                last_progress = -1

            update_job(job_id, status="preparing", progress=3)

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    sanitized = ydl.sanitize_info(info)
                    clean = sanitized if isinstance(sanitized, dict) else None
                last_error = None
                break
            except DownloadError as exc:
                last_error = exc
                if platform != "YouTube" or index == len(attempts) - 1:
                    raise

        if last_error is not None:
            raise last_error

        output = _select_output(job_dir)
        if output.stat().st_size > settings.max_video_bytes:
            raise DownloadTooLarge

        title = _clean_title(clean.get("title") if clean else None)
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

        if platform == "YouTube" and any(
            token in message
            for token in ("confirm you're not a bot", "login_required", "po token", "http error 403")
        ):
            user_error = (
                "YouTube ما زال يرفض عنوان خادم Render لهذا الرابط رغم محاولة التحقق الآلية. "
                "هذا ليس لأن الفيديو خاص."
            )
        elif any(token in message for token in ("private", "members-only", "age-restricted", "cookies")):
            user_error = "الفيديو يحتاج صلاحية أو تسجيل دخول ولا يمكن تنزيله كرابط عام."
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
