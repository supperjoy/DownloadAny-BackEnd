from datetime import datetime, timedelta

from downloadany_backend.media_parsing import douyin, xiaohongshu, youtube, ytdlp
import pytest
from sqlalchemy import select

from downloadany_backend.config import Settings, get_settings
from downloadany_backend.douyin_community import DouyinIE
from downloadany_backend.media_routes import ParseRequest
from downloadany_backend.models import MediaResolveAttempt, User, utcnow
from downloadany_backend.media import (
    MediaParseError,
    DOUYIN_RETRY_USER_AGENTS,
    XIAOHONGSHU_RETRY_USER_AGENTS,
    YOUTUBE_RETRY_CLIENTS,
    _extract_info_with_retries,
    _is_youtube_retryable,
    _url_expiry,
    collapse_formats,
    direct_audio_formats,
    direct_image_formats,
    direct_video_formats,
    extract_source_url,
    expand_xiaohongshu_redirect,
    resolve_bilibili,
    resolve_direct_video,
    _resolve_douyin_resource_url,
    resolve_wechat_channels,
    resolve_xiaohongshu_short_link,
    parse_media,
    validate_extracted_media_url,
    validate_public_media_url,
    validate_source_media_url,
)
from yt_dlp.utils import DownloadError


def test_douyin_community_extractor_supports_jingxuan_modal_links():
    assert DouyinIE._match_id(
        "https://www.douyin.com/jingxuan?modal_id=7706444912656534810"
    ) == "7706444912656534810"
    assert DouyinIE._match_id(
        "https://www.douyin.com/video/7706444912656534810"
    ) == "7706444912656534810"
    assert DouyinIE._match_id(
        "https://www.douyin.com/note/7691547864870421850?previous_page=app_code_link"
    ) == "7691547864870421850"


@pytest.mark.parametrize("audio_only", [False, True])
def test_douyin_gallery_survives_real_ytdlp_format_selection(monkeypatch, audio_only):
    image_url = "https://p3.douyinpic.com/photo.jpeg"
    detail = {
        "aweme_id": "7709112923969769465",
        "desc": "photo post",
        "video": {"play_addr": {"url_list": ["https://example.com/music.mp3"]}}
        if audio_only else {},
        "images": [{"url_list": [image_url], "width": 1080, "height": 1920}],
    }
    # Mock only the network boundary; exercise the actual extractor and
    # YoutubeDL processing that previously raised before returning images.
    monkeypatch.setattr(DouyinIE, "_download_json", lambda *args, **kwargs: {"aweme_detail": detail})
    result = resolve_direct_video(
        "https://www.douyin.com/note/7709112923969769465", XHS_TEST_SETTINGS
    )
    assert result["id"] == detail["aweme_id"]
    assert [image["url"] for image in result["images"]] == [image_url]
    assert result["video_url"] is None


def test_douyin_missing_formats_without_images_still_fails(monkeypatch):
    monkeypatch.setattr(DouyinIE, "_download_json", lambda *args, **kwargs: {
        "aweme_detail": {"aweme_id": "7709112923969769465", "desc": "unavailable", "video": {}},
    })
    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp.time.sleep", lambda _: None)
    with pytest.raises(MediaParseError, match="Requested format is not available"):
        resolve_direct_video(
            "https://www.douyin.com/video/7709112923969769465", XHS_TEST_SETTINGS
        )


def test_direct_image_formats_returns_douyin_gallery_images():
    images = direct_image_formats(
        {
            "images": [
                {
                    "id": "1",
                    "url": "https://p3-pc-sign.douyinpic.com/path/image.webp?x-expires=1797411600",
                    "width": 1260,
                    "height": 784,
                },
                {
                    "id": "2",
                    "url": "https://p9-pc-sign.douyinpic.com/path/image.jpeg?x-expires=1797411600",
                    "width": 1260,
                    "height": 784,
                },
            ]
        }
    )

    assert [item["label"] for item in images] == ["图片 1", "图片 2"]
    assert [item["ext"] for item in images] == ["webp", "jpeg"]
    assert images[0]["expires_at"] == "2026-12-16T09:00:00+00:00"


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


def test_collapse_formats_reads_mangotv_quality_from_format_note():
    info = {
        "formats": [
            {
                "format_id": "787",
                "format_note": "480P",
                "height": None,
                "protocol": "m3u8_native",
                "ext": "mp4",
                "vcodec": "H264",
                "acodec": "AAC",
                "filesize_approx": 30_892_032,
            },
            {
                "format_id": "1368",
                "format_note": "720P",
                "height": None,
                "protocol": "m3u8_native",
                "ext": "mp4",
                "vcodec": "H264",
                "acodec": "AAC",
                "filesize_approx": 53_697_741,
            },
        ]
    }

    formats = collapse_formats(info)

    assert [item["id"] for item in formats] == [
        "__best__",
        "__video_720p__",
        "__video_480p__",
        "__audio_mp3__",
    ]
    assert formats[1]["filesize"] == 53_697_741


@pytest.mark.asyncio
async def test_parse_endpoint_requires_login(client):
    response = await client.post("/api/media/parse", json={"url": "https://example.com/video"})
    assert response.status_code == 401


def test_media_request_extracts_url_from_share_caption():
    request = ParseRequest(
        url=(
            "0.25 复制打开抖音，看看【戴同学啥都玩的作品】 "
            r"https\://v.douyin.com/srtHFDRu7Bk/ W@z.TL :7pm"
        )
    )
    assert request.url == "https://v.douyin.com/srtHFDRu7Bk/"


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
            "images": [
                {
                    "id": "1",
                    "label": "图片 1",
                    "url": "https://p3-pc-sign.douyinpic.com/image.webp",
                    "ext": "webp",
                    "width": 1260,
                    "height": 784,
                }
            ],
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
    assert response.json()["images"][0]["label"] == "图片 1"
    assert response.json()["formats"][0]["id"] == "__best__"


@pytest.mark.parametrize("url", ["http://127.0.0.1/test", "http://[::1]/test", "ftp://example.com/file"])
def test_media_url_rejects_private_or_unsupported_targets(url):
    with pytest.raises(MediaParseError):
        validate_public_media_url(url, resolve_hostname=False)


def test_extract_source_url_from_douyin_share_caption():
    raw = (
        "0.25 复制打开抖音，看看【戴同学啥都玩的作品】GTA6终于来了！ "
        r"https\://v.douyin.com/srtHFDRu7Bk/ W@z.TL :7pm 12/06 ATL:/&#x20;"
    )
    assert extract_source_url(raw) == "https://v.douyin.com/srtHFDRu7Bk/"


