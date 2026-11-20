import base64
import json

import pytest

from downloadany_backend.tiktok_tikvideo import extract_tiktok
from downloadany_backend.media import resolve_direct_video, parse_media, MediaParseError
from downloadany_backend.config import Settings


def button(target):
    payload = base64.urlsafe_b64encode(json.dumps({"url": target}).encode()).decode().rstrip("=")
    return f"<a href='https://dl.snapcdn.app/get?token=e30.{payload}.signature&amp;x=1'><span>Download MP4 HD</span></a>"


def mock_service(monkeypatch, result):
    calls = []

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return result

    def post(self, url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("requests.Session.post", post)
    return calls


def test_extract_hd_payload_without_fetching_download_link(monkeypatch):
    target = "https://v.tokcdn.com/video.mp4?token=abc&expires=123"
    calls = mock_service(monkeypatch, {"status": "ok", "data": button(target)})
    result = extract_tiktok("https://vm.tiktok.com/abc/", timeout=12)
    assert result["video_url"] == target
    assert result["thumbnail"] is None
    assert len(calls) == 1
    assert calls[0] == ("https://tikvideo.app/api/ajaxSearch", {
        "data": {"q": "https://vm.tiktok.com/abc/", "lang": "en", "cftoken": ""}, "timeout": 12,
    })


@pytest.mark.parametrize("markup", [
    "", "<a href='https://dl.snapcdn.app/get?token=bad'>Download MP4 HD</a>",
    button("https://127.0.0.1/video.mp4"), button("https://tokcdn.com.evil.test/a"),
])
def test_rejects_missing_or_invalid_hd_target(monkeypatch, markup):
    mock_service(monkeypatch, {"status": "ok", "data": markup})
    with pytest.raises(ValueError, match="MP4 HD"):
        extract_tiktok("https://www.tiktok.com/@u/video/1")


@pytest.mark.parametrize("resolver", [resolve_direct_video, parse_media])
def test_api_parsers_use_tokcdn(monkeypatch, resolver):
    target = "https://v.tokcdn.com/video.mp4"
    cover = "https://p19-common-sign.tiktokcdn-us.com/cover.webp?signature=abc%2Bdef&expires=123"
    markup = f'<img src="{cover.replace("&", "&amp;")}" />' + button(target)
    mock_service(monkeypatch, {"status": "ok", "data": markup})
    result = resolver("https://www.tiktok.com/@u/video/123", Settings())
    assert result["id"] == "123"
    from downloadany_backend.media_routes import DirectVideoInfo, VideoInfo
    response_model = DirectVideoInfo if resolver is resolve_direct_video else VideoInfo
    assert response_model.model_validate(result).model_dump(by_alias=True)["thumbnail"] == cover
    if resolver is resolve_direct_video:
        assert result["video_url"] == target
        assert result["quality"] == "MP4 HD"
    else:
        assert result["webpage_url"] == target


def test_service_failure_becomes_parse_error(monkeypatch):
    mock_service(monkeypatch, {"status": "error"})
    with pytest.raises(MediaParseError, match="TikTok"):
        resolve_direct_video("https://vm.tiktok.com/abc/", Settings())


@pytest.mark.asyncio
async def test_resolve_http_response_includes_tiktok_cover(client, monkeypatch):
    cover = "https://p19.tiktokcdn-us.com/cover.webp?signature=abc&expires=123"
    markup = f'<img src="{cover.replace("&", "&amp;")}">' + button("https://v16.tokcdn.com/video.mp4")
    mock_service(monkeypatch, {"status": "ok", "data": markup})
    response = await client.post("/api/media/resolve", json={"url": "https://vm.tiktok.com/abc/"})
    assert response.status_code == 200
    assert response.json()["thumbnail"] == cover
    assert response.json()["videoUrl"] == "https://v16.tokcdn.com/video.mp4"
    assert "download_options" not in response.json()
