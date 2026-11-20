"""wechat platform implementation."""


from pathlib import Path
from urllib.parse import parse_qs, urlparse
import uuid

import requests

from .common import (
    DEFAULT_USER_AGENT,
    MediaParseError,
    WECHAT_CHANNELS_FEED_API,
    WECHAT_CHANNELS_SHORT_URI_PATTERN,
    YUANBAO_WECHAT_PARSE_API,
    _hostname_matches,
    _request_proxies,
    validate_extracted_media_url,
)
from ..config import Settings
from .responses import single_file_options
from .result import MediaResult


def extract(url, settings, *, purpose):
    result = MediaResult.from_direct(resolve_wechat_channels(url, settings))
    result.download_source = result.video_url
    result.download_options = single_file_options(result.filesize)
    return result


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
