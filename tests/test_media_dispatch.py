"""Dispatch and provider boundaries, without external network requests."""

import pytest

from downloadany_backend.config import Settings
from downloadany_backend.media_parsing import (
    bilibili, douyin, tiktok, wechat, xiaohongshu, youtube, ytdlp,
)
from downloadany_backend.media_parsing.common import MediaParseError
from downloadany_backend.media_parsing.providers import select_provider
from downloadany_backend.media_parsing.result import MediaResult
from downloadany_backend.media_parsing.responses import direct_response, parse_response
from downloadany_backend.media_parsing.service import parse_media, resolve_direct_video


@pytest.mark.parametrize("host,provider", [
    ("www.tiktok.com", tiktok.extract), ("vm.tiktok.com", tiktok.extract),
    ("v.douyin.com", douyin.extract), ("www.iesdouyin.com", douyin.extract),
    ("www.bilibili.com", bilibili.extract), ("weixin.qq.com", wechat.extract),
    ("channels.weixin.qq.com", wechat.extract), ("xhslink.cn", xiaohongshu.extract),
    ("www.xiaohongshu.com", xiaohongshu.extract), ("youtu.be", youtube.extract),
    ("www.youtube.com", youtube.extract), ("example.com", ytdlp.extract),
    ("tiktok.com.evil.test", ytdlp.extract),
])
def test_provider_selection(host, provider):
    assert select_provider(host) is provider


@pytest.mark.parametrize("resolver", [parse_media, resolve_direct_video])
def test_tiktok_bypasses_ytdlp_and_returns_cover(monkeypatch, resolver):
    def unexpected(*args, **kwargs):
        pytest.fail("TikTok must not invoke yt-dlp")

    monkeypatch.setattr(ytdlp, "YoutubeDL", unexpected)
    monkeypatch.setattr(tiktok, "extract_tiktok", lambda *args, **kwargs: {
        "platform": "tiktok", "id": "123", "title": "clip",
        "webpage_url": "https://www.tiktok.com/@u/video/123",
        "video_url": "https://v16.tokcdn.com/clip.mp4",
        "thumbnail": "https://p19.tiktokcdn-us.com/cover.webp?sign=abc&expires=123",
        "ext": "mp4", "quality": "MP4 HD",
    })
    result = resolver("https://www.tiktok.com/@u/video/123", Settings())
    assert result["thumbnail"].endswith("?sign=abc&expires=123")
    assert "download_source" not in result
    assert "download_options" not in result


def test_bilibili_retains_endpoint_specific_quality_behavior(monkeypatch):
    calls = []

    def full_quality(url, settings, *, purpose):
        calls.append(purpose)
        return MediaResult(id="BV123", title="clip")

    monkeypatch.setattr(ytdlp, "extract", full_quality)
    monkeypatch.setattr(bilibili, "resolve_bilibili", lambda *args: {
        "id": "BV123", "title": "clip", "video_url": "https://cdn.example/file.mp4",
    })
    settings = Settings(ytdlp_proxy="http://127.0.0.1:7890")
    url = "https://www.bilibili.com/video/BV123"
    assert parse_media(url, settings)["id"] == "BV123"
    assert resolve_direct_video(url, settings)["video_url"].endswith("file.mp4")
    assert calls == ["parse"]


def test_response_adapters_do_not_mutate_shared_result():
    result = MediaResult(id="1", title="clip", webpage_url="https://example.com/watch/1",
                         video_url="https://cdn.example/1.mp4", download_source="https://cdn.example/1.mp4")
    assert parse_response(result)["webpage_url"] == result.download_source
    assert direct_response(result)["webpage_url"] == result.webpage_url
    assert result.download_source is not None


@pytest.mark.parametrize("resolver", [parse_media, resolve_direct_video])
def test_source_validation_applies_before_dispatch(resolver):
    with pytest.raises(MediaParseError, match="内网"):
        resolver("http://127.0.0.1/video", Settings())
