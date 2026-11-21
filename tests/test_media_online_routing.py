"""Website routing happens before extraction and anonymous quota accounting."""

import pytest
from sqlalchemy import func, select

from downloadany_backend.models import MediaResolveAttempt


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://xhslink.cn/o/8Cey8dDA72X",
    "https://xhslink.com/abc",
    "https://www.xiaohongshu.com/explore/abc",
    "https://www.toutiao.com/video/123",
    "https://weixin.qq.com/sph/abc",
    "https://finder.video.qq.com/video/abc",
    "https://www.youtube.com/watch?v=abc",
    "https://youtu.be/abc",
    "https://v.douyin.com/abc",
    "https://www.iesdouyin.com/share/video/123",
    "https://vm.tiktok.com/abc",
    "https://www.bilibili.com/video/BV123",
    "https://b23.tv/abc",
    "https://www.instagram.com/reel/abc",
    "https://instagr.am/reel/abc",
    "https://XHSLINK.CN/o/8Cey8dDA72X",
])
async def test_supported_website_platforms_reach_resolver(client, monkeypatch, url):
    calls = []

    def resolve(source_url, settings):
        calls.append(source_url)
        return {
            "platform": "test", "id": "1", "title": "clip",
            "webpage_url": source_url, "video_url": "https://cdn.example/video.mp4",
        }

    monkeypatch.setattr("downloadany_backend.media_routes.resolve_direct_video", resolve)
    response = await client.post("/api/media/resolve", json={"url": url})
    assert response.status_code == 200
    assert calls == [url]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "https://www.kuaishou.com/short-video/123",
    "https://example.com/video",
    "https://xhslink.cn.example.com/o/abc",
    "https://fakexhslink.cn/o/abc",
    "https://xhslink.cn@example.com/video",
])
async def test_client_only_skips_extraction_and_quota(client, monkeypatch, db, url):
    def unexpected(*args, **kwargs):
        pytest.fail("Desktop-only requests must not invoke the resolver")

    monkeypatch.setattr("downloadany_backend.media_routes.resolve_direct_video", unexpected)
    response = await client.post("/api/media/resolve", json={"url": url})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "client_only"
    assert db.scalar(select(func.count()).select_from(MediaResolveAttempt)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["invalid link", "http://127.0.0.1/video"])
async def test_invalid_links_are_errors_instead_of_desktop_guidance(client, db, url):
    response = await client.post("/api/media/resolve", json={"url": url})
    assert response.status_code == 422
    assert "client_only" not in response.text
    assert db.scalar(select(func.count()).select_from(MediaResolveAttempt)) == 0
