"""bilibili platform implementation."""


from urllib.parse import parse_qs, urlparse
import threading

import requests

from .common import (
    BILIBILI_BVID_PATTERN,
    BILIBILI_HOME_URL,
    BILIBILI_HOSTS,
    BILIBILI_PLAY_API,
    BILIBILI_SHORT_HOSTS,
    BILIBILI_VIEW_API,
    DEFAULT_USER_AGENT,
    MediaParseError,
    _request_proxies,
    _url_expiry,
    _hostname_matches,
    validate_public_media_url,
    validate_extracted_media_url,
)
from ..config import Settings
from .result import MediaResult
from . import ytdlp


_bilibili_session_state = threading.local()


def extract(url, settings, *, purpose):
    if _hostname_matches(
        (urlparse(url).hostname or "").lower(), BILIBILI_SHORT_HOSTS
    ):
        url = resolve_bilibili_short_link(url, settings)
    # /parse exposes yt-dlp's quality and merge options; /resolve needs one file.
    if purpose == "parse":
        return ytdlp.extract(url, settings, purpose=purpose)
    return MediaResult.from_direct(resolve_bilibili(url, settings))


def resolve_bilibili_short_link(raw_url: str, settings: Settings) -> str:
    """Expand b23.tv without downloading the final Bilibili video webpage."""

    session = _bilibili_session()
    current = raw_url
    allowed_hosts = BILIBILI_HOSTS | BILIBILI_SHORT_HOSTS
    for _ in range(5):
        parsed = urlparse(current)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not _hostname_matches(
            hostname, allowed_hosts
        ):
            raise MediaParseError("Bilibili 短链接跳转到了非 B站地址")
        validate_public_media_url(
            current, resolve_hostname=not bool(settings.ytdlp_proxy)
        )
        if BILIBILI_BVID_PATTERN.search(current):
            return current
        try:
            response = session.get(
                current,
                headers={"User-Agent": DEFAULT_USER_AGENT},
                proxies=_request_proxies(settings),
                timeout=settings.ytdlp_socket_timeout,
                allow_redirects=False,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            raise MediaParseError("Bilibili 短链接展开失败，请稍后重试") from error
        if response.status_code not in (301, 302, 303, 307, 308):
            break
        location = response.headers.get("Location")
        if not location:
            break
        current = requests.compat.urljoin(current, location)
    raise MediaParseError("无法从 Bilibili 短链接中找到 BV 号")


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

