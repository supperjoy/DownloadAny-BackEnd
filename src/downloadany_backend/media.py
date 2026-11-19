import html
import ipaddress
import re
import socket
import threading
import time
import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlunparse

import requests
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from . import proxy_pool
from .config import Settings
from .douyin_community import DouyinIE


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
_bilibili_session_state = threading.local()
TIKTOK_RETRY_USER_AGENTS = (
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
    return validate_public_media_url(
        source_url,
        resolve_hostname=not (settings.ytdlp_proxy or known_platform or proxy_pool.enabled(settings)),
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
    if not settings.ytdlp_proxy:
        return None
    return {"http": settings.ytdlp_proxy, "https": settings.ytdlp_proxy}


def _extraction_proxy(settings: Settings) -> str:
    """yt-dlp 出口代理：mihomo 池可用时先随机固定一个测活通过的节点走混合端口，
    切换失败（未配置/控制端不可达/全灭）回退 ytdlp_proxy。"""
    if proxy_pool.enabled(settings) and proxy_pool.switch_to_random_active(settings):
        return settings.mihomo_proxy_url
    return settings.ytdlp_proxy


def _bilibili_session() -> requests.Session:
    session = getattr(_bilibili_session_state, "session", None)
    if session is None:
        session = requests.Session()
        _bilibili_session_state.session = session
    return session


def _bilibili_json(url: str, *, params: dict, referer: str, settings: Settings) -> dict:
    session = _bilibili_session()
    proxies = _request_proxies(settings)
    headers = {"Referer": referer, "User-Agent": DEFAULT_USER_AGENT}
    try:
        response = session.get(
            url,
            params=params,
            headers=headers,
            proxies=proxies,
            timeout=settings.ytdlp_socket_timeout,
        )
        if response.status_code == 412:
            # Bilibili commonly rejects datacenter IPs until the client has the
            # visitor cookies issued by its homepage. Refresh them and retry the
            # API once using the same thread-local session.
            session.cookies.clear()
            bootstrap = session.get(
                BILIBILI_HOME_URL,
                headers={"User-Agent": DEFAULT_USER_AGENT},
                proxies=proxies,
                timeout=settings.ytdlp_socket_timeout,
            )
            bootstrap.raise_for_status()
            response = session.get(
                url,
                params=params,
                headers=headers,
                proxies=proxies,
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


def _douyin_requires_referer(url: object) -> bool:
    return isinstance(url, str) and _hostname_matches(
        (urlparse(url).hostname or "").lower(), {"douyinvod.com"}
    )


def _douyin_browser_info(info: dict) -> dict:
    # douyinvod.com requires a Douyin Referer. The web client opens the URL
    # directly, so those formats cannot be offered as standalone links.

    result = {
        **info,
        "formats": [
            item
            for item in info.get("formats") or []
            if isinstance(item, dict) and not _douyin_requires_referer(item.get("url"))
        ],
    }
    if _douyin_requires_referer(result.get("url")):
        result.pop("url", None)
    return result


def _douyin_has_referer_bound_url(info: dict) -> bool:
    return _douyin_requires_referer(info.get("url")) or any(
        isinstance(item, dict) and _douyin_requires_referer(item.get("url"))
        for item in info.get("formats") or []
    )


def _resolve_douyin_resource_url(raw_url: str, settings: Settings) -> str | None:
    """Resolve and probe a CDN URL without the Referer the browser cannot send."""
    allowed_hosts = {"douyin.com", "douyinvod.com", "ixigua.com", "zjcdn.com", "z1cdn.com"}
    current = raw_url
    session = requests.Session()
    for _ in range(4):
        hostname = (urlparse(current).hostname or "").lower()
        if not _hostname_matches(hostname, allowed_hosts):
            return None
        validate_extracted_media_url(current, trust_extractor=True)
        try:
            with session.get(
                current,
                headers={"Range": "bytes=0-0"},
                proxies=_request_proxies(settings),
                timeout=settings.ytdlp_socket_timeout,
                allow_redirects=False,
                stream=True,
            ) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        return None
                    current = requests.compat.urljoin(current, location)
                    continue
                if response.status_code in (200, 206) and response.headers.get(
                    "Content-Type", ""
                ).lower().startswith("video/"):
                    return current
                return None
        except requests.RequestException:
            return None
    return None


def _has_progressive_mp4(info: dict) -> bool:
    return any(item["has_audio"] for item in direct_video_formats(info)) or bool(
        isinstance(info.get("url"), str)
        and info["url"].startswith(("http://", "https://"))
        and info.get("vcodec", "none") != "none"
        and info.get("acodec", "none") != "none"
        and info.get("ext") == "mp4"
        and not str(info.get("protocol") or "").startswith("m3u8")
    )


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


def _canonical_xiaohongshu_note_url(raw_url: str) -> str | None:
    """把小红书域名下的笔记/登录墙地址还原成规范的笔记链接。

    - 已是 /explore/ 或 /discovery/item/ 形态：统一为 https + www.xiaohongshu.com
      （rednote.com 是小红书按出口 IP 分流的海外域名，yt-dlp 不认，需要改写）；
    - /login 登录墙：解码 redirectPath 参数后再走同一逻辑（最多 3 跳）。
    原始 query 串不做解码/重编码，避免二次破坏 percent-encoding。
    """
    current = raw_url.strip()
    for _ in range(3):
        parsed = urlparse(current)
        host = (parsed.hostname or "").lower()
        path = parsed.path or ""
        if not _hostname_matches(host, XIAOHONGSHU_LOGIN_HOSTS):
            return None
        if XIAOHONGSHU_NOTE_PATH_RE.match(path):
            return urlunparse(("https", "www.xiaohongshu.com", path, "", parsed.query, ""))
        if path.rstrip("/").lower().endswith("/login"):
            current = (parse_qs(parsed.query).get("redirectPath") or [""])[0]
            continue
        return None
    return None


def expand_xiaohongshu_redirect(raw_url: str) -> str | None:
    """从短链重定向链条中还原真实的小红书笔记地址。

    xhslink 短链域名不在 yt-dlp 识别范围，裸 HTTP 请求又会被 302 到
    xiaohongshu/rednote 的 /login 墙；登录页的 redirectPath 带着含
    xsec_token 的完整笔记 URL，解出来等价于用户手工复制长链接。
    若链条落地页没带登录 query（如 App 下载引导页），用同一
    path+query 构造 https/http、裸域/www 候选再解一次。
    """
    candidates: list[str] = [raw_url]
    parsed = urlparse(raw_url.strip())
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        for scheme in ("https", "http"):
            for netloc in (parsed.netloc, f"www.{parsed.netloc}"):
                alternate = urlunparse((scheme, netloc, parsed.path, "", parsed.query, ""))
                if alternate not in candidates:
                    candidates.append(alternate)
    for candidate in candidates:
        expanded = _canonical_xiaohongshu_note_url(candidate)
        if expanded:
            return expanded
    return None


def resolve_xiaohongshu_short_link(raw_url: str, settings: Settings) -> str:
    """跟随 xhslink 短链重定向，返回可供 yt-dlp 提取的小红书笔记链接。"""
    session = requests.Session()
    headers = {
        "User-Agent": DEFAULT_USER_AGENT,
        "Referer": XIAOHONGSHU_HOME_URL,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    try:
        response = session.get(
            raw_url,
            headers=headers,
            proxies=_request_proxies(settings),
            timeout=settings.ytdlp_socket_timeout,
            allow_redirects=True,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        raise MediaParseError("小红书短链接请求失败，请稍后重试") from error
    for candidate in [response.url, *(item.url for item in reversed(response.history))]:
        expanded = expand_xiaohongshu_redirect(str(candidate))
        if expanded:
            # 出口交给代理/池时，本机 DNS 结果不可信，不再强拉解析（同 validate_source_media_url）。
            return validate_extracted_media_url(
                expanded,
                trust_extractor=bool(settings.ytdlp_proxy) or proxy_pool.enabled(settings),
            )
    raise MediaParseError(
        "无法从小红书短链接中提取笔记地址，请在 App 内复制完整链接后重试"
    )


def _is_tiktok_retryable(error: DownloadError) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "unexpected response from webpage request",
            "unable to extract webpage video data",
            "unable to extract universal data",
            "failed to parse json",
            "fresh cookies",
            "unable to generate required cookies",
            "unable to obtain douyin security identifier",
            "failed to download web detail json",
            "failed to download signed web detail json",
            "remote end closed connection",
            "timed out",
            "http error 403",
            "http error 429",
            "requested format is not available",
            "no video formats found",
        )
    )


def _is_xiaohongshu_retryable(error: DownloadError) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "requested format is not available",
            "no video formats found",
            "unable to extract initial state",
            "unable to extract webpage",
            "failed to parse json",
            "remote end closed connection",
            "timed out",
        )
    )


def _is_youtube_retryable(error: DownloadError) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "requested format is not available",
            "no video formats found",
            "unable to download api page",
            "unable to extract",
            "failed to parse json",
            "sign in to confirm you",
            "remote end closed connection",
            "timed out",
            "http error 403",
            "http error 429",
        )
    )


