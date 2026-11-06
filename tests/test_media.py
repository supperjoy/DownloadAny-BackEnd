import pytest

from downloadany_backend.media import MediaParseError, collapse_formats, validate_public_media_url


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
        "__subtitles__",
    ]
    assert formats[1]["filesize"] == 1_200


@pytest.mark.parametrize("url", ["http://127.0.0.1/test", "http://[::1]/test", "ftp://example.com/file"])
def test_media_url_rejects_private_or_unsupported_targets(url):
    with pytest.raises(MediaParseError):
        validate_public_media_url(url, resolve_hostname=False)
