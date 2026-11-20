"""Shared URL validation, media format utilities and configuration constants."""


from collections import OrderedDict
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse
import html
import ipaddress
import re
import socket

from .. import proxy_pool
from ..config import Settings
from .network import current_proxy_url


FMT_BEST = "__best__"

FMT_AUDIO_MP3 = "__audio_mp3__"

FMT_THUMBNAIL = "__thumbnail__"

class MediaParseError(Exception):
    pass

SOURCE_URL_RE = re.compile(
    r"https?\\?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+",
    re.IGNORECASE,
)

SOURCE_URL_TRAILING = ".,;:!?\"')]}>。，；：！？”’》〉】〕〗〙〛"

def extract_source_url(raw: str) -> str:
    """Extract the first HTTP(S) URL from a URL or copied share caption."""
    text = html.unescape((raw or "").strip())
    match = SOURCE_URL_RE.search(text)
    if not match:
        raise MediaParseError("未在输入内容中找到有效的 http:// 或 https:// 链接")
    return match.group(0).replace(r"\://", "://", 1).rstrip(SOURCE_URL_TRAILING)

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

DOUYIN_HOSTS = {"douyin.com", "iesdouyin.com"}

XIAOHONGSHU_HOSTS = {"xiaohongshu.com", "xhslink.com"}

YOUTUBE_HOSTS = {"youtube.com", "youtube-nocookie.com", "youtu.be"}

WECHAT_CHANNELS_HOSTS = {"weixin.qq.com", "channels.weixin.qq.com"}

TRUSTED_MEDIA_CDN_DOMAINS = {
    "bilivideo.com",
    "googlevideo.com",
    "tiktok.com",
    "tiktokcdn.com",
    "tiktokv.com",
    "tokcdn.com",
    "douyinpic.com",
    "douyinvod.com",
    "z1cdn.com",
}

BILIBILI_VIEW_API = "https://api.bilibili.com/x/web-interface/view"

BILIBILI_PLAY_API = "https://api.bilibili.com/x/player/playurl"

BILIBILI_HOME_URL = "https://www.bilibili.com/"

BILIBILI_BVID_PATTERN = re.compile(r"(?i)(BV[0-9A-Za-z]{10})")

WECHAT_CHANNELS_SHORT_URI_PATTERN = re.compile(r"^[A-Za-z0-9]+")

WECHAT_CHANNELS_FEED_API = (
    "https://channels.weixin.qq.com/finder-preview/api/feed/get_feed_info"
)

YUANBAO_WECHAT_PARSE_API = "https://yuanbao.tencent.com/api/weixin/get_parse_result"

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


DOUYIN_RETRY_USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) Gecko/20100101 Firefox/154.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36 OPR/118.0.0.0",
)

XIAOHONGSHU_SHORT_HOSTS = {"xhslink.com", "xhslink.cn"}

XIAOHONGSHU_LOGIN_HOSTS = {"xiaohongshu.com", "rednote.com"}

XIAOHONGSHU_HOME_URL = "https://www.xiaohongshu.com/"

XIAOHONGSHU_NOTE_PATH_RE = re.compile(r"^/(?:explore|discovery/item)/[0-9a-fA-F]{8,}")

XIAOHONGSHU_RETRY_USER_AGENTS = (
    DEFAULT_USER_AGENT,
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:154.0) Gecko/20100101 Firefox/154.0",
)

YOUTUBE_RETRY_CLIENTS: tuple[tuple[str, ...] | None, ...] = (
    None,
    ("visionos",),
    ("web_safari", "ios"),
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

def validate_extracted_media_url(raw_url: str, *, trust_extractor: bool = False) -> str:
    parsed = urlparse(raw_url.strip())
    hostname = (parsed.hostname or "").lower()
    trusted_cdn = _hostname_matches(hostname, TRUSTED_MEDIA_CDN_DOMAINS)
    return validate_public_media_url(raw_url, resolve_hostname=not (trusted_cdn or trust_extractor))

def validate_source_media_url(raw_url: str, settings: Settings) -> str:
    source_url = extract_source_url(raw_url)
    hostname = (urlparse(source_url).hostname or "").lower()
    known_platform = _hostname_matches(hostname, DOUYIN_HOSTS | TIKTOK_HOSTS)
    pooled_platform = _hostname_matches(hostname, DOUYIN_HOSTS | TIKTOK_HOSTS | YOUTUBE_HOSTS)
    return validate_public_media_url(
        source_url,
        resolve_hostname=not (
            settings.ytdlp_proxy
            or known_platform
            or (pooled_platform and proxy_pool.enabled(settings))
        ),
    )

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
        height = _display_height(item)
        if not height:
            continue
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
    proxy = current_proxy_url(settings)
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}

def _url_expiry(url: str) -> str | None:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    values = query.get("deadline") or query.get("expire") or query.get("x-expires")
    try:
        if values:
            return datetime.fromtimestamp(int(values[0]), tz=UTC).isoformat()
        # Douyin CDN URLs embed the expiry as the second path segment.
        if _hostname_matches(
            (parsed.hostname or "").lower(),
            {"douyinvod.com", "ixigua.com", "zjcdn.com"},
        ):
            match = re.match(r"^/[0-9a-f]{32}/([0-9a-f]{8})/video/", parsed.path, re.I)
            if match:
                return datetime.fromtimestamp(int(match.group(1), 16), tz=UTC).isoformat()
    except (ValueError, OSError):
        return None
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

def direct_image_formats(info: dict, *, trust_extractor: bool = False) -> list[dict]:
    result = []
    for index, image in enumerate(info.get("images") or [], 1):
        url = image.get("url") if isinstance(image, dict) else None
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        if trust_extractor:
            validate_extracted_media_url(url, trust_extractor=True)
        else:
            validate_extracted_media_url(url)
        path = urlparse(url).path
        extension = path.rsplit(".", 1)[-1].lower() if "." in path else "webp"
        if extension not in {"jpg", "jpeg", "png", "webp", "avif"}:
            extension = "webp"
        result.append(
            {
                "id": str(image.get("id") or index),
                "label": f"图片 {index}",
                "url": url,
                "ext": extension,
                "width": image.get("width"),
                "height": image.get("height"),
                "filesize": image.get("filesize"),
                "expires_at": _url_expiry(url),
            }
        )
    return result

def _hostname_matches(hostname: str, domains: set[str]) -> bool:
    return hostname in domains or any(hostname.endswith(f".{domain}") for domain in domains)
