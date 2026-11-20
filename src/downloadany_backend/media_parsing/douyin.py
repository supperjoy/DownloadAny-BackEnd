"""douyin platform implementation."""


from urllib.parse import urlparse

from yt_dlp.utils import DownloadError
import requests

from .common import (
    DOUYIN_RETRY_USER_AGENTS,
    MediaParseError,
    _hostname_matches,
    _request_proxies,
    _url_expiry,
    direct_image_formats,
    direct_video_formats,
    validate_extracted_media_url,
)
from ..config import Settings
from ..douyin_community import DouyinIE
from .yt_adapter import normalize
from .network import proxy_scope
from . import ytdlp


def _validate_info(info):
    if isinstance(info, dict) and not info.get("url") and not direct_image_formats(info, trust_extractor=True):
        raise DownloadError("Requested format is not available")


def policy():
    return ytdlp.RetryPolicy(
        attempts=tuple(
            {"ignore_no_formats_error": True, **({"http_headers": {"User-Agent": agent}} if agent else {})}
            for agent in (None, *DOUYIN_RETRY_USER_AGENTS)
        ),
        retryable=_is_douyin_retryable,
        extractor_factory=lambda ydl: DouyinIE(ydl),
        validate_info=_validate_info,
    )


def _load(url, settings, purpose):
    return ytdlp.load(
        url, settings, purpose=purpose, policy=policy(),
        extra_options={"retries": 2, "extractor_retries": 2} if purpose == "resolve" else None,
    )


def _extract(url, settings, *, purpose):
    info = _load(url, settings, purpose)
    if purpose == "parse":
        return normalize(info, url, direct=False, trust=True)
    if direct_image_formats(info, trust_extractor=True):
        result = normalize(info, url, direct=False, trust=True)
        result.platform = "douyin"
        result.duration = None
        return result
    browser_info = _douyin_browser_info(info)
    referer_bound = _douyin_has_referer_bound_url(info)
    for _ in range(2):
        if _has_progressive_mp4(browser_info) or not referer_bound:
            break
        try:
            info = _load(url, settings, purpose)
        except MediaParseError:
            break
        browser_info = _douyin_browser_info(info)
        referer_bound = _douyin_has_referer_bound_url(info)
    if referer_bound and not _has_progressive_mp4(browser_info):
        raise MediaParseError("抖音当前只返回需要来源校验的 CDN 地址，请稍后重试")
    result = normalize(browser_info, url, direct=True, trust=True, progressive=True)
    candidates = [item["url"] for item in result.formats if item["has_audio"]]
    if result.video_url:
        candidates.append(result.video_url)
    for candidate in dict.fromkeys(candidates):
        resolved = _resolve_douyin_resource_url(candidate, settings)
        if resolved and (not result.resource_url or _douyin_requires_referer(result.resource_url)):
            result.resource_url = resolved
        if result.resource_url and not _douyin_requires_referer(result.resource_url):
            break
    if not result.resource_url:
        raise MediaParseError("抖音 CDN 未返回可直接访问的视频资源，请稍后重试")
    result.video_url = result.resource_url
    result.expires_at = _url_expiry(result.video_url)
    result.formats = []
    return result


def extract(url, settings, *, purpose):
    with proxy_scope(settings):
        return _extract(url, settings, purpose=purpose)


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


def _is_douyin_retryable(error: Exception) -> bool:
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
