import ipaddress
import socket
from collections import OrderedDict
from urllib.parse import urlparse

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .config import Settings


FMT_BEST = "__best__"
FMT_AUDIO_MP3 = "__audio_mp3__"
FMT_THUMBNAIL = "__thumbnail__"
FMT_SUBTITLES = "__subtitles__"


class MediaParseError(Exception):
    pass


def validate_public_media_url(raw_url: str, *, resolve_hostname: bool = True) -> str:
    url = raw_url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise MediaParseError("链接必须是有效的 http:// 或 https:// 地址")
    try:
        literal_ip = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        if not literal_ip.is_global:
            raise MediaParseError("不允许解析本机或内网地址")
        return url
    if not resolve_hostname:
        return url
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, None)}
    except socket.gaierror as error:
        raise MediaParseError("无法解析链接域名") from error
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise MediaParseError("不允许解析本机或内网地址")
    return url


def _size(format_info: dict) -> int | None:
    value = format_info.get("filesize") or format_info.get("filesize_approx")
    return int(value) if isinstance(value, (int, float)) and value > 0 else None


def collapse_formats(info: dict) -> list[dict]:
    formats = info.get("formats") or []
    result: list[dict] = [
        {
            "id": FMT_BEST,
            "label": "最佳画质 + 音频（FFmpeg 合并）",
            "height": None,
            "ext": None,
            "filesize": _size(info),
        }
    ]

    best_audio_size = max(
        (
            _size(item) or 0
            for item in formats
            if item.get("vcodec", "none") == "none" and item.get("acodec", "none") != "none"
        ),
        default=0,
    )
    resolutions: OrderedDict[int, int] = OrderedDict()
    for item in formats:
        if item.get("vcodec", "none") == "none":
            continue
        height = item.get("height")
        if not isinstance(height, (int, float)) or height <= 0:
            continue
        height = int(height)
        resolutions[height] = max(resolutions.get(height, 0), _size(item) or 0)

    for height in sorted(resolutions, reverse=True)[:8]:
        video_size = resolutions[height]
        total_size = video_size + best_audio_size
        result.append(
            {
                "id": f"__video_{height}p__",
                "label": f"{height}p",
                "height": height,
                "ext": None,
                "filesize": total_size or None,
            }
        )

    result.append(
        {
            "id": FMT_AUDIO_MP3,
            "label": "仅音频（MP3 提取）",
            "height": None,
            "ext": "mp3",
            "filesize": None,
        }
    )
    if info.get("thumbnail"):
        result.append(
            {
                "id": FMT_THUMBNAIL,
                "label": "封面图片",
                "height": None,
                "ext": None,
                "filesize": None,
            }
        )
    if info.get("subtitles") or info.get("automatic_captions"):
        result.append(
            {
                "id": FMT_SUBTITLES,
                "label": "字幕（SRT）",
                "height": None,
                "ext": "srt",
                "filesize": None,
            }
        )
    return result


def parse_media(raw_url: str, settings: Settings) -> dict:
    url = validate_public_media_url(raw_url, resolve_hostname=not bool(settings.ytdlp_proxy))
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": settings.ytdlp_socket_timeout,
        "retries": 1,
        "extractor_retries": 1,
    }
    if settings.ytdlp_proxy:
        options["proxy"] = settings.ytdlp_proxy
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as error:
        message = str(error).removeprefix("ERROR: ").strip()
        raise MediaParseError(message or "yt-dlp 解析失败") from error
    if not isinstance(info, dict) or info.get("entries") is not None:
        raise MediaParseError("暂不支持播放列表或频道链接")
    return {
        "id": str(info.get("id") or ""),
        "title": str(info.get("title") or "(无标题)"),
        "duration": info.get("duration"),
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader"),
        "webpage_url": info.get("webpage_url") or url,
        "formats": collapse_formats(info),
    }
