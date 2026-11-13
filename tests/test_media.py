import pytest

from downloadany_backend.config import Settings
from downloadany_backend.douyin_community import DouyinIE
from downloadany_backend.media import (
    MediaParseError,
    TIKTOK_RETRY_USER_AGENTS,
    XIAOHONGSHU_RETRY_USER_AGENTS,
    YOUTUBE_RETRY_CLIENTS,
    _extract_info_with_retries,
    _url_expiry,
    collapse_formats,
    direct_audio_formats,
    direct_video_formats,
    resolve_bilibili,
    validate_extracted_media_url,
    validate_public_media_url,
)
from yt_dlp.utils import DownloadError


def test_douyin_community_extractor_supports_jingxuan_modal_links():
    assert DouyinIE._match_id(
        "https://www.douyin.com/jingxuan?modal_id=7706444912656534810"
    ) == "7706444912656534810"
    assert DouyinIE._match_id(
        "https://www.douyin.com/video/7706444912656534810"
    ) == "7706444912656534810"


def test_collapse_formats_matches_desktop_download_sentinels():
    info = {
        "thumbnail": "https://example.com/thumb.jpg",
        "subtitles": {"en": []},
        "formats": [
            {"height": 1080, "vcodec": "avc1", "acodec": "none", "filesize": 1_000},
            {"height": 720, "vcodec": "avc1", "acodec": "none", "filesize": 700},
            {"vcodec": "none", "acodec": "mp4a", "filesize": 200},
        ],
    }
    formats = collapse_formats(info)
    assert [item["id"] for item in formats] == [
        "__best__",
        "__video_1080p__",
        "__video_720p__",
        "__audio_mp3__",
        "__thumbnail__",
        "__subtitle_en__",
    ]
    assert formats[1]["filesize"] == 1_200