def test_validate_source_media_url_extracts_share_caption_without_dns():
    raw = "复制打开抖音 https://v.douyin.com/srtHFDRu7Bk/ 7pm"
    settings = Settings(
        jwt_secret="test-secret-that-is-at-least-32-characters-long",
        ytdlp_proxy="",
    )
    assert validate_source_media_url(raw, settings) == "https://v.douyin.com/srtHFDRu7Bk/"


def test_extracted_media_url_never_allows_private_literal_ip():
    with pytest.raises(MediaParseError):
        validate_extracted_media_url("http://127.0.0.1/video.mp4")


def test_known_short_video_hosts_ignore_private_dns_but_not_private_urls(monkeypatch):
    monkeypatch.setattr(
        "downloadany_backend.media_parsing.common.socket.getaddrinfo",
        lambda *args: [(None, None, None, None, ("127.0.0.1", 0))],
    )
    settings = Settings(
        jwt_secret="test-secret-that-is-at-least-32-characters-long",
        ytdlp_proxy="",
    )
    for url in (
        "https://v.douyin.com/abc123/",
        "https://www.douyin.com/video/7706444912656534810",
        "https://www.tiktok.com/@creator/video/7692795493075881236",
    ):
        assert validate_source_media_url(url, settings) == url
    with pytest.raises(MediaParseError, match="内网地址"):
        validate_source_media_url("https://example.com/video", settings)
    with pytest.raises(MediaParseError, match="内网地址"):
        validate_source_media_url("http://127.0.0.1/video", settings)
    with pytest.raises(MediaParseError, match="内网地址"):
        validate_extracted_media_url("http://192.168.1.2/video.mp4", trust_extractor=True)


def test_douyin_resolve_accepts_extractor_cdn_under_private_dns(monkeypatch):
    monkeypatch.setattr("downloadany_backend.media_parsing.douyin._resolve_douyin_resource_url", lambda url, settings: url)
    monkeypatch.setattr(
        "downloadany_backend.media_parsing.common.socket.getaddrinfo",
        lambda *args: [(None, None, None, None, ("127.0.0.1", 0))],
    )
    cdn_url = "https://video.byted-example.com/video.mp4"
    monkeypatch.setattr(
        "downloadany_backend.media_parsing.ytdlp._extract_info_with_retries",
        lambda *args, **kwargs: {
            "id": "video-1",
            "url": cdn_url,
            "ext": "mp4",
            "protocol": "https",
            "vcodec": "h264",
            "acodec": "aac",
            "formats": [],
        },
    )
    result = resolve_direct_video(
        "https://www.douyin.com/video/7706444912656534810",
        Settings(
            jwt_secret="test-secret-that-is-at-least-32-characters-long",
            ytdlp_proxy="",
        ),
    )
    assert result["video_url"] == cdn_url


def test_douyin_gallery_returns_all_images_without_probing_companion_video(monkeypatch):
    image_urls = [
        "https://p95-zjwztc-sign.douyinpic.com/first.jpeg?x-expires=1797649200",
        "https://p5-ex-gddgtc-sign.douyinpic.com/second.jpeg?x-expires=1797649200",
    ]
    monkeypatch.setattr(
        "downloadany_backend.media_parsing.ytdlp._extract_info_with_retries",
        lambda *args, **kwargs: {
            "id": "7670916508982356849",
            "title": "测试图集",
            "webpage_url": "https://www.douyin.com/note/7670916508982356849",
            "url": "https://sf6-cdn-tos.douyinstatic.com/companion.mp4",
            "formats": [],
            "images": [
                {"id": str(index), "url": image_url, "width": 1440, "height": 1920}
                for index, image_url in enumerate(image_urls, 1)
            ],
        },
    )
    monkeypatch.setattr(
        "downloadany_backend.media_parsing.douyin._resolve_douyin_resource_url",
        lambda *args: pytest.fail("图集不应检查视频 CDN"),
    )

    result = resolve_direct_video("https://v.douyin.com/I4phTbIwYjE/", XHS_TEST_SETTINGS)

    assert [image["url"] for image in result["images"]] == image_urls
    assert result["video_url"] is None
    assert result["resource_url"] is None
    assert result["formats"] == []


def test_douyin_resolve_avoids_cdn_that_requires_referer(monkeypatch):
    monkeypatch.setattr("downloadany_backend.media_parsing.douyin._resolve_douyin_resource_url", lambda url, settings: url)
    restricted = "https://v26-web.douyinvod.com/video.mp4?a=6383"
    direct = "https://v9-dy.ixigua.com/video.mp4?a=1768"
    monkeypatch.setattr(
        "downloadany_backend.media_parsing.ytdlp._extract_info_with_retries",
        lambda *args, **kwargs: {
            "id": "video-1",
            "url": restricted,
            "ext": "mp4",
            "vcodec": "h264",
            "acodec": "aac",
            "formats": [
                {
                    "format_id": f"h264_720p-{index}",
                    "url": url,
                    "protocol": "https",
                    "ext": "mp4",
                    "width": 720,
                    "height": 1280,
                    "vcodec": "h264",
                    "acodec": "aac",
                }
                for index, url in enumerate((restricted, direct))
            ],
        },
    )
    result = resolve_direct_video(
        "https://www.douyin.com/video/7706444912656534810",
        XHS_TEST_SETTINGS,
    )
    assert result["video_url"] == direct
    assert result["resource_url"] == direct
    assert result["formats"] == []


def test_douyin_resolve_reextracts_when_only_referer_bound_cdn_is_returned(monkeypatch):
    monkeypatch.setattr("downloadany_backend.media_parsing.douyin._resolve_douyin_resource_url", lambda url, settings: url)
    restricted = "https://v26-web.douyinvod.com/video.mp4?a=6383"
    direct = "https://v9-dy.ixigua.com/video.mp4?a=1768"
    calls = []

    def fake_extract(*args, **kwargs):
        calls.append(1)
        cdn_url = restricted if len(calls) == 1 else direct
        return {
            "id": "video-1",
            "url": cdn_url,
            "ext": "mp4",
            "protocol": "https",
            "vcodec": "h264",
            "acodec": "aac",
            "formats": [],
        }

    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp._extract_info_with_retries", fake_extract)
    result = resolve_direct_video(
        "https://www.douyin.com/video/7706444912656534810",
        XHS_TEST_SETTINGS,
    )
    assert len(calls) == 2
    assert result["video_url"] == direct


def test_douyin_resolve_rejects_referer_bound_cdn_after_retries(monkeypatch):
    calls = []

    def fake_extract(*args, **kwargs):
        calls.append(1)
        return {
            "id": "video-1",
            "url": "https://v26-web.douyinvod.com/video.mp4?a=6383",
            "ext": "mp4",
            "vcodec": "h264",
            "acodec": "aac",
            "formats": [],
        }

    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp._extract_info_with_retries", fake_extract)
    with pytest.raises(MediaParseError, match="来源校验"):
        resolve_direct_video(
            "https://www.douyin.com/video/7706444912656534810",
            XHS_TEST_SETTINGS,
        )
    assert len(calls) == 3


