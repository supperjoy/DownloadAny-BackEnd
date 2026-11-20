"""Compatibility exports for media callers; orchestration lives in media_parsing."""

from .media_parsing.common import (
    BILIBILI_BVID_PATTERN, BILIBILI_HOME_URL, BILIBILI_HOSTS, BILIBILI_PLAY_API,
    BILIBILI_VIEW_API, DEFAULT_USER_AGENT, DIRECT_VIDEO_FORMAT, DOUYIN_HOSTS,
    DOUYIN_RETRY_USER_AGENTS, FMT_AUDIO_MP3, FMT_BEST, FMT_THUMBNAIL,
    MediaParseError, SOURCE_URL_RE, SOURCE_URL_TRAILING, TIKTOK_HOSTS,
    TRUSTED_MEDIA_CDN_DOMAINS, WECHAT_CHANNELS_FEED_API, WECHAT_CHANNELS_HOSTS,
    WECHAT_CHANNELS_SHORT_URI_PATTERN, XIAOHONGSHU_HOME_URL, XIAOHONGSHU_HOSTS,
    XIAOHONGSHU_LOGIN_HOSTS, XIAOHONGSHU_NOTE_PATH_RE,
    XIAOHONGSHU_RETRY_USER_AGENTS, XIAOHONGSHU_SHORT_HOSTS, YOUTUBE_HOSTS,
    YOUTUBE_RETRY_CLIENTS, YTDLP_QUIET_LOGGER, YUANBAO_WECHAT_PARSE_API,
    _QuietYoutubeDLLogger, _codec_priority, _display_height, _hostname_matches,
    _request_proxies, _safe_subtitle_language, _size, _subtitle_languages,
    _url_expiry, collapse_formats, direct_audio_formats, direct_image_formats,
    direct_video_formats, extract_source_url, validate_extracted_media_url,
    validate_public_media_url, validate_source_media_url,
)
from .media_parsing.bilibili import _bilibili_json, _bilibili_session, resolve_bilibili
from .media_parsing.douyin import (
    _douyin_browser_info, _douyin_has_referer_bound_url, _douyin_requires_referer,
    _has_progressive_mp4, _resolve_douyin_resource_url,
)
from .media_parsing.service import parse_media, resolve_direct_video
from .media_parsing.wechat import (
    _wechat_authorized_feed, _wechat_channels_short_uri, _wechat_feed_info,
    _wechat_json, _wechat_video_candidates, _yuanbao_cookie, resolve_wechat_channels,
)
from .media_parsing.xiaohongshu import (
    _canonical_xiaohongshu_note_url, expand_xiaohongshu_redirect,
    resolve_xiaohongshu_short_link,
)
from .media_parsing.ytdlp import _extract_info_with_retries
from .media_parsing.youtube import _is_youtube_retryable

TIKTOK_RETRY_USER_AGENTS = DOUYIN_RETRY_USER_AGENTS
