from unittest.mock import patch

import pytest

from app.security import URLValidationError, validate_public_media_url


PUBLIC_DNS = [(None, None, None, None, ("31.13.72.174", 443))]


def test_accepts_instagram_public_url():
    with patch("socket.getaddrinfo", return_value=PUBLIC_DNS):
        value = validate_public_media_url("https://www.instagram.com/reel/abc/")
    assert value.platform == "Instagram"


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