def test_douyin_resource_url_follows_redirect_and_checks_video_response(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, status, headers):
            self.status_code = status
            self.headers = headers

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    class FakeSession:
        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            if url.startswith("https://www.douyin.com/"):
                return FakeResponse(302, {"Location": "https://v9-dy.ixigua.com/video.mp4"})
            return FakeResponse(206, {"Content-Type": "video/mp4"})

    monkeypatch.setattr("requests.Session", FakeSession)
    settings = Settings(jwt_secret="test-secret-that-is-at-least-32-characters-long")
    assert _resolve_douyin_resource_url(
        "https://www.douyin.com/aweme/v1/play/?video_id=123", settings
    ) == "https://v9-dy.ixigua.com/video.mp4"
    assert all(call[1]["headers"] == {"Range": "bytes=0-0"} for call in calls)
    assert all(call[1]["allow_redirects"] is False for call in calls)


def test_url_expiry_reads_bilibili_deadline():
    assert _url_expiry("https://cdn.example/video.mp4?deadline=1794395326") == "2026-11-11T11:08:46+00:00"


def test_url_expiry_reads_youtube_expire():
    assert _url_expiry("https://cdn.example/video.mp4?expire=1794420592") == "2026-11-11T18:09:52+00:00"


def test_url_expiry_reads_douyin_cdn_path():
    assert _url_expiry(
        "https://v11-weba.douyinvod.com/aa6c8a54f0b57a7917a13cd362fff963/"
        "6afdcc6e/video/tos/cn/file/?a=6383"
    ) == "2026-11-18T16:03:58+00:00"
    assert _url_expiry(
        "https://v9-dy.ixigua.com/2385f08a0f6116e395824791de1f9e4f/"
        "6afdc8ef/video/tos/cn/file/?a=1768"
    ) == "2026-11-18T15:49:03+00:00"


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


@pytest.mark.parametrize(
    "source_url",
    [
        "https://www.douyin.com/video/7706444912656534810",
    ],
)
def test_short_video_resolve_returns_progressive_cdn_url(source_url, monkeypatch):
    monkeypatch.setattr("downloadany_backend.media_parsing.douyin._resolve_douyin_resource_url", lambda url, settings: url)
    cdn_url = "https://v16m.tiktokcdn.com/video.mp4?expire=1794573858"
    extracted = {}

    def fake_extract(url, options, **flags):
        extracted.update(url=url, options=options, flags=flags)
        return {
            "id": "video-1",
            "title": "测试视频",
            "url": "https://v16m.tiktokcdn.com/silent.mp4",
            "vcodec": "h264",
            "acodec": "none",
            "formats": [
                {
                    "format_id": "video-only",
                    "url": "https://v16m.tiktokcdn.com/silent.mp4",
                    "protocol": "https",
                    "ext": "mp4",
                    "width": 1080,
                    "height": 1920,
                    "vcodec": "h264",
                    "acodec": "none",
                },
                {
                    "format_id": "progressive",
                    "url": cdn_url,
                    "protocol": "https",
                    "ext": "mp4",
                    "width": 720,
                    "height": 1280,
                    "vcodec": "h264",
                    "acodec": "aac",
                    "filesize": 12345,
                },
            ],
        }

    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp._extract_info_with_retries", fake_extract)
    result = resolve_direct_video(source_url, XHS_TEST_SETTINGS)

    assert extracted["options"]["format"] == "best[protocol^=http][vcodec!=none][acodec!=none][ext=mp4]/best[protocol^=http][vcodec!=none][acodec!=none]/bestvideo[protocol^=http][ext=mp4]/bestvideo[protocol^=http]"
    assert extracted["flags"]["policy"].extractor_factory is not None
    assert result["video_url"] == cdn_url
    if extracted["flags"]["policy"].extractor_factory:
        assert result["resource_url"] == cdn_url
        assert result["formats"] == []
    else:
        assert result["formats"][0]["url"] == cdn_url
        assert result["formats"][0]["has_audio"] is True
    assert result["filesize"] == 12345
    assert result["expires_at"] == "2026-11-13T12:44:18+00:00"


@pytest.mark.parametrize(
    "source_url",
    [
        "https://www.douyin.com/video/7706444912656534810",
    ],
)
def test_short_video_resolve_rejects_silent_only_result(source_url, monkeypatch):
    monkeypatch.setattr(
        "downloadany_backend.media_parsing.ytdlp._extract_info_with_retries",
        lambda *args, **kwargs: {
            "id": "video-1",
            "url": "https://v16m.tiktokcdn.com/silent.mp4",
            "ext": "mp4",
            "vcodec": "h264",
            "acodec": "none",
            "formats": [],
        },
    )
    with pytest.raises(MediaParseError, match="音视频合一"):
        resolve_direct_video(source_url, XHS_TEST_SETTINGS)


def test_douyin_resolve_uses_top_level_cdn_when_formats_omit_it(monkeypatch):
    monkeypatch.setattr("downloadany_backend.media_parsing.douyin._resolve_douyin_resource_url", lambda url, settings: url)
    cdn_url = "https://v16m.tiktokcdn.com/progressive.mp4"
    monkeypatch.setattr(
        "downloadany_backend.media_parsing.ytdlp._extract_info_with_retries",
        lambda *args, **kwargs: {
            "id": "video-1",
            "url": cdn_url,
            "ext": "mp4",
            "protocol": "https",
            "vcodec": "h264",
            "acodec": "aac",
            "formats": [
                {
                    "format_id": "video-only",
                    "url": "https://v16m.tiktokcdn.com/silent.mp4",
                    "protocol": "https",
                    "ext": "mp4",
                    "width": 1080,
                    "height": 1920,
                    "vcodec": "h264",
                    "acodec": "none",
                }
            ],
        },
    )
    result = resolve_direct_video(
        "https://www.douyin.com/video/7692795493075881236",
        XHS_TEST_SETTINGS,
    )
    assert result["video_url"] == cdn_url
    assert result["formats"] == []


def test_tiktok_uses_tikvideo_extractor(monkeypatch):
    calls = []

    def extract(url, **kwargs):
        calls.append((url, kwargs))
        return {"id": "1", "title": "TikTok", "video_url": "https://v.tokcdn.com/video.mp4"}

    monkeypatch.setattr("downloadany_backend.media_parsing.tiktok.extract_tiktok", extract)
    result = resolve_direct_video(
        "https://www.tiktok.com/@u/video/1",
        Settings(ytdlp_socket_timeout=15, ytdlp_proxy="http://localhost:7897"),
    )
    assert result["id"] == "1"
    assert calls == [("https://www.tiktok.com/@u/video/1", {
        "timeout": 15, "proxy": "http://localhost:7897",
    })]