def _extract_info_with_retries(
    url: str,
    options: dict,
    *,
    tiktok: bool = False,
    douyin: bool = False,
    xiaohongshu: bool = False,
    youtube: bool = False,
) -> dict:
    if tiktok or douyin:
        user_agents: tuple[str | None, ...] = (None, *TIKTOK_RETRY_USER_AGENTS)
    elif xiaohongshu:
        user_agents = XIAOHONGSHU_RETRY_USER_AGENTS
    elif youtube:
        user_agents = tuple(None for _ in YOUTUBE_RETRY_CLIENTS)
    else:
        user_agents = (None,)
    last_error: DownloadError | None = None
    for index, user_agent in enumerate(user_agents):
        attempt_options = dict(options)
        if tiktok or douyin or xiaohongshu or youtube:
            attempt_options["logger"] = YTDLP_QUIET_LOGGER
        if youtube and YOUTUBE_RETRY_CLIENTS[index]:
            extractor_args = dict(attempt_options.get("extractor_args") or {})
            youtube_args = dict(extractor_args.get("youtube") or {})
            youtube_args["player_client"] = list(YOUTUBE_RETRY_CLIENTS[index] or ())
            extractor_args["youtube"] = youtube_args
            attempt_options["extractor_args"] = extractor_args
        headers = dict(attempt_options.get("http_headers") or {})
        if user_agent:
            headers["User-Agent"] = user_agent
        if xiaohongshu:
            headers["Referer"] = XIAOHONGSHU_HOME_URL
        if headers:
            attempt_options["http_headers"] = headers
        if index:
            time.sleep(0.35 * index)
        try:
            with YoutubeDL(attempt_options) as ydl:
                if _hostname_matches(
                    (urlparse(url).hostname or "").lower(), DOUYIN_HOSTS
                ):
                    ydl.add_info_extractor(DouyinIE(ydl))
                return ydl.extract_info(url, download=False)
        except DownloadError as error:
            last_error = error
            retryable = (
                ((tiktok or douyin) and _is_tiktok_retryable(error))
                or (xiaohongshu and _is_xiaohongshu_retryable(error))
                or (youtube and _is_youtube_retryable(error))
            )
            if not retryable or index == len(user_agents) - 1:
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


