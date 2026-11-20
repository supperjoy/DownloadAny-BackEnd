"""Translate yt-dlp metadata once, without exposing it to the API layer."""


from .common import (
    MediaParseError,
    _size,
    _url_expiry,
    collapse_formats,
    direct_audio_formats,
    direct_image_formats,
    direct_video_formats,
    validate_extracted_media_url,
)
from .result import MediaResult


def normalize(info: dict, url: str, *, direct: bool, trust: bool = False,
              progressive: bool = False) -> MediaResult:
    if not isinstance(info, dict) or info.get("entries") is not None:
        raise MediaParseError("暂不支持播放列表或频道链接")
    result = MediaResult(
        id=str(info.get("id") or ""), title=str(info.get("title") or "(无标题)"),
        platform=str(info.get("extractor_key") or info.get("extractor") or "generic").lower(),
        webpage_url=info.get("webpage_url") or url, thumbnail=info.get("thumbnail"),
        uploader=info.get("uploader"), duration=info.get("duration"),
        images=direct_image_formats(info, trust_extractor=trust),
        download_options=collapse_formats(info),
    )
    if not direct:
        return result
    result.formats = direct_video_formats(info)
    result.audio_formats = direct_audio_formats(info)
    selected = None
    video_url = info.get("url")
    if progressive:
        selected = next((item for item in result.formats if item["has_audio"]), None)
        if selected:
            video_url = selected["url"]
            result.formats = [selected, *[item for item in result.formats if item is not selected]]
        elif not (
            isinstance(video_url, str) and video_url.startswith(("http://", "https://"))
            and info.get("vcodec", "none") != "none"
            and info.get("acodec", "none") != "none" and info.get("ext") == "mp4"
            and not str(info.get("protocol") or "").startswith("m3u8")
        ):
            raise MediaParseError("该视频没有可直接下载的音视频合一 MP4 地址")
        else:
            result.formats = []
    if not isinstance(video_url, str) or not video_url:
        raise MediaParseError("该视频没有可直接播放的音视频合一格式")
    validate_extracted_media_url(video_url, trust_extractor=trust)
    result.video_url = video_url
    result.quality = selected["label"] if selected else info.get("format_note") or info.get("resolution") or info.get("format_id")
    result.ext = selected["ext"] if selected else info.get("ext")
    result.filesize = selected["filesize"] if selected else _size(info)
    result.expires_at = _url_expiry(video_url)
    result.audio_url = result.audio_formats[0]["url"] if result.audio_formats else None
    return result
