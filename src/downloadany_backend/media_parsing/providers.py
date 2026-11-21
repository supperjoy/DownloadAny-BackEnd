"""Provider registry. Platform modules own extraction and platform policy."""


from .common import (
    BILIBILI_HOSTS,
    BILIBILI_SHORT_HOSTS,
    DOUYIN_HOSTS,
    TIKTOK_HOSTS,
    WECHAT_CHANNELS_HOSTS,
    XIAOHONGSHU_HOSTS,
    XIAOHONGSHU_SHORT_HOSTS,
    YOUTUBE_HOSTS,
    _hostname_matches,
)
from . import bilibili, douyin, tiktok, wechat, xiaohongshu, youtube, ytdlp


PROVIDERS = (
    (TIKTOK_HOSTS, tiktok.extract),
    (DOUYIN_HOSTS, douyin.extract),
    (BILIBILI_HOSTS | BILIBILI_SHORT_HOSTS, bilibili.extract),
    (WECHAT_CHANNELS_HOSTS, wechat.extract),
    (XIAOHONGSHU_HOSTS | XIAOHONGSHU_SHORT_HOSTS, xiaohongshu.extract),
    (YOUTUBE_HOSTS, youtube.extract),
)


ONLINE_GENERIC_HOSTS = {"toutiao.com", "finder.video.qq.com", "instagram.com", "instagr.am"}


def is_online_parse_supported(hostname: str) -> bool:
    """官网支持专用解析器及指定的通用解析平台，其余交给桌面端。"""
    hostname = hostname.lower()
    return any(_hostname_matches(hostname, domains) for domains, _ in PROVIDERS) or (
        _hostname_matches(hostname, ONLINE_GENERIC_HOSTS)
    )


def select_provider(hostname):
    for domains, provider in PROVIDERS:
        if _hostname_matches(hostname, domains):
            return provider
    return ytdlp.extract