def _wechat_channels_short_uri(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    candidate = ""
    if _hostname_matches((parsed.hostname or "").lower(), {"weixin.qq.com"}):
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[-2].lower() == "sph":
            candidate = parts[-1]
    elif _hostname_matches(
        (parsed.hostname or "").lower(), {"channels.weixin.qq.com"}
    ):
        candidate = parse_qs(parsed.query).get("id", [""])[0]
    match = WECHAT_CHANNELS_SHORT_URI_PATTERN.match(candidate)
    if not match:
        raise MediaParseError("未在微信视频号链接中找到有效的分享 ID")
    return match.group(0)


def _wechat_json(
    session: requests.Session,
    url: str,
    *,
    payload: dict,
    headers: dict[str, str],
    settings: Settings,
    params: dict[str, str] | None = None,
) -> dict:
    try:
        response = session.post(
            url,
            params=params,
            json=payload,
            headers=headers,
            proxies=_request_proxies(settings),
            timeout=settings.ytdlp_socket_timeout,
        )
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as error:
        raise MediaParseError("微信视频号播放信息请求失败") from error
    if not isinstance(result, dict):
        raise MediaParseError("微信视频号返回了无效的播放信息")
    return result


def _wechat_feed_info(
    session: requests.Session,
    *,
    settings: Settings,
    short_uri: str | None = None,
    export_id: str | None = None,
    general_token: str = "",
) -> dict:
    payload: dict = {"baseReq": {"generalToken": general_token}}
    if export_id:
        payload["exportId"] = export_id
        referer = (
            "https://channels.weixin.qq.com/finder-preview/pages/feed"
            f"?token={general_token}&eid={export_id}"
        )
        page_url = "https://channels.weixin.qq.com/finder-preview/pages/feed"
    elif short_uri:
        payload["shortUri"] = short_uri
        referer = (
            "https://channels.weixin.qq.com/finder-preview/pages/sph"
            f"?id={short_uri}"
        )
        page_url = "https://channels.weixin.qq.com/finder-preview/pages/sph"
    else:
        raise MediaParseError("微信视频号请求缺少分享 ID")
    result = _wechat_json(
        session,
        WECHAT_CHANNELS_FEED_API,
        payload=payload,
        params={"_rid": uuid.uuid4().hex, "_pageUrl": page_url},
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://channels.weixin.qq.com",
            "Referer": referer,
            "User-Agent": DEFAULT_USER_AGENT,
        },
        settings=settings,
    )
    if result.get("errCode") not in (None, 0):
        raise MediaParseError(str(result.get("errMsg") or "微信视频号解析失败"))
    data = result.get("data")
    if not isinstance(data, dict):
        raise MediaParseError("微信视频号返回了无效的作品信息")
    detail = data.get("errMsg")
    if isinstance(detail, dict) and detail.get("type") not in (None, 0):
        message = detail.get("content") or detail.get("title")
        raise MediaParseError(str(message or "该微信视频号作品暂不可播放"))
    return data


