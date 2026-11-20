"""Provider registry. Platform modules own extraction and platform policy."""


from .common import (
    BILIBILI_HOSTS,
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
    (BILIBILI_HOSTS, bilibili.extract),
    (WECHAT_CHANNELS_HOSTS, wechat.extract),
    (XIAOHONGSHU_HOSTS | XIAOHONGSHU_SHORT_HOSTS, xiaohongshu.extract),
    (YOUTUBE_HOSTS, youtube.extract),
)


def select_provider(hostname):
    for domains, provider in PROVIDERS:
        if _hostname_matches(hostname, domains):
            return provider
    return ytdlp.extract
