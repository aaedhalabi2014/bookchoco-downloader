from __future__ import annotations

import html
import ipaddress
import json
import re
import socket
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
try:
    from curl_cffi import requests as curl_requests
except Exception:  # optional runtime acceleration/impersonation
    curl_requests = None
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




_FACEBOOK_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 "
    "Mobile/15E148 Safari/604.1"
)


def _decode_fb_value(raw: str) -> str:
    value = html.unescape(raw)
    try:
        return json.loads(f'"{value}"')
    except Exception:
        return (
            value.replace(r"\/", "/")
            .replace(r"\u0025", "%")
            .replace(r"\u0026", "&")
            .replace(r"\u003D", "=")
            .replace(r"\u003F", "?")
        )


def _facebook_variant_urls(page: str) -> list[tuple[str, str]]:
    patterns = [
        ("HD", r'"browser_native_hd_url"\s*:\s*"([^"]+)"'),
        ("HD", r'"playable_url_quality_hd"\s*:\s*"([^"]+)"'),
        ("HD", r'"hd_src"\s*:\s*"([^"]+)"'),
        ("HD", r'"hd_src_no_ratelimit"\s*:\s*"([^"]+)"'),
        ("SD", r'"browser_native_sd_url"\s*:\s*"([^"]+)"'),
        ("SD", r'"playable_url"\s*:\s*"([^"]+)"'),
        ("SD", r'"sd_src"\s*:\s*"([^"]+)"'),
        ("SD", r'"sd_src_no_ratelimit"\s*:\s*"([^"]+)"'),
        ("MP4", r'<video[^>]+src=["\']([^"\']+\.mp4[^"\']*)["\']'),
        ("MP4", r'"(?:video_url|videoURL|src)"\s*:\s*"([^"]*fbcdn[^"]*\.mp4[^"]*)"'),
    ]
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for label, pattern in patterns:
        for match in re.finditer(pattern, page, flags=re.IGNORECASE):
            url = _decode_fb_value(match.group(1)).strip()
            if not url.startswith(("http://", "https://")) or url in seen:
                continue
            if "fbcdn" not in (urlsplit(url).hostname or "").lower():
                continue
            seen.add(url)
            found.append((label, url))
    return found


def _facebook_title(page: str) -> str:
    for pattern in (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        r"<title>(.*?)</title>",
    ):
        match = re.search(pattern, page, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return html.unescape(re.sub(r"\s+", " ", match.group(1))).strip()[:180]
    return "Facebook video"


def canonicalize_facebook_url(url: str) -> str:
    """Resolve Facebook share/reel URLs to a stable mobile watch URL when possible."""
    headers = {
        "User-Agent": _FACEBOOK_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        with httpx.Client(follow_redirects=True, timeout=15.0, headers=headers) as client:
            response = client.get(url)
            final_url = str(response.url)
    except httpx.HTTPError:
        return url

    parts = urlsplit(final_url)
    candidates = [
        re.search(r"/videos/(\d+)", parts.path or ""),
        re.search(r"/reel/(\d+)", parts.path or ""),
        re.search(r"[?&]v=(\d+)", final_url),
    ]
    for match in candidates:
        if match:
            return f"https://m.facebook.com/watch/?v={match.group(1)}"

    return final_url or url


def _facebook_fetch_page(url: str) -> tuple[str, str] | None:
    headers = {
        "User-Agent": _FACEBOOK_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
    }

    if curl_requests is not None:
        try:
            response = curl_requests.get(
                url,
                headers=headers,
                allow_redirects=True,
                timeout=20,
                impersonate="chrome",
            )
            if response.status_code == 200:
                return str(response.url), response.text
        except Exception:
            pass

    try:
        with httpx.Client(follow_redirects=True, timeout=20.0, headers=headers) as client:
            response = client.get(url)
            if response.status_code == 200:
                return str(response.url), response.text
    except httpx.HTTPError:
        pass
    return None


def _facebook_pages(url: str) -> list[tuple[str, str]]:
    pages: list[tuple[str, str]] = []
    seen_urls: set[str] = set()

    def add(candidate: str) -> None:
        if not candidate or candidate in seen_urls:
            return
        seen_urls.add(candidate)
        fetched = _facebook_fetch_page(candidate)
        if fetched is not None:
            pages.append(fetched)

    add(url)

    if pages:
        final_url = pages[0][0]
        parts = urlsplit(final_url)
        if parts.hostname and parts.hostname.endswith("facebook.com"):
            mobile = urlunsplit((parts.scheme or "https", "m.facebook.com", parts.path, parts.query, ""))
            add(mobile)

    # Facebook's public embed endpoint often contains the actual playable
    # fbcdn URL even when the regular share/reel page is login/cookie gated.
    plugin_url = (
        "https://www.facebook.com/plugins/video.php?href="
        + quote(url, safe="")
        + "&show_text=false&width=560"
    )
    add(plugin_url)

    return pages


def _analyze_facebook(url: str) -> dict[str, Any] | None:
    try:
        pages = _facebook_pages(url)
    except httpx.HTTPError:
        return None

    title = "Facebook video"
    variants: list[tuple[str, str]] = []
    seen: set[str] = set()
    for page_url, page in pages:
        if title == "Facebook video":
            title = _facebook_title(page) or title
        for label, direct_url in _facebook_variant_urls(page):
            if direct_url in seen:
                continue
            seen.add(direct_url)
            variants.append((label, direct_url))

    if not variants:
        return None

    # Prefer HD first, while still exposing SD when Facebook publishes both.
    variants.sort(key=lambda item: 0 if item[0] == "HD" else 1)
    formats = []
    for index, (label, direct_url) in enumerate(variants[:4]):
        formats.append(
            {
                "id": f"fb-{label.lower()}-{index}",
                "label": label,
                "height": 0,
                "width": 0,
                "fps": 0.0,
                "tbr": 0.0,
                "filesize": 0,
                "ext": "mp4",
                "url": direct_url,
                "headers": {
                    "user-agent": _FACEBOOK_UA,
                    "referer": pages[0][0],
                },
            }
        )
    return {"title": title, "formats": formats}

def analyze_media(url: str, platform: str) -> dict[str, Any]:
    target_url = url
    if platform == "Facebook":
        target_url = canonicalize_facebook_url(url)
        facebook = _analyze_facebook(url)
        if not (facebook and facebook.get("formats")) and target_url != url:
            facebook = _analyze_facebook(target_url)
        if facebook and facebook.get("formats"):
            return facebook

    try:
        with yt_dlp.YoutubeDL(_base_opts(platform)) as ydl:
            info = ydl.extract_info(target_url, download=False)
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