def _wechat_authorized_feed(
    session: requests.Session,
    raw_url: str,
    settings: Settings,
) -> dict:
    yuanbao_cookie = _yuanbao_cookie(settings)
    if not yuanbao_cookie:
        raise MediaParseError(
            "微信视频号完整提取需要服务端配置腾讯元宝登录凭证"
        )
    parsed = _wechat_json(
        session,
        YUANBAO_WECHAT_PARSE_API,
        payload={"type": "video_channel_url", "url": raw_url, "scene": 1},
        headers={
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Cookie": yuanbao_cookie,
            "Origin": "https://yuanbao.tencent.com",
            "Referer": "https://yuanbao.tencent.com/",
            "User-Agent": DEFAULT_USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
            "X-Source": "web",
        },
        settings=settings,
    )
    parse_data = parsed.get("data")
    if not isinstance(parse_data, dict):
        raise MediaParseError("腾讯元宝登录凭证已失效或无法解析该视频号链接")
    playable_url = str(parse_data.get("playable_url") or "")
    query = parse_qs(urlparse(playable_url).query)
    export_id = (query.get("eid") or [parse_data.get("wx_export_id") or ""])[0]
    general_token = (query.get("token") or [""])[0]
    if not export_id or not general_token:
        raise MediaParseError("腾讯元宝未返回有效的微信视频号临时播放凭证")
    return _wechat_feed_info(
        session,
        settings=settings,
        export_id=str(export_id),
        general_token=str(general_token),
    )


def _yuanbao_cookie(settings: Settings) -> str:
    inline_cookie = settings.yuanbao_cookie.strip()
    if inline_cookie:
        return inline_cookie
    cookie_path = settings.yuanbao_cookie_file.strip()
    if not cookie_path:
        return ""
    try:
        lines = Path(cookie_path).read_text(encoding="utf-8-sig").splitlines()
    except OSError as error:
        raise MediaParseError("无法读取本地腾讯元宝登录凭证文件") from error

    values: dict[str, str] = {}
    allowed_names = ("hy_user", "hy_token", "hy_source")
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("#HttpOnly_"):
            line = line.removeprefix("#HttpOnly_")
        elif not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        domain, name, value = parts[0], parts[5], parts[6]
        if (
            domain == "tencent.com" or domain.endswith(".tencent.com")
        ) and name in allowed_names:
            values[name] = value
    if not {"hy_user", "hy_token"}.issubset(values):
        raise MediaParseError("本地腾讯元宝登录凭证文件已失效或格式不正确")
    return "; ".join(
        f"{name}={values[name]}" for name in allowed_names if name in values
    )


