from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import settings


BASE_ALLOWED_HOSTS = {
    "instagram.com",
    "www.instagram.com",
    "facebook.com",
    "www.facebook.com",
    "m.facebook.com",
    "fb.watch",
    "x.com",
    "www.x.com",
    "twitter.com",
    "www.twitter.com",
    "tiktok.com",
    "www.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "threads.net",
    "www.threads.net",
    "reddit.com",
    "www.reddit.com",
    "redd.it",
    "v.redd.it",
    "vimeo.com",
    "www.vimeo.com",
}

PLATFORM_BY_SUFFIX = {
    "instagram.com": "Instagram",
    "facebook.com": "Facebook",
    "fb.watch": "Facebook",
    "x.com": "X",
    "twitter.com": "X",
    "tiktok.com": "TikTok",
    "threads.net": "Threads",
    "reddit.com": "Reddit",
    "redd.it": "Reddit",
    "v.redd.it": "Reddit",
    "vimeo.com": "Vimeo",
}


class URLValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ValidatedURL:
    url: str
    hostname: str
    platform: str


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


def _platform_for(hostname: str) -> str:
    for suffix, platform in PLATFORM_BY_SUFFIX.items():
        if hostname == suffix or hostname.endswith("." + suffix):
            return platform
    return "Video"


def validate_public_media_url(raw_url: str) -> ValidatedURL:
    candidate = (raw_url or "").strip()
    if not candidate or len(candidate) > 2048:
        raise URLValidationError("الرابط غير صالح.")

    try:
        parts = urlsplit(candidate)
    except ValueError as exc:
        raise URLValidationError("تعذر قراءة الرابط.") from exc

    if parts.scheme.lower() not in {"http", "https"}:
        raise URLValidationError("استخدم رابطًا يبدأ بـ http أو https.")
    if not parts.hostname:
        raise URLValidationError("الرابط لا يحتوي على نطاق صالح.")
    if parts.username or parts.password:
        raise URLValidationError("الروابط التي تحتوي بيانات دخول غير مسموحة.")

    hostname = parts.hostname.lower().rstrip(".")
    allowed = BASE_ALLOWED_HOSTS | settings.extra_hosts
    if hostname not in allowed:
        raise URLValidationError("هذه المنصة غير مفعّلة حاليًا في النسخة الحالية.")

    try:
        addresses = socket.getaddrinfo(hostname, parts.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise URLValidationError("تعذر التحقق من نطاق الرابط.") from exc

    resolved = {item[4][0] for item in addresses}
    if not resolved or any(_is_private_or_special(ip) for ip in resolved):
        raise URLValidationError("تم رفض الرابط لأسباب أمنية.")

    return ValidatedURL(url=candidate, hostname=hostname, platform=_platform_for(hostname))
