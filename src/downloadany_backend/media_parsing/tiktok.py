"""TikTok's dedicated provider does not depend on yt-dlp."""


import requests

from .common import MediaParseError, _url_expiry, validate_extracted_media_url
from .responses import single_file_options
from .result import MediaResult
from ..tiktok_tikvideo import extract_tiktok


def extract(url, settings, *, purpose):
    try:
        data = extract_tiktok(url, timeout=settings.ytdlp_socket_timeout, proxy=settings.ytdlp_proxy or None)
    except (requests.RequestException, ValueError) as error:
        raise MediaParseError(f"TikTok 解析失败：{error}") from error
    result = MediaResult.from_direct(data)
    validate_extracted_media_url(result.video_url, trust_extractor=True)
    result.expires_at = _url_expiry(result.video_url)
    result.download_source = result.video_url
    result.download_options = single_file_options(result.filesize)
    return result