def test_douyin_retries_transient_403_with_fresh_extractor(monkeypatch):
    attempts = []
    registrations = []

    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            attempts.append(self.options)
            return self

        def __exit__(self, *_):
            return None

        def add_info_extractor(self, extractor):
            self.extractor = extractor
            registrations.append(extractor)

        def extract_info(self, url, download=False):
            if len(attempts) == 1:
                raise DownloadError("ERROR: HTTP Error 403: Forbidden")
            return {"id": "7691547864870421850", "url": "https://cdn.example/video.mp4"}

    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp.YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr("downloadany_backend.media_parsing.douyin.DouyinIE", lambda ydl: object())
    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp.time.sleep", lambda _: None)

    info = _extract_info_with_retries(
        "https://www.iesdouyin.com/share/video/7686367919383311098/",
        {},
        policy=douyin.policy(),
    )

    assert info["id"] == "7691547864870421850"
    assert len(attempts) == 2
    assert len(registrations) == 2
    assert attempts[1]["http_headers"]["User-Agent"] == DOUYIN_RETRY_USER_AGENTS[0]


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

    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp.YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp.time.sleep", lambda _: None)

    info = _extract_info_with_retries(
        "https://www.xiaohongshu.com/explore/6aeae2ed0000000019003c86",
        {},
        policy=xiaohongshu.policy(),
    )

    assert info["id"] == "6aeae2ed0000000019003c86"
    assert [item["http_headers"]["User-Agent"] for item in attempts] == list(
        XIAOHONGSHU_RETRY_USER_AGENTS
    )
    assert all(
        item["http_headers"]["Referer"] == "https://www.xiaohongshu.com/"
        for item in attempts
    )


# 真实线上样本：腾讯云 HK 访问 xhslink.cn 短链 302 到 rednote 登录墙，
# redirectPath 内是带 xsec_token 的完整笔记地址（percent-encoded 一次）。
XHS_LOGIN_WALL_URL = (
    "https://www.rednote.com/login?redirectPath=http%3A%2F%2Fwww.rednote.com%2F"
    "discovery%2Fitem%2F6af9738d00000000090214ed%3Fapp_platform%3Dandroid"
    "%26xsec_source%3Dapp_share%26type%3Dvideo"
    "%26xsec_token%3DCBc5AZxTKF4I0memE0hlKdQF2RWB6eNLMLGQP-o4ET1RY%3D"
)
XHS_NOTE_LONG_URL = (
    "https://www.xiaohongshu.com/discovery/item/6af9738d00000000090214ed"
    "?app_platform=android&xsec_source=app_share&type=video"
    "&xsec_token=CBc5AZxTKF4I0memE0hlKdQF2RWB6eNLMLGQP-o4ET1RY="
)
XHS_TEST_SETTINGS = Settings(
    jwt_secret="test-secret-that-is-at-least-32-characters-long",
    ytdlp_proxy="http://127.0.0.1:7890",
)


def test_expand_xiaohongshu_redirect_decodes_login_wall_redirect_path():
    assert expand_xiaohongshu_redirect(XHS_LOGIN_WALL_URL) == XHS_NOTE_LONG_URL


def test_expand_xiaohongshu_redirect_canonicalizes_percent_encoded_redirect_path():
    # 国内出口 IP 的样本：redirectPath 里 token 以 %3D 编码
    percent_wall = (
        "https://www.xiaohongshu.com/login?redirectPath=http%3A%2F%2Fwww.xiaohongshu.com%2F"
        "discovery%2Fitem%2F6af9738d00000000090214ed%3Fxsec_token%3DCBc5%253D"
        "%26xsec_source%3Dapp_share"
    )
    assert (
        expand_xiaohongshu_redirect(percent_wall)
        == "https://www.xiaohongshu.com/discovery/item/6af9738d00000000090214ed"
        "?xsec_token=CBc5%3D&xsec_source=app_share"
    )


def test_expand_xiaohongshu_redirect_passes_through_direct_note_link():
    assert (
        expand_xiaohongshu_redirect(
            "http://www.xiaohongshu.com/explore/6af9738d00000000090214ed?xsec_token=abc"
        )
        == "https://www.xiaohongshu.com/explore/6af9738d00000000090214ed?xsec_token=abc"
    )


def test_expand_xiaohongshu_redirect_rejects_unrelated_pages():
    assert expand_xiaohongshu_redirect("https://www.xiaohongshu.com/") is None
    assert (
        expand_xiaohongshu_redirect(
            "https://example.com/login?redirectPath=https%3A%2F%2Fevil.com%2Fx"
        )
        is None
    )


def test_resolve_xiaohongshu_short_link_follows_redirect_chain(monkeypatch):
    requested = {}

    class FakeResponse:
        def __init__(self, url, history=()):
            self.url = url
            self.history = list(history)

        def raise_for_status(self):
            return None

    class FakeSession:
        def get(self, url, **kwargs):
            requested["url"] = url
            requested.update(kwargs)
            return FakeResponse(
                XHS_LOGIN_WALL_URL,
                history=[FakeResponse("https://xhslink.cn/o/8bQrEKxb2wT")],
            )

    monkeypatch.setattr(
        "requests.Session", lambda: FakeSession()
    )

    resolved = resolve_xiaohongshu_short_link(
        "https://xhslink.cn/o/8bQrEKxb2wT", XHS_TEST_SETTINGS
    )

    assert resolved == XHS_NOTE_LONG_URL
    assert requested["url"] == "https://xhslink.cn/o/8bQrEKxb2wT"
    assert requested["allow_redirects"] is True
    assert requested["proxies"] == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }
    assert requested["headers"]["Referer"] == "https://www.xiaohongshu.com/"


def test_resolve_xiaohongshu_short_link_raises_when_chain_stays_unexpanded(monkeypatch):
    class FakeResponse:
        url = "https://www.xiaohongshu.com/404"
        history = []

        def raise_for_status(self):
            return None

    class FakeSession:
        def get(self, url, **kwargs):
            return FakeResponse()

    monkeypatch.setattr(
        "requests.Session", lambda: FakeSession()
    )

    with pytest.raises(MediaParseError, match="小红书短链接"):
        resolve_xiaohongshu_short_link("https://xhslink.cn/o/deadlink", XHS_TEST_SETTINGS)


def test_resolve_xiaohongshu_short_link_wraps_network_failure(monkeypatch):
    import requests as requests_module

    class FakeSession:
        def get(self, url, **kwargs):
            raise requests_module.exceptions.ConnectionError("dns down")

    monkeypatch.setattr(
        "requests.Session", lambda: FakeSession()
    )

    with pytest.raises(MediaParseError, match="请求失败"):
        resolve_xiaohongshu_short_link("https://xhslink.cn/o/8bQrEKxb2wT", XHS_TEST_SETTINGS)


