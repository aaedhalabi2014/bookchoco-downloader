from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from .config import settings


# Only explicitly trusted media-platform domains are accepted. Subdomains of
# these roots are also accepted safely (e.g. music.youtube.com, clips.twitch.tv).
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

    # Expanded support
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "youtube-nocookie.com": "YouTube",
    "pinterest.com": "Pinterest",
    "pin.it": "Pinterest",
    "snapchat.com": "Snapchat",
    "twitch.tv": "Twitch",
    "dailymotion.com": "Dailymotion",
    "dai.ly": "Dailymotion",
    "soundcloud.com": "SoundCloud",
    "streamable.com": "Streamable",
    "rumble.com": "Rumble",
    "bilibili.com": "Bilibili",
    "b23.tv": "Bilibili",
    "kick.com": "Kick",
    "bsky.app": "Bluesky",
    "flickr.com": "Flickr",
    "9gag.com": "9GAG",
    "odysee.com": "Odysee",
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


def _platform_for(hostname: str) -> str | None:
    # Longest suffix first so exact/specific domains win predictably.
    for suffix in sorted(PLATFORM_BY_SUFFIX, key=len, reverse=True):
        if hostname == suffix or hostname.endswith("." + suffix):
            return PLATFORM_BY_SUFFIX[suffix]
    return None


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
    platform = _platform_for(hostname)

    # EXTRA_ALLOWED_HOSTS stays exact-match only by design. This prevents an
    # environment typo from turning into a wildcard allow rule.
    if platform is None and hostname not in settings.extra_hosts:
        raise URLValidationError("هذه المنصة غير مفعّلة حاليًا في النسخة الحالية.")

    if platform is None:
        platform = "Video"

    try:
        addresses = socket.getaddrinfo(hostname, parts.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise URLValidationError("تعذر التحقق من نطاق الرابط.") from exc

    resolved = {item[4][0] for item in addresses}
    if not resolved or any(_is_private_or_special(ip) for ip in resolved):
        raise URLValidationError("تم رفض الرابط لأسباب أمنية.")

    return ValidatedURL(url=candidate, hostname=hostname, platform=platform)
