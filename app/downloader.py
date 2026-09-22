from __future__ import annotations

import json
import re
import shutil
import struct
import subprocess
import threading
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.utils import DownloadError

from .config import DOWNLOAD_DIR, settings
from .jobs import update_job


_DOWNLOAD_SEMAPHORE = threading.BoundedSemaphore(settings.max_concurrent_downloads)
_TRANSCODE_SEMAPHORE = threading.BoundedSemaphore(1)
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


def _probe_codecs(path: Path) -> tuple[str | None, str | None, str | None]:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "stream=codec_type,codec_name,pix_fmt",
                "-of", "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(result.stdout or "{}")
        video_codec = None
        audio_codec = None
        pixel_format = None
        for stream in payload.get("streams", []):
            if stream.get("codec_type") == "video" and video_codec is None:
                video_codec = stream.get("codec_name")
                pixel_format = stream.get("pix_fmt")
            elif stream.get("codec_type") == "audio" and audio_codec is None:
                audio_codec = stream.get("codec_name")
        return video_codec, audio_codec, pixel_format
    except Exception:
        return None, None, None


def _mp4_has_faststart(path: Path) -> bool:
    """Return True when the MP4 moov atom is before mdat without reading the file into memory."""
    if path.suffix.lower() not in {".mp4", ".m4v", ".mov"}:
        return False
    try:
        file_size = path.stat().st_size
        with path.open("rb") as stream:
            offset = 0
            for _ in range(128):
                if offset + 8 > file_size:
                    break
                stream.seek(offset)
                header = stream.read(8)
                if len(header) != 8:
                    break
                size = struct.unpack(">I", header[:4])[0]
                box_type = header[4:8]
                header_size = 8
                if size == 1:
                    extended = stream.read(8)
                    if len(extended) != 8:
                        return False
                    size = struct.unpack(">Q", extended)[0]
                    header_size = 16
                elif size == 0:
                    size = file_size - offset
                if size < header_size or offset + size > file_size:
                    return False
                if box_type == b"moov":
                    return True
                if box_type == b"mdat":
                    return False
                offset += size
    except OSError:
        pass
    return False


def _make_ios_compatible(path: Path, job_id: str) -> Path:
    """Normalize only when the source is not already Safari/Photos friendly."""
    video_codec, audio_codec, pixel_format = _probe_codecs(path)
    safe_codecs = (
        path.suffix.lower() == ".mp4"
        and video_codec in {"h264", "hevc"}
        and audio_codec in {None, "aac"}
        and (
            pixel_format is None
            or pixel_format in {"yuv420p", "yuvj420p", "yuv420p10le"}
        )
    )

    # Safari/Photos can seek MP4 files over HTTP Range requests even when the
    # moov atom is not at the front. Do not rewrite an already compatible file:
    # that extra full-file copy was the main cause of long 99% waits on Render.
    if safe_codecs:
        return path

    update_job(
        job_id,
        status="processing",
        phase="processing",
        progress=99,
        speed_bps=None,
        eta_seconds=None,
    )

    temp = path.with_name(f"{path.stem}.ios.mp4")
    if safe_codecs:
        command = [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(path),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c", "copy",
            "-movflags", "+faststart",
            str(temp),
        ]
    else:
        command = [
            "ffmpeg", "-y", "-v", "error",
            "-i", str(path),
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "25",
            "-threads", "0",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=-2:min(1280\\,ih)",
            "-c:a", "aac",
            "-b:a", "160k",
            "-movflags", "+faststart",
            str(temp),
        ]

    acquired = _TRANSCODE_SEMAPHORE.acquire(timeout=300)
    if not acquired:
        return path
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=240,
        )
        if temp.exists() and temp.stat().st_size > 0:
            path.unlink(missing_ok=True)
            return temp
    except Exception:
        temp.unlink(missing_ok=True)
    finally:
        _TRANSCODE_SEMAPHORE.release()

    return path


