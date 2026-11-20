"""xiaohongshu platform implementation."""


from urllib.parse import parse_qs, urlparse, urlunparse

import requests

from .. import proxy_pool
from .common import (
    DEFAULT_USER_AGENT,
    MediaParseError,
    XIAOHONGSHU_HOME_URL,
    XIAOHONGSHU_LOGIN_HOSTS,
    XIAOHONGSHU_NOTE_PATH_RE,
    XIAOHONGSHU_RETRY_USER_AGENTS,
    XIAOHONGSHU_SHORT_HOSTS,
    _hostname_matches,
    _request_proxies,
    validate_extracted_media_url,
)
from ..config import Settings
from .yt_adapter import normalize
from . import ytdlp


def policy():
    return ytdlp.RetryPolicy(
        attempts=tuple({"http_headers": {"User-Agent": agent, "Referer": XIAOHONGSHU_HOME_URL}}
                       for agent in XIAOHONGSHU_RETRY_USER_AGENTS),
        retryable=_is_xiaohongshu_retryable,
    )


def extract(url, settings, *, purpose):
    if _hostname_matches((urlparse(url).hostname or "").lower(), XIAOHONGSHU_SHORT_HOSTS):
        url = resolve_xiaohongshu_short_link(url, settings)
    info = ytdlp.load(url, settings, purpose=purpose, policy=policy())
    return normalize(info, url, direct=purpose == "resolve")


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
            return validate_extracted_media_url(
                expanded,
                trust_extractor=bool(settings.ytdlp_proxy) or proxy_pool.enabled(settings),
            )
    raise MediaParseError(
        "无法从小红书短链接中提取笔记地址，请在 App 内复制完整链接后重试"
    )


def _is_xiaohongshu_retryable(error: Exception) -> bool:
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