def _fake_xhs_info():
    return {
        "id": "6af9738d00000000090214ed",
        "title": "测试笔记",
        "duration": 12,
        "url": "https://sns-video.example.com/video.mp4",
        "ext": "mp4",
        "formats": [
            {
                "format_id": "2",
                "url": "https://sns-video.example.com/video.mp4",
                "protocol": "https",
                "ext": "mp4",
                "width": 720,
                "height": 1280,
                "vcodec": "avc1",
                "acodec": "mp4a",
            }
        ],
    }


def test_resolve_direct_video_expands_xiaohongshu_short_link(monkeypatch):
    extracted = {}

    def fake_extract(url, options, **flags):
        extracted["url"] = url
        extracted["flags"] = flags
        return _fake_xhs_info()

    monkeypatch.setattr(
        "downloadany_backend.media_parsing.xiaohongshu.resolve_xiaohongshu_short_link",
        lambda url, settings: XHS_NOTE_LONG_URL,
    )
    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp._extract_info_with_retries", fake_extract)
    monkeypatch.setattr(
        "downloadany_backend.media_parsing.yt_adapter.validate_extracted_media_url", lambda url, **kwargs: url
    )

    result = resolve_direct_video("https://xhslink.cn/o/8bQrEKxb2wT", XHS_TEST_SETTINGS)

    assert extracted["url"] == XHS_NOTE_LONG_URL
    assert extracted["flags"]["policy"].attempts[0]["http_headers"]["Referer"] == "https://www.xiaohongshu.com/"
    assert result["video_url"] == "https://sns-video.example.com/video.mp4"


def test_resolve_direct_video_skips_expansion_for_long_xiaohongshu_links(monkeypatch):
    calls = []

    def fake_extract(url, options, **flags):
        return _fake_xhs_info()

    monkeypatch.setattr(
        "downloadany_backend.media_parsing.xiaohongshu.resolve_xiaohongshu_short_link",
        lambda *args: calls.append(args),
    )
    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp._extract_info_with_retries", fake_extract)
    monkeypatch.setattr(
        "downloadany_backend.media_parsing.yt_adapter.validate_extracted_media_url", lambda url, **kwargs: url
    )

    resolve_direct_video(XHS_NOTE_LONG_URL, XHS_TEST_SETTINGS)

    assert calls == []


def test_parse_media_expands_xiaohongshu_short_link(monkeypatch):
    extracted = {}

    def fake_extract(url, options, **flags):
        extracted["url"] = url
        extracted["flags"] = flags
        return _fake_xhs_info()

    monkeypatch.setattr(
        "downloadany_backend.media_parsing.xiaohongshu.resolve_xiaohongshu_short_link",
        lambda url, settings: XHS_NOTE_LONG_URL,
    )
    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp._extract_info_with_retries", fake_extract)

    result = parse_media("https://xhslink.cn/o/8bQrEKxb2wT", XHS_TEST_SETTINGS)

    assert extracted["url"] == XHS_NOTE_LONG_URL
    assert extracted["flags"]["policy"].attempts[0]["http_headers"]["Referer"] == "https://www.xiaohongshu.com/"
    assert [item["id"] for item in result["formats"]][0] == "__best__"


def _pool_video_info():
    return {
        "id": "7692795493075881236",
        "title": "测试视频",
        "url": "https://v1.tiktokcdn.com/video.mp4",
        "ext": "mp4",
        "formats": [
            {
                "format_id": "1",
                "url": "https://v1.tiktokcdn.com/video.mp4",
                "protocol": "https",
                "ext": "mp4",
                "height": 1080,
                "width": 720,
                "vcodec": "h264",
                "acodec": "aac",
            }
        ],
    }


def _pool_settings(**overrides):
    base = {
        "jwt_secret": "test-secret-that-is-at-least-32-characters-long",
        "ytdlp_proxy": "http://127.0.0.1:7891",
        "mihomo_controller_url": "http://127.0.0.1:9090",
        "mihomo_proxy_url": "http://127.0.0.1:7890",
    }
    base.update(overrides)
    return Settings(**base)


def test_resolve_direct_video_uses_mihomo_proxy_after_random_switch(monkeypatch):
    switches = []
    captured = {}

    def fake_switch(settings):
        switches.append(settings)
        return "🇯🇵|日本-中转 02"

    def fake_extract(url, options, **flags):
        captured["options"] = options
        return _pool_video_info()

    monkeypatch.setattr("downloadany_backend.proxy_pool.switch_to_random_active", fake_switch)
    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp._extract_info_with_retries", fake_extract)

    resolve_direct_video("https://www.youtube.com/watch?v=pool1", _pool_settings())

    assert len(switches) == 1
    assert captured["options"]["proxy"] == "http://127.0.0.1:7890"


def test_resolve_direct_video_falls_back_to_ytdlp_proxy_when_switch_fails(monkeypatch):
    captured = {}

    monkeypatch.setattr("downloadany_backend.proxy_pool.switch_to_random_active", lambda settings: None)

    def fake_extract(url, options, **flags):
        captured["options"] = options
        return _pool_video_info()

    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp._extract_info_with_retries", fake_extract)

    resolve_direct_video("https://www.youtube.com/watch?v=pool2", _pool_settings())

    assert captured["options"]["proxy"] == "http://127.0.0.1:7891"


def test_resolve_direct_video_skips_pool_when_mihomo_disabled(monkeypatch):
    captured = {}

    def forbidden(settings):
        raise AssertionError("pool must stay untouched without controller url")

    monkeypatch.setattr("downloadany_backend.proxy_pool.switch_to_random_active", forbidden)

    def fake_extract(url, options, **flags):
        captured["options"] = options
        return _pool_video_info()

    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp._extract_info_with_retries", fake_extract)

    settings = _pool_settings(mihomo_controller_url="")
    resolve_direct_video("https://www.youtube.com/watch?v=pool3", settings)

    assert captured["options"]["proxy"] == "http://127.0.0.1:7891"


def test_resolve_direct_video_pool_skips_local_dns_of_target_url(monkeypatch):
    import socket as socket_module

    def forbidden_dns(*args):
        raise AssertionError("target DNS must be skipped when pool is enabled")

    monkeypatch.setattr(socket_module, "getaddrinfo", forbidden_dns)
    monkeypatch.setattr(
        "downloadany_backend.proxy_pool.switch_to_random_active", lambda settings: "节点"
    )

    def fake_extract(url, options, **flags):
        return _pool_video_info()

    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp._extract_info_with_retries", fake_extract)

    # 出口由代理池负责时，入站 URL 不再本机解析（服务器 DNS 可能被污染/屏蔽）。
    # 用 YouTube 而非抖音系：排除"已知平台跳过 DNS"的旧分支干扰。
    result = resolve_direct_video(
        "https://www.youtube.com/watch?v=5034TTDW584",
        _pool_settings(ytdlp_proxy=""),
    )
    assert result["video_url"] == "https://v1.tiktokcdn.com/video.mp4"