def download_video(job_id: str, url: str, platform: str) -> None:
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    last_progress = -1
    stream_state: dict[str, dict[str, int]] = {}
    expected_total_bytes = 0

    def _size_of(fmt: dict[str, Any]) -> int:
        return int(fmt.get("filesize") or fmt.get("filesize_approx") or 0)

    def _discover_expected_total(info: dict[str, Any]) -> int:
        requested = info.get("requested_formats") or info.get("requested_downloads") or []
        if isinstance(requested, list):
            total = sum(_size_of(fmt) for fmt in requested if isinstance(fmt, dict))
            if total > 0:
                return total
        return _size_of(info)

    def hook(data: dict[str, Any]) -> None:
        nonlocal last_progress, expected_total_bytes
        status = data.get("status")
        info = data.get("info_dict") if isinstance(data.get("info_dict"), dict) else {}

        if status == "downloading":
            downloaded = int(data.get("downloaded_bytes") or 0)
            total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)

            discovered = _discover_expected_total(info)
            if discovered > expected_total_bytes:
                expected_total_bytes = discovered

            format_id = str(info.get("format_id") or "")
            filename = str(data.get("filename") or data.get("tmpfilename") or "")
            stream_key = f"{filename}::{format_id}" or "current"
            stream_state[stream_key] = {"downloaded": downloaded, "total": total}

            aggregate_downloaded = sum(item["downloaded"] for item in stream_state.values())
            known_total = sum(item["total"] for item in stream_state.values() if item["total"] > 0)
            aggregate_total = max(expected_total_bytes, known_total, total)

            if aggregate_downloaded > settings.max_video_bytes:
                raise DownloadTooLarge

            progress = (
                min(100, int(aggregate_downloaded * 100 / aggregate_total))
                if aggregate_total > 0
                else max(1, last_progress)
            )
            speed = float(data.get("speed") or 0) or None
            eta = None
            if speed and aggregate_total > aggregate_downloaded:
                eta = int((aggregate_total - aggregate_downloaded) / speed)
            elif data.get("eta") is not None:
                eta = int(data.get("eta") or 0)

            # Keep the ring monotonic even when YouTube downloads video and
            # audio as separate streams. The byte counters remain raw telemetry.
            progress = max(last_progress, progress)
            if progress != last_progress or downloaded == total:
                last_progress = progress
                update_job(
                    job_id,
                    status="downloading",
                    phase="downloading",
                    progress=progress,
                    downloaded_bytes=aggregate_downloaded,
                    total_bytes=aggregate_total,
                    speed_bps=speed,
                    eta_seconds=eta,
                )

        elif status == "finished":
            downloaded = int(data.get("downloaded_bytes") or 0)
            total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or downloaded)
            format_id = str(info.get("format_id") or "")
            filename = str(data.get("filename") or data.get("tmpfilename") or "")
            stream_key = f"{filename}::{format_id}" or "current"
            stream_state[stream_key] = {
                "downloaded": max(downloaded, total),
                "total": max(total, downloaded),
            }

    def postprocessor_hook(data: dict[str, Any]) -> None:
        status = data.get("status")
        if status == "started":
            update_job(
                job_id,
                status="processing",
                phase="processing",
                progress=99,
                speed_bps=None,
                eta_seconds=None,
            )

    max_height = settings.download_max_height

    def base_opts() -> dict[str, Any]:
        # Prefer a progressive H.264/AAC MP4 at 720p+ when available. This
        # removes a separate audio/video merge from the fast path while keeping
        # a compatible 1080p fallback.
        format_selector = (
            f"b[vcodec^=avc1][acodec^=mp4a][height<={max_height}][ext=mp4]/"
            f"bv*[vcodec^=avc1][height<={max_height}][ext=mp4]+ba[acodec^=mp4a][ext=m4a]/"
            f"b[vcodec^=avc1][acodec^=mp4a][height<={max_height}][ext=mp4]/"
            f"bv*[height<={max_height}][ext=mp4]+ba[ext=m4a]/"
            f"b[height<={max_height}][ext=mp4]/"
            f"bv*[height<={max_height}]+ba/b[height<={max_height}]/b"
        )
        return {
            "outtmpl": str(job_dir / "%(id)s.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "restrictfilenames": False,
            "progress_hooks": [hook],
            "postprocessor_hooks": [postprocessor_hook],
            "retries": 3,
            "fragment_retries": 3,
            "socket_timeout": 30,
            "concurrent_fragment_downloads": 4,
            "max_filesize": settings.max_video_bytes,
            "merge_output_format": "mp4",
            "format": format_selector,
        }

    attempts: list[dict[str, Any]] = [base_opts()]

    if platform == "YouTube":
        # Current yt-dlp guidance recommends mweb + a PO-token provider.
        # bgutil is running locally on 127.0.0.1:4416 in the same container.
        pot = base_opts()
        pot["extractor_args"] = {
            "youtube": ["player_client=mweb"],
            "youtubepot-bgutilhttp": [f"base_url={_POT_PROVIDER_URL}"],
        }

        # If the provider/client combination is temporarily affected by a
        # YouTube experiment, fall back to yt-dlp's default clients next.
        default_clients = base_opts()

        # Final fallback tries clients that sometimes expose different playback
        # paths, including HLS on Safari.
        hls = base_opts()
        hls["extractor_args"] = {
            "youtube": ["player_client=web_safari,android_vr,web_embedded"],
            "youtubepot-bgutilhttp": [f"base_url={_POT_PROVIDER_URL}"],
        }
        hls["format"] = (
            f"b[protocol^=m3u8][vcodec^=avc1][height<={max_height}]/"
            f"b[protocol^=m3u8][height<={max_height}]/"
            f"bv*[protocol^=m3u8][height<={max_height}]+ba[protocol^=m3u8]/"
            f"b[height<={max_height}]/b"
        )

        pot_legacy = base_opts()
        pot_legacy["extractor_args"] = {
            "youtube": ["player_client=mweb"],
            "youtubepot-bgutilhttp": [
                f"base_url={_POT_PROVIDER_URL};disable_innertube=1"
            ],
        }

        web_pot = base_opts()
        web_pot["extractor_args"] = {
            "youtube": ["player_client=web"],
            "youtubepot-bgutilhttp": [f"base_url={_POT_PROVIDER_URL}"],
        }

        # Route YouTube only through an optional residential/ISP proxy.
        # Keeping the same proxy for extraction and media downloading avoids
        # mixing the Render datacenter IP with the proxy IP during one job.
        youtube_proxy = settings.youtube_proxy_url.strip()
        for attempt in (pot, pot_legacy, web_pot, default_clients, hls):
            if youtube_proxy:
                attempt["proxy"] = youtube_proxy
            else:
                attempt["source_address"] = "0.0.0.0"

        attempts = [pot, pot_legacy, web_pot, default_clients, hls]

    acquired = _DOWNLOAD_SEMAPHORE.acquire(timeout=300)
    if not acquired:
        update_job(job_id, status="error", error="الخادم مشغول حاليًا. حاول بعد قليل.")
        return

    try:
        clean: dict[str, Any] | None = None
        last_error: DownloadError | None = None

        for index, ydl_opts in enumerate(attempts):
            if index:
                _reset_job_dir(job_dir)
                stream_state.clear()
                expected_total_bytes = 0
                last_progress = -1

            update_job(
                job_id,
                status="preparing",
                phase="preparing",
                progress=0,
                downloaded_bytes=0,
                total_bytes=0,
                speed_bps=None,
                eta_seconds=None,
            )

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

        output = _make_ios_compatible(output, job_id)
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

        final_size = final_path.stat().st_size
        update_job(
            job_id,
            status="ready",
            progress=100,
            phase="ready",
            downloaded_bytes=final_size,
            total_bytes=final_size,
            speed_bps=None,
            eta_seconds=0,
            platform=platform,
            title=title,
            filename=final_name,
            file_path=str(final_path),
            error=None,
        )
    except DownloadTooLarge:
        shutil.rmtree(job_dir, ignore_errors=True)
        update_job(job_id, status="error", phase="error", error=f"حجم الفيديو يتجاوز الحد المسموح ({settings.max_video_mb} MB).")
    except DownloadError as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        message = str(exc).lower()

        if platform == "YouTube" and any(
            token in message
            for token in ("confirm you're not a bot", "login_required", "po token", "http error 403")
        ):
            if settings.youtube_proxy_url.strip():
                user_error = (
                    "YouTube رفض عنوان البروكسي الحالي. جرّب تبديل IP البروكسي أو جلسة جديدة."
                )
            else:
                user_error = (
                    "YouTube يرفض عنوان خادم Render. فعّل YOUTUBE_PROXY_URL ببروكسي ISP/Residential."
                )
        elif any(
            token in message
            for token in (
                "private video",
                "this video is private",
                "members-only",
                "members only",
                "age-restricted",
            )
        ):
            user_error = "الفيديو يحتاج صلاحية فعلية أو تسجيل دخول ولا يمكن تنزيله كرابط عام."
        elif "unsupported url" in message:
            user_error = "هذا الرابط غير مدعوم حاليًا."
        else:
            user_error = "تعذر تجهيز الفيديو من هذا الرابط. قد تكون المنصة غيّرت طريقة الوصول إليه."

        update_job(job_id, status="error", phase="error", error=user_error)
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        update_job(job_id, status="error", phase="error", error="حدث خطأ غير متوقع أثناء تجهيز الفيديو.")
    finally:
        _DOWNLOAD_SEMAPHORE.release()
