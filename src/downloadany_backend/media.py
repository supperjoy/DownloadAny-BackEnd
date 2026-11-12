import ipaddress
import re
import socket
from collections import OrderedDict
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import requests
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .config import Settings


FMT_BEST = "__best__"
FMT_AUDIO_MP3 = "__audio_mp3__"
FMT_THUMBNAIL = "__thumbnail__"


class MediaParseError(Exception):
    pass


class _QuietYoutubeDLLogger:
    def debug(self, _: str) -> None:
        pass

    def info(self, _: str) -> None:
        pass

    def warning(self, _: str) -> None:
        pass

    def error(self, _: str) -> None:
        pass


BILIBILI_HOSTS = {"bilibili.com", "www.bilibili.com"}
TIKTOK_HOSTS = {"tiktok.com", "tiktokv.com"}
TRUSTED_MEDIA_CDN_DOMAINS = {
    "bilivideo.com",
    "googlevideo.com",
    "tiktok.com",
    "tiktokcdn.com",
    "tiktokv.com",
    "tokcdn.com",
}
BILIBILI_VIEW_API = "https://api.bilibili.com/x/web-interface/view"
BILIBILI_PLAY_API = "https://api.bilibili.com/x/player/playurl"
BILIBILI_BVID_PATTERN = re.compile(r"(?i)(BV[0-9A-Za-z]{10})")
DIRECT_VIDEO_FORMAT = (
    "best[protocol^=http][vcodec!=none][acodec!=none][ext=mp4]/"
    "best[protocol^=http][vcodec!=none][acodec!=none]/"
    "bestvideo[protocol^=http][ext=mp4]/"
    "bestvideo[protocol^=http]"
)
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
TIKTOK_RETRY_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) Gecko/20100101 Firefox/154.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 OPR/118.0.0.0",
)
YTDLP_QUIET_LOGGER = _QuietYoutubeDLLogger()


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


def validate_extracted_media_url(raw_url: str) -> str:
    parsed = urlparse(raw_url.strip())
    hostname = (parsed.hostname or "").lower()
    trusted_cdn = _hostname_matches(hostname, TRUSTED_MEDIA_CDN_DOMAINS)
    return validate_public_media_url(raw_url, resolve_hostname=not trusted_cdn)


def _size(format_info: dict) -> int | None:
    value = format_info.get("filesize") or format_info.get("filesize_approx")
    return int(value) if isinstance(value, (int, float)) and value > 0 else None


def _safe_subtitle_language(language: str) -> bool:
    return (
        bool(language)
        and language != "live_chat"
        and len(language) <= 35
        and all(character.isascii() and (character.isalnum() or character in "-_") for character in language)
    )


def _subtitle_languages(info: dict) -> list[str]:
    preferred = ("zh-Hans", "zh-CN", "zh", "zh-Hant", "en-orig", "en")
    manual = info.get("subtitles") if isinstance(info.get("subtitles"), dict) else {}
    automatic = (
        info.get("automatic_captions")
        if isinstance(info.get("automatic_captions"), dict)
        else {}
    )
    available = {
        str(language)
        for language in manual
        if _safe_subtitle_language(str(language))
    }
    available.update(
        language
        for language in preferred
        if language in automatic and _safe_subtitle_language(language)
    )
    ordered = [language for language in preferred if language in available]
    ordered.extend(sorted(available.difference(ordered)))
    return ordered


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
    for language in _subtitle_languages(info):
        result.append(
            {
                "id": f"__subtitle_{language}__",
                "label": f"{language}（SRT）",
                "height": None,
                "ext": "srt",
                "filesize": None,
            }
        )
    return result


def _request_proxies(settings: Settings) -> dict[str, str] | None:
    if not settings.ytdlp_proxy:
        return None
    return {"http": settings.ytdlp_proxy, "https": settings.ytdlp_proxy}