def _wechat_video_candidates(feed: dict) -> list[dict]:
    candidates = []
    for key, codec in (
        ("h264VideoInfo", "h264"),
        ("videoInfo", None),
        ("h265VideoInfo", "h265"),
    ):
        info = feed.get(key)
        if not isinstance(info, dict):
            continue
        media_url = info.get("videoUrl") or info.get("mediaUrl")
        if isinstance(media_url, str) and media_url.startswith(("http://", "https://")):
            candidates.append({**info, "url": media_url, "codec": codec})
    media_url = feed.get("videoUrl")
    if isinstance(media_url, str) and media_url.startswith(("http://", "https://")):
        candidates.append({"url": media_url, "codec": None})
    unique = []
    seen = set()
    for candidate in candidates:
        if candidate["url"] not in seen:
            seen.add(candidate["url"])
            unique.append(candidate)
    return unique


def resolve_wechat_channels(raw_url: str, settings: Settings) -> dict:
    short_uri = _wechat_channels_short_uri(raw_url)
    session = requests.Session()
    data = _wechat_feed_info(session, settings=settings, short_uri=short_uri)
    feed = data.get("feedInfo") if isinstance(data.get("feedInfo"), dict) else {}
    candidates = _wechat_video_candidates(feed)
    if not candidates:
        data = _wechat_authorized_feed(session, raw_url, settings)
        feed = data.get("feedInfo") if isinstance(data.get("feedInfo"), dict) else {}
        candidates = _wechat_video_candidates(feed)
    if not candidates:
        if feed.get("picInfo"):
            raise MediaParseError("该微信视频号链接是图文作品，暂不包含可下载视频")
        raise MediaParseError("该微信视频号作品没有返回可下载的视频流")

    author = data.get("authorInfo") if isinstance(data.get("authorInfo"), dict) else {}
    scene = data.get("sceneInfo") if isinstance(data.get("sceneInfo"), dict) else {}
    title = str(feed.get("description") or "微信视频号视频").strip()
    cover = feed.get("coverUrl")
    raw_duration = feed.get("duration") or feed.get("videoDuration")
    try:
        duration = float(raw_duration) if raw_duration is not None else None
    except (TypeError, ValueError):
        duration = None
    formats = []
    for index, candidate in enumerate(candidates):
        media_url = validate_extracted_media_url(str(candidate["url"]))
        codec = str(candidate.get("codec") or "").upper()
        raw_width = candidate.get("width") or feed.get("width")
        raw_height = candidate.get("height") or feed.get("height")
        try:
            width = int(raw_width) if raw_width is not None else None
        except (TypeError, ValueError):
            width = None
        try:
            height = int(raw_height) if raw_height is not None else None
        except (TypeError, ValueError):
            height = None
        quality = min(width, height) if isinstance(width, (int, float)) and isinstance(height, (int, float)) else None
        label = "原画"
        if codec:
            label += f" · {codec}"
        label += " · 音视频"
        formats.append(
            {
                "id": codec.lower() or f"wechat-{index + 1}",
                "label": label,
                "url": media_url,
                "ext": "mp4",
                "width": width,
                "height": height,
                "quality": int(quality) if quality else None,
                "fps": candidate.get("fps"),
                "filesize": candidate.get("fileSize") or candidate.get("filesize"),
                "vcodec": candidate.get("codec"),
                "acodec": None,
                "has_audio": True,
                "expires_at": None,
            }
        )
    primary = formats[0]
    return {
        "platform": "wechat-channels",
        "id": str(scene.get("dynamicExportId") or short_uri),
        "title": title or "微信视频号视频",
        "video_url": primary["url"],
        "webpage_url": raw_url,
        "thumbnail": cover if isinstance(cover, str) else None,
        "uploader": author.get("nickname"),
        "duration": duration,
        "quality": primary["label"],
        "ext": "mp4",
        "filesize": primary["filesize"],
        "expires_at": None,
        "audio_url": None,
        "audio_formats": [],
        "formats": formats,
    }


