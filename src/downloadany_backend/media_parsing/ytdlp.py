"""Generic yt-dlp execution; providers supply their retry policies."""


from collections.abc import Callable
from dataclasses import dataclass, field
import copy
import time

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .common import DIRECT_VIDEO_FORMAT, MediaParseError, YTDLP_QUIET_LOGGER
from .network import current_proxy_url, rotate_current_proxy
from .yt_adapter import normalize


@dataclass(frozen=True)
class RetryPolicy:
    attempts: tuple[dict, ...] = field(default_factory=lambda: ({},))
    retryable: Callable[[Exception], bool] = lambda _: False
    extractor_factory: Callable | None = None
    validate_info: Callable | None = None


def _build_proxy_setup(settings) -> tuple[str, Callable[[], str] | None]:
    return current_proxy_url(settings), lambda: rotate_current_proxy(settings)


def _extract_info_with_retries(
    url: str,
    options: dict,
    *,
    policy: RetryPolicy | None = None,
    proxy_refresher: Callable[[], str] | None = None,
) -> dict:
    policy = policy or RetryPolicy()
    for index, overrides in enumerate(policy.attempts):
        attempt_options = copy.deepcopy(options)
        if index and proxy_refresher is not None:
            refreshed = proxy_refresher()
            if refreshed:
                attempt_options["proxy"] = refreshed
            else:
                attempt_options.pop("proxy", None)
        attempt_options["logger"] = YTDLP_QUIET_LOGGER
        for key, value in overrides.items():
            if isinstance(value, dict):
                attempt_options[key] = {**attempt_options.get(key, {}), **copy.deepcopy(value)}
            else:
                attempt_options[key] = value
        if index:
            time.sleep(0.35 * index)
        try:
            with YoutubeDL(attempt_options) as ydl:
                if policy.extractor_factory:
                    ydl.add_info_extractor(policy.extractor_factory(ydl))
                info = ydl.extract_info(url, download=False)
                if policy.validate_info:
                    policy.validate_info(info)
                return info
        except DownloadError as error:
            if not policy.retryable(error) or index == len(policy.attempts) - 1:
                raise
    raise ValueError("A retry policy must contain at least one attempt")


def options(settings, *, direct):
    result = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "skip_download": True, "socket_timeout": settings.ytdlp_socket_timeout,
        "retries": 1, "extractor_retries": 1,
    }
    if direct:
        result["format"] = DIRECT_VIDEO_FORMAT
    if settings.ytdlp_proxy:
        result["proxy"] = settings.ytdlp_proxy
    return result


def load(url, settings, *, purpose, policy=None, extra_options=None):
    opts = options(settings, direct=purpose == "resolve")
    opts.update(extra_options or {})
    proxy, proxy_refresher = _build_proxy_setup(settings)
    if proxy:
        opts["proxy"] = proxy
    else:
        opts.pop("proxy", None)
    try:
        info = _extract_info_with_retries(
            url, opts, policy=policy, proxy_refresher=proxy_refresher
        )
    except DownloadError as error:
        raise MediaParseError(str(error).removeprefix("ERROR: ").strip() or "yt-dlp 解析失败") from error
    if not isinstance(info, dict) or info.get("entries") is not None:
        raise MediaParseError("暂不支持播放列表或频道链接")
    return info


def extract(url, settings, *, purpose):
    return normalize(load(url, settings, purpose=purpose), url, direct=purpose == "resolve")