def _bilibili_json(url: str, *, params: dict, referer: str, settings: Settings) -> dict:
    try:
        response = requests.get(
            url,
            params=params,
            headers={"Referer": referer, "User-Agent": DEFAULT_USER_AGENT},
            proxies=_request_proxies(settings),
            timeout=settings.ytdlp_socket_timeout,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as error:
        raise MediaParseError("Bilibili 播放信息请求失败") from error
    if not isinstance(payload, dict) or payload.get("code") != 0:
        message = payload.get("message") if isinstance(payload, dict) else None
        raise MediaParseError(str(message or "Bilibili 无法解析该视频"))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise MediaParseError("Bilibili 返回了无效的播放信息")
    return data


def _url_expiry(url: str) -> str | None:
    query = parse_qs(urlparse(url).query)
    values = query.get("deadline") or query.get("expire")
    if not values:
        return None
    try:
        return datetime.fromtimestamp(int(values[0]), tz=UTC).isoformat()
    except (ValueError, OSError):
        return None


def _display_height(format_info: dict) -> int | None:
    note_match = re.search(r"(\d{3,4})p", str(format_info.get("format_note") or ""), re.IGNORECASE)
    if note_match:
        return int(note_match.group(1))
    id_match = re.search(r"_(\d{3,4})p(?:_|$)", str(format_info.get("format_id") or ""), re.IGNORECASE)
    if id_match:
        return int(id_match.group(1))
    width = format_info.get("width")
    height = format_info.get("height")
    dimensions = [int(value) for value in (width, height) if isinstance(value, (int, float)) and value > 0]
    return min(dimensions) if dimensions else None


def _codec_priority(format_info: dict) -> int:
    codec = str(format_info.get("vcodec") or "").lower()
    if codec.startswith(("avc", "h264")):
        return 3
    if codec.startswith("av01"):
        return 2
    return 1


def direct_video_formats(info: dict) -> list[dict]:
    selected: dict[tuple[int, int, bool], dict] = {}
    for item in info.get("formats") or []:
        url = item.get("url")
        protocol = str(item.get("protocol") or "")
        if (
            not isinstance(url, str)
            or not url.startswith(("http://", "https://"))
            or protocol.startswith("m3u8")
            or item.get("vcodec", "none") == "none"
            or item.get("ext") != "mp4"
        ):
            continue
        quality = _display_height(item)
        if not quality:
            continue
        fps = int(item.get("fps") or 0)
        has_audio = item.get("acodec", "none") != "none"
        key = (quality, fps, has_audio)
        current = selected.get(key)
        score = (_codec_priority(item), _size(item) is not None, float(item.get("tbr") or 0))
        current_score = (
            (_codec_priority(current), _size(current) is not None, float(current.get("tbr") or 0))
            if current
            else (-1, False, -1.0)
        )
        if score > current_score:
            selected[key] = item

    result = []
    for (quality, fps, has_audio), item in selected.items():
        fps_text = f" {fps}FPS" if fps > 30 else ""
        audio_text = "音视频" if has_audio else "仅视频"
        result.append(
            {
                "id": str(item.get("format_id") or ""),
                "label": f"{quality}P{fps_text} · {audio_text}",
                "url": item["url"],
                "ext": item.get("ext"),
                "width": item.get("width"),
                "height": item.get("height"),
                "quality": quality,
                "fps": item.get("fps"),
                "filesize": _size(item),
                "vcodec": item.get("vcodec"),
                "acodec": item.get("acodec"),
                "has_audio": has_audio,
                "expires_at": _url_expiry(item["url"]),
            }
        )
    return sorted(
        result,
        key=lambda item: (item["quality"], item["fps"] or 0, item["has_audio"]),
        reverse=True,
    )


def direct_audio_formats(info: dict) -> list[dict]:
    candidates = []
    for item in info.get("formats") or []:
        url = item.get("url")
        protocol = str(item.get("protocol") or "")
        if (
            not isinstance(url, str)
            or not url.startswith(("http://", "https://"))
            or protocol.startswith("m3u8")
            or item.get("vcodec", "none") != "none"
            or item.get("acodec", "none") == "none"
            or item.get("ext") not in {"m4a", "mp4", "mp3"}
        ):
            continue
        abr = item.get("abr") or item.get("tbr")
        language = item.get("language")
        bitrate = f" {int(abr)}K" if isinstance(abr, (int, float)) and abr > 0 else ""
        label = f"{str(item.get('ext') or 'm4a').upper()}{bitrate}"
        if language and language not in label:
            label = f"{label} · {language}"
        candidates.append(
            {
                "id": str(item.get("format_id") or ""),
                "label": label,
                "url": url,
                "ext": item.get("ext"),
                "abr": abr,
                "filesize": _size(item),
                "acodec": item.get("acodec"),
                "language": language,
                "expires_at": _url_expiry(url),
                "_score": (
                    item.get("ext") == "m4a",
                    float(item.get("language_preference") or 0),
                    float(abr or 0),
                    _size(item) or 0,
                ),
            }
        )
    candidates.sort(key=lambda item: item["_score"], reverse=True)
    for item in candidates:
        item.pop("_score", None)
    return candidates


def _hostname_matches(hostname: str, domains: set[str]) -> bool:
    return hostname in domains or any(hostname.endswith(f".{domain}") for domain in domains)


def _is_tiktok_retryable(error: DownloadError) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "unexpected response from webpage request",
            "unable to extract webpage video data",
            "unable to extract universal data",
            "failed to parse json",
        )
    )


def _extract_info_with_retries(url: str, options: dict, *, tiktok: bool = False) -> dict:
    user_agents: tuple[str | None, ...] = (None, *TIKTOK_RETRY_USER_AGENTS) if tiktok else (None,)
    last_error: DownloadError | None = None
    for index, user_agent in enumerate(user_agents):
        attempt_options = dict(options)
        if tiktok:
            attempt_options["logger"] = YTDLP_QUIET_LOGGER
        if user_agent:
            headers = dict(attempt_options.get("http_headers") or {})
            headers["User-Agent"] = user_agent
            attempt_options["http_headers"] = headers
        try:
            with YoutubeDL(attempt_options) as ydl:
                return ydl.extract_info(url, download=False)
        except DownloadError as error:
            last_error = error
            if not tiktok or (index == 0 and not _is_tiktok_retryable(error)):
                raise
    assert last_error is not None
    raise last_error