def resolve_direct_video(raw_url: str, settings: Settings) -> dict:
    url = validate_source_media_url(raw_url, settings)
    hostname = (urlparse(url).hostname or "").lower()
    if _hostname_matches(hostname, XIAOHONGSHU_SHORT_HOSTS):
        url = resolve_xiaohongshu_short_link(url, settings)
        hostname = (urlparse(url).hostname or "").lower()
    if _hostname_matches(hostname, BILIBILI_HOSTS):
        return resolve_bilibili(url, settings)
    if _hostname_matches(hostname, WECHAT_CHANNELS_HOSTS):
        return resolve_wechat_channels(url, settings)
    is_tiktok = _hostname_matches(hostname, TIKTOK_HOSTS)
    is_douyin = _hostname_matches(hostname, DOUYIN_HOSTS)
    is_xiaohongshu = _hostname_matches(hostname, XIAOHONGSHU_HOSTS)
    is_youtube = _hostname_matches(hostname, YOUTUBE_HOSTS)

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
    if is_douyin:
        options["retries"] = 2
        options["extractor_retries"] = 2
    proxy = _extraction_proxy(settings)
    if proxy:
        options["proxy"] = proxy
    try:
        info = _extract_info_with_retries(
            url,
            options,
            tiktok=is_tiktok,
            douyin=is_douyin,
            xiaohongshu=is_xiaohongshu,
            youtube=is_youtube,
        )
    except DownloadError as error:
        message = str(error).removeprefix("ERROR: ").strip()
        raise MediaParseError(message or "yt-dlp 解析失败") from error
    if not isinstance(info, dict) or info.get("entries") is not None:
        raise MediaParseError("暂不支持播放列表或频道链接")
    images = direct_image_formats(info, trust_extractor=is_tiktok or is_douyin)
    if is_douyin and images:
        # Douyin photo posts can include a short companion video or music track.
        # Its CDN URL is not the content the user wants and may fail the video
        # resource probe, so return every extracted photo directly.
        return {
            "platform": str(info.get("extractor_key") or info.get("extractor") or "douyin").lower(),
            "id": str(info.get("id") or ""),
            "title": str(info.get("title") or "(无标题)"),
            "video_url": None,
            "resource_url": None,
            "webpage_url": info.get("webpage_url") or url,
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader"),
            "duration": None,
            "quality": None,
            "ext": None,
            "filesize": None,
            "expires_at": None,
            "audio_url": None,
            "audio_formats": [],
            "images": images,
            "formats": [],
        }
    if is_douyin:
        browser_info = _douyin_browser_info(info)
        referer_bound = _douyin_has_referer_bound_url(info)
        for _ in range(2):
            if _has_progressive_mp4(browser_info) or not referer_bound:
                break
            try:
                retry_info = _extract_info_with_retries(url, options, douyin=True)
            except DownloadError:
                break
            if not isinstance(retry_info, dict) or retry_info.get("entries") is not None:
                break
            browser_info = _douyin_browser_info(retry_info)
            referer_bound = _douyin_has_referer_bound_url(retry_info)
        info = browser_info
        if referer_bound and not _has_progressive_mp4(info):
            raise MediaParseError("抖音当前只返回需要来源校验的 CDN 地址，请稍后重试")
    formats = direct_video_formats(info)
    audio_formats = direct_audio_formats(info)
    video_url = info.get("url")
    primary_format = None
    if is_tiktok or is_douyin:
        # The web client downloads a single URL. A video-only yt-dlp fallback
        # would otherwise produce a silent file even when a progressive MP4
        # is available among the extracted formats.
        progressive = next((item for item in formats if item["has_audio"]), None)
        if progressive:
            primary_format = progressive
            video_url = progressive["url"]
            formats = [progressive, *[item for item in formats if item is not progressive]]
        elif not (
            isinstance(video_url, str)
            and video_url.startswith(("http://", "https://"))
            and info.get("vcodec", "none") != "none"
            and info.get("acodec", "none") != "none"
            and info.get("ext") == "mp4"
            and not str(info.get("protocol") or "").startswith("m3u8")
        ):
            raise MediaParseError("该视频没有可直接下载的音视频合一 MP4 地址")
        else:
            # Let the web client use videoUrl when the extractor omitted the
            # selected progressive format from its formats list.
            formats = []
    resource_url = None
    if is_douyin:
        candidates = [item["url"] for item in formats if item["has_audio"]]
        if isinstance(video_url, str):
            candidates.append(video_url)
        for candidate in dict.fromkeys(candidates):
            resolved = _resolve_douyin_resource_url(candidate, settings)
            if resolved and (not resource_url or _douyin_requires_referer(resource_url)):
                resource_url = resolved
            if resource_url and not _douyin_requires_referer(resource_url):
                break
        if not resource_url:
            raise MediaParseError("抖音 CDN 未返回可直接访问的视频资源，请稍后重试")
        video_url = resource_url
        formats = []
    if not isinstance(video_url, str) or not video_url:
        raise MediaParseError("该视频没有可直接播放的音视频合一格式")
    if is_tiktok or is_douyin:
        validate_extracted_media_url(video_url, trust_extractor=True)
    else:
        validate_extracted_media_url(video_url)
    return {
        "platform": str(info.get("extractor_key") or info.get("extractor") or "generic").lower(),
        "id": str(info.get("id") or ""),
        "title": str(info.get("title") or "(无标题)"),
        "video_url": video_url,
        "resource_url": resource_url,
        "webpage_url": info.get("webpage_url") or url,
        "thumbnail": info.get("thumbnail"),
        "uploader": info.get("uploader"),
        "duration": info.get("duration"),
        "quality": (
            primary_format["label"]
            if primary_format
            else info.get("format_note") or info.get("resolution") or info.get("format_id")
        ),
        "ext": primary_format["ext"] if primary_format else info.get("ext"),
        "filesize": primary_format["filesize"] if primary_format else _size(info),
        "expires_at": _url_expiry(video_url),
        "audio_url": audio_formats[0]["url"] if audio_formats else None,
        "audio_formats": audio_formats,
        "images": images,
        "formats": formats,
    }


