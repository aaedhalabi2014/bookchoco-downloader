from unittest.mock import patch

import pytest

from app.security import URLValidationError, validate_public_media_url


PUBLIC_DNS = [(None, None, None, None, ("142.250.185.174", 443))]


@pytest.mark.parametrize(
    ("url", "platform"),
    [
        ("https://www.instagram.com/reel/abc/", "Instagram"),
        ("https://www.facebook.com/reel/123/", "Facebook"),
        ("https://x.com/example/status/1", "X"),
        ("https://www.tiktok.com/@user/video/123", "TikTok"),
        ("https://www.threads.net/@user/post/abc", "Threads"),
        ("https://www.reddit.com/r/videos/comments/abc/post/", "Reddit"),
        ("https://vimeo.com/123456", "Vimeo"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "YouTube"),
        ("https://youtu.be/dQw4w9WgXcQ", "YouTube"),
        ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", "YouTube"),
        ("https://www.pinterest.com/pin/123456/", "Pinterest"),
        ("https://pin.it/example", "Pinterest"),
        ("https://www.snapchat.com/spotlight/example", "Snapchat"),
        ("https://clips.twitch.tv/example", "Twitch"),
        ("https://www.twitch.tv/videos/123456", "Twitch"),
        ("https://www.dailymotion.com/video/example", "Dailymotion"),
        ("https://dai.ly/example", "Dailymotion"),
        ("https://soundcloud.com/artist/track", "SoundCloud"),
        ("https://streamable.com/example", "Streamable"),
        ("https://rumble.com/example.html", "Rumble"),
        ("https://www.bilibili.com/video/BVexample", "Bilibili"),
        ("https://b23.tv/example", "Bilibili"),
        ("https://kick.com/channel", "Kick"),
        ("https://bsky.app/profile/user/post/example", "Bluesky"),
        ("https://www.flickr.com/photos/example/123/", "Flickr"),
        ("https://9gag.com/gag/example", "9GAG"),
        ("https://odysee.com/@channel:1/video:1", "Odysee"),
    ],
)
def test_accepts_supported_public_urls(url, platform):
    with patch("socket.getaddrinfo", return_value=PUBLIC_DNS):
        value = validate_public_media_url(url)
    assert value.platform == platform


def test_rejects_lookalike_subdomain():
    with pytest.raises(URLValidationError):
        validate_public_media_url("https://youtube.com.evil.example/video")


def test_rejects_unlisted_domain():
    with pytest.raises(URLValidationError):
        validate_public_media_url("https://example.com/video.mp4")


def test_rejects_credentials():
    with pytest.raises(URLValidationError):
        validate_public_media_url("https://user:pass@instagram.com/reel/abc")


def test_rejects_private_dns_result():
    private_dns = [(None, None, None, None, ("127.0.0.1", 443))]
    with patch("socket.getaddrinfo", return_value=private_dns), pytest.raises(URLValidationError):
        validate_public_media_url("https://instagram.com/reel/abc")