def test_youtube_ssl_eof_errors_are_retryable():
    eof = DownloadError(
        "ERROR: [youtube] 0S2qMuyXjfQ: Unable to download API page: "
        "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol "
        "(_ssl.c:1032) (caused by SSLError(...))"
    )
    bare_ssl = DownloadError(
        "ERROR: [youtube] xx: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"
    )
    assert _is_youtube_retryable(eof) is True
    assert _is_youtube_retryable(bare_ssl) is True


def test_extract_info_switches_proxy_via_refresher_on_retry(monkeypatch):
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
            if len(attempts) < 2:
                raise DownloadError(
                    "ERROR: [youtube] xx: Unable to download API page: "
                    "[SSL: UNEXPECTED_EOF_WHILE_READING]"
                )
            return {"id": "xx", "url": "https://rr.googlevideo.com/videoplayback"}

    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp.YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp.time.sleep", lambda _: None)

    refreshed = []

    def refresher():
        refreshed.append(1)
        return "http://pool-2"

    info = _extract_info_with_retries(
        "https://www.youtube.com/watch?v=xx",
        {"proxy": "http://pool-1"},
        policy=youtube.policy(),
        proxy_refresher=refresher,
    )

    assert info["id"] == "xx"
    assert attempts[0]["proxy"] == "http://pool-1"
    assert attempts[1]["proxy"] == "http://pool-2"
    assert len(refreshed) == 1


def test_resolve_direct_video_rotates_pool_node_between_retries(monkeypatch):
    switched = []

    def fake_switch(settings, avoid=None):
        switched.append(avoid)
        return f"节点{len(switched)}"

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
            if len(attempts) < 2:
                raise DownloadError(
                    "ERROR: [youtube] 0S2qMuyXjfQ: Unable to download API page: "
                    "[SSL: UNEXPECTED_EOF_WHILE_READING]"
                )
            return {
                "id": "0S2qMuyXjfQ",
                "title": "测试",
                "url": "https://rr.googlevideo.com/videoplayback?x=1",
                "ext": "mp4",
            }

    monkeypatch.setattr("downloadany_backend.proxy_pool.switch_to_random_active", fake_switch)
    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp.YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp.time.sleep", lambda _: None)

    result = resolve_direct_video(
        "https://www.youtube.com/watch?v=0S2qMuyXjfQ",
        _pool_settings(ytdlp_proxy=""),
    )

    assert result["video_url"] == "https://rr.googlevideo.com/videoplayback?x=1"
    # 首次选择 avoid=None；重试时避开上一次钉住的节点。
    assert switched == [None, "节点1"]
    assert all(item["proxy"] == "http://127.0.0.1:7890" for item in attempts)


def test_parse_media_uses_mihomo_proxy_after_random_switch(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        "downloadany_backend.proxy_pool.switch_to_random_active",
        lambda settings: "🇭🇰|香港-IEPL 03",
    )

    def fake_extract(url, options, **flags):
        captured["options"] = options
        return _pool_video_info()

    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp._extract_info_with_retries", fake_extract)

    parse_media("https://www.youtube.com/watch?v=pool4", _pool_settings())

    assert captured["options"]["proxy"] == "http://127.0.0.1:7890"


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

    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp.YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp.time.sleep", lambda _: None)

    info = _extract_info_with_retries(
        "https://www.youtube.com/watch?v=HFuWyTmZxAc",
        {},
        policy=youtube.policy(),
    )

    assert info["id"] == "HFuWyTmZxAc"
    assert attempts[0].get("extractor_args") is None
    assert attempts[1]["extractor_args"]["youtube"]["player_client"] == list(
        YOUTUBE_RETRY_CLIENTS[1]
    )
    assert attempts[2]["extractor_args"]["youtube"]["player_client"] == list(
        YOUTUBE_RETRY_CLIENTS[2]
    )


def test_youtube_retries_curly_apostrophe_bot_challenge(monkeypatch):
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
            if len(attempts) == 1:
                raise DownloadError("Sign in to confirm you’re not a bot")
            return {"id": "QQxb0En8j-o"}

    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp.YoutubeDL", FakeYoutubeDL)
    monkeypatch.setattr("downloadany_backend.media_parsing.ytdlp.time.sleep", lambda _: None)

    info = _extract_info_with_retries(
        "https://www.youtube.com/watch?v=QQxb0En8j-o", {}, policy=youtube.policy()
    )

    assert info["id"] == "QQxb0En8j-o"
    assert len(attempts) == 2
    assert attempts[1]["extractor_args"]["youtube"]["player_client"] == list(
        YOUTUBE_RETRY_CLIENTS[1]
    )