def parse_media(raw_url: str, settings: Settings) -> dict:
    url = validate_source_media_url(raw_url, settings)
    hostname = (urlparse(url).hostname or "").lower()
    if _hostname_matches(hostname, XIAOHONGSHU_SHORT_HOSTS):
        url = resolve_xiaohongshu_short_link(url, settings)
        hostname = (urlparse(url).hostname or "").lower()
    if _hostname_matches(hostname, WECHAT_CHANNELS_HOSTS):
        resolved = resolve_wechat_channels(url, settings)
        formats = [
            {
                "id": FMT_BEST,
                "label": "视频（原画）",
                "height": None,
                "ext": "mp4",
                "filesize": resolved.get("filesize"),
            },
            {
                "id": FMT_AUDIO_MP3,
                "label": "仅音频（MP3 提取）",
                "height": None,
                "ext": "mp3",
                "filesize": None,
            },
        ]
        return {
            "id": resolved["id"],
            "title": resolved["title"],
            "duration": resolved.get("duration"),
            "thumbnail": resolved.get("thumbnail"),
            "uploader": resolved.get("uploader"),
            # The desktop downloader must receive the signed CDN URL instead
            # of handing the unsupported share page back to yt-dlp.
            "webpage_url": resolved["video_url"],
            "formats": formats,
        }
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": settings.ytdlp_socket_timeout,
        "retries": 1,
        "extractor_retries": 1,
    }
    proxy = _extraction_proxy(settings)
    if proxy:
        options["proxy"] = proxy
    try:
        info = _extract_info_with_retries(
            url,
            options,
            tiktok=_hostname_matches(hostname, TIKTOK_HOSTS),
            douyin=_hostname_matches(hostname, DOUYIN_HOSTS),
            xiaohongshu=_hostname_matches(hostname, XIAOHONGSHU_HOSTS),
            youtube=_hostname_matches(hostname, YOUTUBE_HOSTS),
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
        "images": direct_image_formats(
            info,
            trust_extractor=_hostname_matches(hostname, TIKTOK_HOSTS | DOUYIN_HOSTS),
        ),
        "formats": collapse_formats(info),
    }