@pytest.mark.asyncio
async def test_parse_endpoint_requires_login(client):
    response = await client.post("/api/media/parse", json={"url": "https://example.com/video"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_parse_endpoint_returns_desktop_video_info(client, monkeypatch):
    password = "correct-horse-123"
    tokens = (
        await client.post(
            "/api/auth/register",
            json={
                "email": "media@example.com",
                "password": password,
                "confirm_password": password,
            },
        )
    ).json()
    monkeypatch.setattr(
        "downloadany_backend.media_routes.parse_media",
        lambda url, settings: {
            "id": "video-1",
            "title": "测试视频",
            "webpage_url": url,
            "formats": [{"id": "__best__", "label": "最佳画质 + 音频"}],
        },
    )

    response = await client.post(
        "/api/media/parse",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
        json={"url": "https://example.com/video"},
    )

    assert response.status_code == 200
    assert response.json()["webpageUrl"] == "https://example.com/video"
    assert response.json()["formats"][0]["id"] == "__best__"


@pytest.mark.parametrize("url", ["http://127.0.0.1/test", "http://[::1]/test", "ftp://example.com/file"])
def test_media_url_rejects_private_or_unsupported_targets(url):
    with pytest.raises(MediaParseError):
        validate_public_media_url(url, resolve_hostname=False)


def test_extracted_media_url_never_allows_private_literal_ip():
    with pytest.raises(MediaParseError):
        validate_extracted_media_url("http://127.0.0.1/video.mp4")


def test_url_expiry_reads_bilibili_deadline():
    assert _url_expiry("https://cdn.example/video.mp4?deadline=1794395326") == "2026-11-11T11:08:46+00:00"


def test_url_expiry_reads_youtube_expire():
    assert _url_expiry("https://cdn.example/video.mp4?expire=1794420592") == "2026-11-11T18:09:52+00:00"


def test_direct_video_formats_returns_youtube_qualities_and_prefers_h264():
    info = {
        "formats": [
            {
                "format_id": "399",
                "url": "https://cdn.example/av1.mp4?expire=1794420592",
                "protocol": "https",
                "ext": "mp4",
                "width": 1080,
                "height": 1920,
                "format_note": "1080p60",
                "fps": 60,
                "vcodec": "av01.0.09M.08",
                "acodec": "none",
            },
            {
                "format_id": "299",
                "url": "https://cdn.example/h264.mp4?expire=1794420592",
                "protocol": "https",
                "ext": "mp4",
                "width": 1080,
                "height": 1920,
                "format_note": "1080p60",
                "fps": 60,
                "vcodec": "avc1.64002A",
                "acodec": "none",
                "filesize": 25_123_503,
            },
            {
                "format_id": "18",
                "url": "https://cdn.example/360.mp4",
                "protocol": "https",
                "ext": "mp4",
                "width": 360,
                "height": 640,
                "format_note": "360p",
                "fps": 30,
                "vcodec": "avc1.42001E",
                "acodec": "mp4a.40.2",
            },
        ]
    }

    formats = direct_video_formats(info)

    assert [item["id"] for item in formats] == ["299", "18"]
    assert formats[0]["label"] == "1080P 60FPS · 仅视频"
    assert formats[0]["quality"] == 1080
    assert formats[0]["has_audio"] is False
    assert formats[1]["has_audio"] is True


def test_direct_audio_formats_returns_youtube_m4a():
    formats = direct_audio_formats(
        {
            "formats": [
                {
                    "format_id": "140",
                    "url": "https://cdn.example/audio.m4a?expire=1794420592",
                    "protocol": "https",
                    "ext": "m4a",
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                    "abr": 129.5,
                    "filesize": 680_641,
                    "language": "zh-Hant",
                }
            ]
        }
    )

    assert formats[0]["id"] == "140"
    assert formats[0]["label"] == "M4A 129K · zh-Hant"
    assert formats[0]["filesize"] == 680_641


def test_tiktok_formats_include_deduplicated_video_and_mp3_audio():
    info = {
        "formats": [
            {
                "format_id": f"h264_540p_525603-{index}",
                "url": f"https://v{index}.tiktokcdn.com/video.mp4?expire=1794573858",
                "protocol": "https",
                "ext": "mp4",
                "width": 576,
                "height": 1024,
                "fps": 30,
                "vcodec": "h264",
                "acodec": "aac",
                "filesize": 683_284,
            }
            for index in range(2)
        ]
        + [
            {
                "format_id": "audio",
                "url": "https://v16m.tiktokcdn.com/audio.mp3",
                "protocol": "https",
                "ext": "mp3",
                "vcodec": "none",
                "acodec": "mp3",
            }
        ]
    }

    videos = direct_video_formats(info)
    audios = direct_audio_formats(info)

    assert len(videos) == 1
    assert videos[0]["label"] == "540P · 音视频"
    assert audios[0]["label"] == "MP3"
    assert audios[0]["id"] == "audio"


def test_tiktok_retries_with_rotating_user_agents(monkeypatch):
    attempts = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            attempts.append(self.options)
            return self

        def __exit__(self, *_):
            return None

        def extract_info(self, url, download=False):
            if len(attempts) < 3:
                raise DownloadError("ERROR: Unexpected response from webpage request")
            return {"id": "7692795493075881236"}

    monkeypatch.setattr("downloadany_backend.media.YoutubeDL", FakeYoutubeDL)

    info = _extract_info_with_retries("https://www.tiktok.com/@u/video/1", {}, tiktok=True)

    assert info["id"] == "7692795493075881236"
    assert attempts[0].get("http_headers") is None
    assert [item["http_headers"]["User-Agent"] for item in attempts[1:]] == list(
        TIKTOK_RETRY_USER_AGENTS
    )


def test_xiaohongshu_retries_transient_empty_formats(monkeypatch):
    attempts = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            attempts.append(self.options)
            return self

        def __exit__(self, *_):
            return None

        def extract_info(self, url, download=False):
            if len(attempts) < 3:
                raise DownloadError("ERROR: Requested format is not available")
            return {"id": "6aeae2ed0000000019003c86", "url": "https://cdn.example/video.mp4"}

    monkeypatch.setattr("downloadany_backend.media.YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr("downloadany_backend.media.time.sleep", lambda _: None)

    info = _extract_info_with_retries(
        "https://www.xiaohongshu.com/explore/6aeae2ed0000000019003c86",
        {},
        xiaohongshu=True,
    )

    assert info["id"] == "6aeae2ed0000000019003c86"
    assert [item["http_headers"]["User-Agent"] for item in attempts] == list(
        XIAOHONGSHU_RETRY_USER_AGENTS
    )
    assert all(
        item["http_headers"]["Referer"] == "https://www.xiaohongshu.com/"
        for item in attempts
    )


def test_youtube_retries_with_fallback_player_clients(monkeypatch):
    attempts = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            attempts.append(self.options)
            return self

        def __exit__(self, *_):
            return None

        def extract_info(self, url, download=False):
            if len(attempts) < 3:
                raise DownloadError("ERROR: No video formats found")
            return {"id": "HFuWyTmZxAc", "url": "https://cdn.example/video.mp4"}

    monkeypatch.setattr("downloadany_backend.media.YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr("downloadany_backend.media.time.sleep", lambda _: None)

    info = _extract_info_with_retries(
        "https://www.youtube.com/watch?v=HFuWyTmZxAc",
        {},
        youtube=True,
    )

    assert info["id"] == "HFuWyTmZxAc"
    assert attempts[0].get("extractor_args") is None
    assert attempts[1]["extractor_args"]["youtube"]["player_client"] == list(
        YOUTUBE_RETRY_CLIENTS[1]
    )
    assert attempts[2]["extractor_args"]["youtube"]["player_client"] == list(
        YOUTUBE_RETRY_CLIENTS[2]
    )


def test_resolve_bilibili_returns_single_progressive_mp4(monkeypatch):
    calls = []

    def fake_bilibili_json(url, *, params, referer, settings):
        calls.append((url, params, referer))
        if url.endswith("/view"):
            return {
                "title": "测试视频",
                "pic": "https://i.example/cover.jpg",
                "owner": {"name": "作者"},
                "pages": [{"cid": 123, "duration": 42, "part": "P1"}],
            }
        return {
            "format": "mp4720",
            "durl": [
                {
                    "url": "https://cdn.example/video.mp4?deadline=1794395326",
                    "size": 2048,
                }
            ],
        }

    monkeypatch.setattr("downloadany_backend.media._bilibili_json", fake_bilibili_json)
    monkeypatch.setattr("downloadany_backend.media.validate_extracted_media_url", lambda url: url)
    result = resolve_bilibili(
        "https://www.bilibili.com/video/BV1TLW46nEvE/?p=1",
        Settings(jwt_secret="test-secret-that-is-at-least-32-characters-long"),
    )

    assert result["video_url"] == "https://cdn.example/video.mp4?deadline=1794395326"
    assert result["quality"] == "mp4720"
    assert result["filesize"] == 2048
    assert result["formats"][0]["has_audio"] is True
    assert calls[1][1]["platform"] == "html5"


@pytest.mark.asyncio
async def test_resolve_endpoint_returns_camel_case_video_url(client, monkeypatch):
    monkeypatch.setattr(
        "downloadany_backend.media_routes.resolve_direct_video",
        lambda url, settings: {
            "platform": "bilibili",
            "id": "BV1TLW46nEvE",
            "title": "测试视频",
            "video_url": "https://cdn.example/video.mp4",
            "webpage_url": url,
            "ext": "mp4",
            "audio_url": "https://cdn.example/audio.m4a",
            "audio_formats": [
                {
                    "id": "140",
                    "label": "M4A 129K",
                    "url": "https://cdn.example/audio.m4a",
                    "has_audio": True,
                }
            ],
            "formats": [
                {
                    "id": "299",
                    "label": "1080P 60FPS · 仅视频",
                    "url": "https://cdn.example/1080.mp4",
                    "quality": 1080,
                    "fps": 60,
                    "has_audio": False,
                }
            ],
        },
    )

    response = await client.post(
        "/api/media/resolve",
        json={"url": "https://www.bilibili.com/video/BV1TLW46nEvE/"},
    )

    assert response.status_code == 200
    assert response.json()["videoUrl"] == "https://cdn.example/video.mp4"
    assert response.json()["formats"][0]["hasAudio"] is False
    assert "video_url" not in response.json()


def resolved_video(url, settings):
    return {
        "platform": "test",
        "id": "video-1",
        "title": "测试视频",
        "video_url": "https://cdn.example/video.mp4",
        "webpage_url": url,
        "formats": [],
    }


@pytest.mark.asyncio
async def test_anonymous_resolve_allows_five_successes_and_rejects_sixth(client, monkeypatch):
    monkeypatch.setattr("downloadany_backend.media_routes.resolve_direct_video", resolved_video)

    for _ in range(5):
        response = await client.post("/api/media/resolve", json={"url": "https://example.com/video"})
        assert response.status_code == 200

    limited = await client.post("/api/media/resolve", json={"url": "https://example.com/video"})
    assert limited.status_code == 403
    assert "5" in limited.json()["detail"]


@pytest.mark.asyncio
async def test_failed_resolve_does_not_consume_anonymous_quota(client, monkeypatch):
    calls = 0

    def fail_once(url, settings):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise MediaParseError("测试失败")
        return resolved_video(url, settings)

    monkeypatch.setattr("downloadany_backend.media_routes.resolve_direct_video", fail_once)
    failed = await client.post("/api/media/resolve", json={"url": "https://example.com/video"})
    assert failed.status_code == 422

    for _ in range(5):
        response = await client.post("/api/media/resolve", json={"url": "https://example.com/video"})
        assert response.status_code == 200


@pytest.mark.asyncio
async def test_logged_in_resolve_is_unlimited_but_rate_limited(client, monkeypatch):
    monkeypatch.setattr("downloadany_backend.media_routes.resolve_direct_video", resolved_video)
    password = "correct-horse-123"
    tokens = (
        await client.post(
            "/api/auth/register",
            json={
                "email": "web-media@example.com",
                "password": password,
                "confirm_password": password,
            },
        )
    ).json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    for _ in range(10):
        response = await client.post(
            "/api/media/resolve",
            headers=headers,
            json={"url": "https://example.com/video"},
        )
        assert response.status_code == 200

    limited = await client.post(
        "/api/media/resolve",
        headers=headers,
        json={"url": "https://example.com/video"},
    )
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