def test_direct_resolve_reports_format_unavailable_as_parse_error(monkeypatch):
    monkeypatch.setattr(
        "downloadany_backend.media_parsing.ytdlp._extract_info_with_retries",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            DownloadError("ERROR: Requested format is not available")
        ),
    )

    with pytest.raises(MediaParseError, match="Requested format is not available"):
        resolve_direct_video(
            "https://www.mgtv.com/b/912960/24797136.html",
            Settings(
                jwt_secret="test-secret-that-is-at-least-32-characters-long",
                ytdlp_proxy="http://127.0.0.1:7890",
            ),
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

    monkeypatch.setattr("downloadany_backend.media_parsing.bilibili._bilibili_json", fake_bilibili_json)
    monkeypatch.setattr("downloadany_backend.media_parsing.bilibili.validate_extracted_media_url", lambda url, **kwargs: url)
    result = resolve_bilibili(
        "https://www.bilibili.com/video/BV1TLW46nEvE/?p=1",
        Settings(jwt_secret="test-secret-that-is-at-least-32-characters-long"),
    )

    assert result["video_url"] == "https://cdn.example/video.mp4?deadline=1794395326"
    assert result["quality"] == "mp4720"
    assert result["filesize"] == 2048
    assert result["formats"][0]["has_audio"] is True
    assert calls[1][1]["platform"] == "html5"


def test_resolve_wechat_channels_uses_authorized_feed_when_anonymous_hides_video(monkeypatch):
    calls = []

    def fake_wechat_json(session, url, *, payload, headers, settings, params=None):
        calls.append((url, payload, headers, params))
        if "yuanbao.tencent.com" in url:
            return {
                "data": {
                    "wx_export_id": "export-fallback",
                    "playable_url": "https://channels.weixin.qq.com/finder-preview/pages/feed"
                    "?token=temporary-token&eid=export-123",
                }
            }
        if payload.get("shortUri"):
            return {
                "errCode": 0,
                "data": {
                    "feedInfo": {"coverUrl": "https://finder.video.qq.com/cover.jpg"},
                    "authorInfo": {"nickname": "作者"},
                    "errMsg": {"type": 0},
                },
            }
        return {
            "errCode": 0,
            "data": {
                "feedInfo": {
                    "description": "测试视频号",
                    "coverUrl": "https://finder.video.qq.com/cover.jpg",
                    "videoDuration": "42",
                    "h264VideoInfo": {
                        "videoUrl": "https://finder.video.qq.com/251/20302/stodownload?token=signed",
                        "width": 1920,
                        "height": 1080,
                    },
                },
                "authorInfo": {"nickname": "作者"},
                "sceneInfo": {"dynamicExportId": "dynamic-123"},
                "errMsg": {"type": 0},
            },
        }

    monkeypatch.setattr("downloadany_backend.media_parsing.wechat._wechat_json", fake_wechat_json)
    monkeypatch.setattr(
        "downloadany_backend.media_parsing.wechat.validate_extracted_media_url", lambda url, **kwargs: url
    )
    result = resolve_wechat_channels(
        "https://weixin.qq.com/sph/A9aMPLihxL",
        Settings(
            jwt_secret="test-secret-that-is-at-least-32-characters-long",
            yuanbao_cookie="hy_user=test; hy_token=secret",
        ),
    )

    assert result["platform"] == "wechat-channels"
    assert result["id"] == "dynamic-123"
    assert result["title"] == "测试视频号"
    assert result["uploader"] == "作者"
    assert result["duration"] == 42
    assert result["formats"][0]["label"] == "原画 · H264 · 音视频"
    assert result["formats"][0]["quality"] == 1080
    assert calls[0][1]["shortUri"] == "A9aMPLihxL"
    assert calls[1][2]["Cookie"] == "hy_user=test; hy_token=secret"
    assert calls[2][1] == {
        "baseReq": {"generalToken": "temporary-token"},
        "exportId": "export-123",
    }


def test_resolve_wechat_channels_explains_missing_server_credential(monkeypatch):
    monkeypatch.setattr("downloadany_backend.media_parsing.wechat._yuanbao_cookie", lambda settings: "")
    monkeypatch.setattr(
        "downloadany_backend.media_parsing.wechat._wechat_feed_info",
        lambda *args, **kwargs: {
            "feedInfo": {"coverUrl": "https://finder.video.qq.com/cover.jpg"},
            "errMsg": {"type": 0},
        },
    )

    with pytest.raises(MediaParseError, match="配置腾讯元宝登录凭证"):
        resolve_wechat_channels(
            "https://weixin.qq.com/sph/A9aMPLihxL",
            Settings(jwt_secret="test-secret-that-is-at-least-32-characters-long"),
        )


def test_resolve_wechat_channels_reads_local_cookie_file(monkeypatch, tmp_path):
    cookie_file = tmp_path / "yuanbao-cookies.txt"
    cookie_file.write_text(
        ".tencent.com\tTRUE\t/\tTRUE\t0\thy_user\ttest-user\n"
        ".tencent.com\tTRUE\t/\tTRUE\t0\thy_token\ttest-token\n",
        encoding="utf-8",
    )
    captured = {}
    monkeypatch.setattr(
        "downloadany_backend.media_parsing.wechat._wechat_feed_info",
        lambda *args, **kwargs: (
            {"feedInfo": {"coverUrl": "https://finder.video.qq.com/cover.jpg"}}
            if kwargs.get("short_uri")
            else {"feedInfo": {}}
        ),
    )

    def fake_json(session, url, *, payload, headers, settings, params=None):
        captured["cookie"] = headers["Cookie"]
        return {
            "data": {
                "playable_url": "https://channels.weixin.qq.com/feed?token=t&eid=e"
            }
        }

    monkeypatch.setattr("downloadany_backend.media_parsing.wechat._wechat_json", fake_json)
    with pytest.raises(MediaParseError, match="没有返回可下载的视频流"):
        resolve_wechat_channels(
            "https://weixin.qq.com/sph/A9aMPLihxL",
            Settings(
                jwt_secret="test-secret-that-is-at-least-32-characters-long",
                yuanbao_cookie_file=str(cookie_file),
            ),
        )
    assert captured["cookie"] == "hy_user=test-user; hy_token=test-token"


def test_desktop_wechat_parse_hands_signed_cdn_url_to_local_downloader(monkeypatch):
    monkeypatch.setattr(
        "downloadany_backend.media_parsing.wechat.resolve_wechat_channels",
        lambda url, settings: {
            "id": "dynamic-123",
            "title": "测试视频号",
            "duration": 42,
            "thumbnail": "https://finder.video.qq.com/cover.jpg",
            "uploader": "作者",
            "video_url": "https://finder.video.qq.com/251/20302/stodownload?token=signed",
            "filesize": 2048,
        },
    )

    result = parse_media(
        "https://weixin.qq.com/sph/A9aMPLihxL",
        Settings(
            jwt_secret="test-secret-that-is-at-least-32-characters-long",
            ytdlp_proxy="http://127.0.0.1:7890",
        ),
    )

    assert result["webpage_url"].startswith("https://finder.video.qq.com/")
    assert [item["id"] for item in result["formats"]] == [
        "__best__",
        "__audio_mp3__",
    ]


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


def test_beijing_day_start_uses_utc8_midnight():
    from downloadany_backend.media_routes import beijing_day_start

    # 北京 23:30（当天 11-23）→ 日界 = 11-22 16:00 UTC
    assert beijing_day_start(datetime(2026, 11, 23, 15, 30)) == datetime(2026, 11, 22, 16, 0)
    # 北京 00:30（次日 11-24）→ 日界 = 11-23 16:00 UTC
    assert beijing_day_start(datetime(2026, 11, 23, 16, 30)) == datetime(2026, 11, 23, 16, 0)


@pytest.mark.asyncio
async def test_anonymous_resolve_allows_one_per_beijing_day(client, monkeypatch):
    monkeypatch.setattr("downloadany_backend.media_routes.resolve_direct_video", resolved_video)

    ok = await client.post("/api/media/resolve", json={"url": "https://www.youtube.com/watch?v=video"})
    assert ok.status_code == 200

    limited = await client.post("/api/media/resolve", json={"url": "https://www.youtube.com/watch?v=video"})
    assert limited.status_code == 403
    assert limited.json()["detail"]["code"] == "anonymous_daily_limit"


@pytest.mark.asyncio
async def test_anonymous_quota_resets_next_beijing_day(client, monkeypatch, db):
    monkeypatch.setattr("downloadany_backend.media_routes.resolve_direct_video", resolved_video)

    ok = await client.post("/api/media/resolve", json={"url": "https://www.youtube.com/watch?v=video"})
    assert ok.status_code == 200

    attempt = db.scalar(select(MediaResolveAttempt))
    attempt.created_at = utcnow() - timedelta(days=2)
    db.commit()

    again = await client.post("/api/media/resolve", json={"url": "https://www.youtube.com/watch?v=video"})
    assert again.status_code == 200


@pytest.mark.asyncio
async def test_pending_anonymous_attempt_consumes_quota(client, monkeypatch, db):
    calls = 0

    def fail_once(url, settings):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise MediaParseError("测试失败")
        return resolved_video(url, settings)

    monkeypatch.setattr("downloadany_backend.media_routes.resolve_direct_video", fail_once)

    # 失败不占额度，但会留下当前测试客户端使用的 IP，供种子行复用。
    failed = await client.post("/api/media/resolve", json={"url": "https://www.youtube.com/watch?v=video"})
    assert failed.status_code == 422
    ip = db.scalar(select(MediaResolveAttempt.ip_address))

    db.add(MediaResolveAttempt(user_id=None, ip_address=ip, successful=None, created_at=utcnow()))
    db.commit()

    limited = await client.post("/api/media/resolve", json={"url": "https://www.youtube.com/watch?v=video"})
    assert limited.status_code == 403
    assert limited.json()["detail"]["code"] == "anonymous_daily_limit"


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
    failed = await client.post("/api/media/resolve", json={"url": "https://www.youtube.com/watch?v=video"})
    assert failed.status_code == 422

    ok = await client.post("/api/media/resolve", json={"url": "https://www.youtube.com/watch?v=video"})
    assert ok.status_code == 200


async def register_web_user(client, email="web-media@example.com"):
    password = "correct-horse-123"
    tokens = (
        await client.post(
            "/api/auth/register",
            json={
                "email": email,
                "password": password,
                "confirm_password": password,
            },
        )
    ).json()
    return {"Authorization": f"Bearer {tokens['access_token']}"}


def seed_user_attempts(db, email, count, *, successful=True, created_at=None):
    user_id = db.scalar(select(User.id).where(User.email == email))
    for _ in range(count):
        db.add(
            MediaResolveAttempt(
                user_id=user_id,
                ip_address="9.9.9.9",
                successful=successful,
                created_at=created_at or utcnow(),
            )
        )
    db.commit()


@pytest.mark.asyncio
async def test_logged_in_resolve_allows_three_per_beijing_day(client, monkeypatch):
    monkeypatch.setattr("downloadany_backend.media_routes.resolve_direct_video", resolved_video)
    headers = await register_web_user(client)

    for _ in range(3):
        response = await client.post(
            "/api/media/resolve",
            headers=headers,
            json={"url": "https://www.youtube.com/watch?v=video"},
        )
        assert response.status_code == 200

    # 第 4 次必须是额度 403，而不是先触发的频控 429（Review Focus #4）。
    limited = await client.post(
        "/api/media/resolve",
        headers=headers,
        json={"url": "https://www.youtube.com/watch?v=video"},
    )
    assert limited.status_code == 403
    assert limited.json()["detail"]["code"] == "user_daily_limit"


@pytest.mark.asyncio
async def test_user_daily_quota_counts_by_account_not_ip(client, monkeypatch, db):
    monkeypatch.setattr("downloadany_backend.media_routes.resolve_direct_video", resolved_video)
    email = "quota-ip@example.com"
    headers = await register_web_user(client, email)
    # 种子行的 IP 与测试客户端不同，仍应计入该账号额度（Review Focus #2）。
    seed_user_attempts(db, email, 3)

    limited = await client.post(
        "/api/media/resolve",
        headers=headers,
        json={"url": "https://www.youtube.com/watch?v=video"},
    )
    assert limited.status_code == 403
    assert limited.json()["detail"]["code"] == "user_daily_limit"


@pytest.mark.asyncio
async def test_user_quota_resets_next_beijing_day(client, monkeypatch, db):
    monkeypatch.setattr("downloadany_backend.media_routes.resolve_direct_video", resolved_video)
    email = "quota-reset@example.com"
    headers = await register_web_user(client, email)
    seed_user_attempts(db, email, 3, created_at=utcnow() - timedelta(days=2))

    response = await client.post(
        "/api/media/resolve",
        headers=headers,
        json={"url": "https://www.youtube.com/watch?v=video"},
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_pending_user_attempt_consumes_quota(client, monkeypatch, db):
    monkeypatch.setattr("downloadany_backend.media_routes.resolve_direct_video", resolved_video)
    email = "quota-pending@example.com"
    headers = await register_web_user(client, email)
    seed_user_attempts(db, email, 2)
    seed_user_attempts(db, email, 1, successful=None)

    limited = await client.post(
        "/api/media/resolve",
        headers=headers,
        json={"url": "https://www.youtube.com/watch?v=video"},
    )
    assert limited.status_code == 403
    assert limited.json()["detail"]["code"] == "user_daily_limit"


@pytest.mark.asyncio
async def test_failed_resolve_does_not_consume_user_quota(client, monkeypatch):
    calls = 0

    def fail_once(url, settings):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise MediaParseError("测试失败")
        return resolved_video(url, settings)

    monkeypatch.setattr("downloadany_backend.media_routes.resolve_direct_video", fail_once)
    headers = await register_web_user(client, "quota-fail@example.com")

    failed = await client.post(
        "/api/media/resolve",
        headers=headers,
        json={"url": "https://www.youtube.com/watch?v=video"},
    )
    assert failed.status_code == 422

    for _ in range(3):
        response = await client.post(
            "/api/media/resolve",
            headers=headers,
            json={"url": "https://www.youtube.com/watch?v=video"},
        )
        assert response.status_code == 200

    limited = await client.post(
        "/api/media/resolve",
        headers=headers,
        json={"url": "https://www.youtube.com/watch?v=video"},
    )
    assert limited.status_code == 403


@pytest.mark.asyncio
async def test_logged_in_resolve_rate_limited(client, monkeypatch):
    monkeypatch.setattr("downloadany_backend.media_routes.resolve_direct_video", resolved_video)
    # 抬高日额度以单独验证频控行为不回退（Review Focus #6）。
    monkeypatch.setattr(get_settings(), "user_daily_parse_limit", 50)
    headers = await register_web_user(client)

    for _ in range(10):
        response = await client.post(
            "/api/media/resolve",
            headers=headers,
            json={"url": "https://www.youtube.com/watch?v=video"},
        )
        assert response.status_code == 200

    limited = await client.post(
        "/api/media/resolve",
        headers=headers,
        json={"url": "https://www.youtube.com/watch?v=video"},
    )
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