def resolve_bilibili(raw_url: str, settings: Settings) -> dict:
    match = BILIBILI_BVID_PATTERN.search(raw_url)
    if not match:
        raise MediaParseError("未在 Bilibili 链接中找到 BV 号")
    bvid = match.group(1)
    webpage_url = f"https://www.bilibili.com/video/{bvid}/"
    view = _bilibili_json(
        BILIBILI_VIEW_API,
        params={"bvid": bvid},
        referer=webpage_url,
        settings=settings,
    )
    pages = view.get("pages") or []
    requested_page = parse_qs(urlparse(raw_url).query).get("p", ["1"])[0]
    try:
        page_index = max(0, int(requested_page) - 1)
    except ValueError:
        page_index = 0
    if not pages or page_index >= len(pages) or not pages[page_index].get("cid"):
        raise MediaParseError("Bilibili 视频分 P 信息不存在")
    page = pages[page_index]
    play = _bilibili_json(
        BILIBILI_PLAY_API,
        params={
            "bvid": bvid,
            "cid": page["cid"],
            "qn": 64,
            "fnval": 0,
            "fnver": 0,
            "fourk": 1,
            "platform": "html5",
            "high_quality": 1,
        },
        referer=webpage_url,
        settings=settings,
    )
    parts = play.get("durl") or []
    if len(parts) != 1 or not parts[0].get("url"):
        raise MediaParseError("该 Bilibili 视频无法生成单文件播放地址")
    media = parts[0]
    video_url = str(media["url"])
    validate_extracted_media_url(video_url)
    owner = view.get("owner") or {}
    return {
        "platform": "bilibili",
        "id": bvid,
        "title": str(view.get("title") or page.get("part") or "(无标题)"),
        "video_url": video_url,
        "webpage_url": webpage_url,
        "thumbnail": view.get("pic"),
        "uploader": owner.get("name"),
        "duration": page.get("duration") or view.get("duration"),
        "quality": str(play.get("format") or "mp4"),
        "ext": "mp4",
        "filesize": media.get("size"),
        "expires_at": _url_expiry(video_url),
        "audio_url": None,
        "audio_formats": [],
        "formats": [
            {
                "id": str(play.get("quality") or "64"),
                "label": "720P · 音视频",
                "url": video_url,
                "ext": "mp4",
                "width": None,
                "height": 720,
                "quality": 720,
                "fps": None,
                "filesize": media.get("size"),
                "vcodec": None,
                "acodec": None,
                "has_audio": True,
                "expires_at": _url_expiry(video_url),
            }
        ],
    }


def resolve_direct_video(raw_url: str, settings: Settings) -> dict:
    url = validate_public_media_url(raw_url, resolve_hostname=not bool(settings.ytdlp_proxy))
    hostname = (urlparse(url).hostname or "").lower()
    if _hostname_matches(hostname, BILIBILI_HOSTS):
        return resolve_bilibili(url, settings)
    is_tiktok = _hostname_matches(hostname, TIKTOK_HOSTS)

    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "format": DIRECT_VIDEO_FORMAT,
        "socket_timeout": settings.ytdlp_socket_timeout,
        "retries": 1,
        "extractor_retries": 1,
    }
    if settings.ytdlp_proxy:
        options["proxy"] = settings.ytdlp_proxy
    try:
        info = _extract_info_with_retries(url, options, tiktok=is_tiktok)
    except DownloadError as error:
        message = str(error).removeprefix("ERROR: ").strip()
        raise MediaParseError(message or "yt-dlp 解析失败") from error
    if not isinstance(info, dict) or info.get("entries") is not None:
        raise MediaParseError("暂不支持播放列表或频道链接")
    formats = direct_video_formats(info)
    audio_formats = direct_audio_formats(info)
    video_url = info.get("url")
    if not isinstance(video_url, str) or not video_url:
        raise MediaParseError("该视频没有可直接播放的音视频合一格式")
    validate_extracted_media_url(video_url)
    return {
        "platform": str(info.get("extractor_key") or info.get("extractor") or "generic").lower(),
        "id": str(info.get("id") or ""),
        "title": str(info.get("title") or "(无标题)"),
        "video_url": video_url,
        "webpage_url": info.get("webpage_url") or url,
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader"),
        "duration": info.get("duration"),
        "quality": info.get("format_note") or info.get("resolution") or info.get("format_id"),
        "ext": info.get("ext"),
        "filesize": _size(info),
        "expires_at": _url_expiry(video_url),
        "audio_url": audio_formats[0]["url"] if audio_formats else None,
        "audio_formats": audio_formats,
        "formats": formats,
    }


def parse_media(raw_url: str, settings: Settings) -> dict:
    url = validate_public_media_url(raw_url, resolve_hostname=not bool(settings.ytdlp_proxy))
    hostname = (urlparse(url).hostname or "").lower()
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
        info = _extract_info_with_retries(
            url,
            options,
            tiktok=_hostname_matches(hostname, TIKTOK_HOSTS),
        )
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
