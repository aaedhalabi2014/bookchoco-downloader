from __future__ import annotations

import ipaddress
import socket
from typing import Any
from urllib.parse import urlsplit

import yt_dlp
from yt_dlp.utils import DownloadError

from .config import settings


_POT_PROVIDER_URL = "http://127.0.0.1:4416"


class AnalyzeFailed(Exception):
    pass


def _is_private_or_special(ip_text: str) -> bool:
    ip = ipaddress.ip_address(ip_text)
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
        )
    )


def validate_extracted_url(raw_url: str) -> str:
    parts = urlsplit(raw_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise AnalyzeFailed("رابط الوسائط الناتج غير صالح.")
    hostname = parts.hostname.lower().rstrip(".")
    try:
        addresses = socket.getaddrinfo(hostname, parts.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise AnalyzeFailed("تعذر التحقق من خادم الفيديو.") from exc
    resolved = {item[4][0] for item in addresses}
    if not resolved or any(_is_private_or_special(ip) for ip in resolved):
        raise AnalyzeFailed("تم رفض رابط الوسائط لأسباب أمنية.")
    return raw_url


def _clean_headers(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    allowed = {"user-agent", "referer", "origin", "accept", "accept-language"}
    clean: dict[str, str] = {}
    for key, raw in value.items():
        name = str(key).strip().lower()
        text = str(raw).strip()
        if name in allowed and text and len(text) < 2048:
            clean[name] = text
    return clean


def _fmt_size(fmt: dict[str, Any]) -> int:
    return int(fmt.get("filesize") or fmt.get("filesize_approx") or 0)


def _quality_label(height: int, width: int, note: str) -> str:
    if height:
        return f"{height}p"
    if width:
        return f"{width}px"
    return note.strip()[:32] or "أفضل جودة"


def _base_opts(platform: str) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 2,
        "extract_flat": False,
    }
    if platform == "YouTube":
        opts["extractor_args"] = {
            "youtube": ["player_client=mweb"],
            "youtubepot-bgutilhttp": [f"base_url={_POT_PROVIDER_URL}"],
        }
        proxy = settings.youtube_proxy_url.strip()
        if proxy:
            opts["proxy"] = proxy
        else:
            opts["source_address"] = "0.0.0.0"
    return opts


def analyze_media(url: str, platform: str) -> dict[str, Any]:
    try:
        with yt_dlp.YoutubeDL(_base_opts(platform)) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        raise AnalyzeFailed(str(exc)) from exc

    if not isinstance(info, dict):
        raise AnalyzeFailed("تعذر قراءة معلومات الفيديو.")

    title = str(info.get("title") or "video").strip()[:180] or "video"
    base_headers = _clean_headers(info.get("http_headers"))
    choices: list[dict[str, Any]] = []

    for fmt in info.get("formats") or []:
        if not isinstance(fmt, dict):
            continue
        direct_url = str(fmt.get("url") or "").strip()
        if not direct_url.startswith(("http://", "https://")):
            continue

        vcodec = str(fmt.get("vcodec") or "none")
        acodec = str(fmt.get("acodec") or "none")
        if vcodec == "none" or acodec == "none":
            continue

        protocol = str(fmt.get("protocol") or "").lower()
        if "m3u8" in protocol or "dash" in protocol:
            continue

        ext = str(fmt.get("ext") or "").lower()
        if ext not in {"mp4", "m4v"}:
            continue

        try:
            validate_extracted_url(direct_url)
        except AnalyzeFailed:
            continue

        height = int(fmt.get("height") or 0)
        width = int(fmt.get("width") or 0)
        fps = float(fmt.get("fps") or 0)
        tbr = float(fmt.get("tbr") or 0)
        size = _fmt_size(fmt)
        format_id = str(fmt.get("format_id") or "").strip()
        if not format_id:
            continue

        headers = dict(base_headers)
        headers.update(_clean_headers(fmt.get("http_headers")))

        choices.append(
            {
                "id": format_id,
                "label": _quality_label(height, width, str(fmt.get("format_note") or "")),
                "height": height,
                "width": width,
                "fps": fps,
                "tbr": tbr,
                "filesize": size,
                "ext": ext,
                "url": direct_url,
                "headers": headers,
            }
        )

    by_key: dict[tuple[int, int], dict[str, Any]] = {}
    for item in choices:
        key = (int(item["height"]), int(round(float(item["fps"]) or 0)))
        score = (int(item["height"]), float(item["tbr"]), int(item["filesize"]))
        current = by_key.get(key)
        if current is None:
            by_key[key] = item
            continue
        current_score = (
            int(current["height"]),
            float(current["tbr"]),
            int(current["filesize"]),
        )
        if score > current_score:
            by_key[key] = item

    formats = sorted(
        by_key.values(),
        key=lambda item: (int(item["height"]), float(item["tbr"]), int(item["filesize"])),
        reverse=True,
    )[:8]

    return {"title": title, "formats": formats}
