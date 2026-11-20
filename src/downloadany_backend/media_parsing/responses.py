"""Convert normalized media into the two existing API contracts."""


from dataclasses import asdict

from .common import FMT_AUDIO_MP3, FMT_BEST
from .result import MediaResult


def single_file_options(filesize=None):
    return [
        {"id": FMT_BEST, "label": "视频（原画）", "height": None, "ext": "mp4", "filesize": filesize},
        {"id": FMT_AUDIO_MP3, "label": "仅音频（MP3 提取）", "height": None, "ext": "mp3", "filesize": None},
    ]


def direct_response(result: MediaResult) -> dict:
    data = asdict(result)
    data.pop("download_options")
    data.pop("download_source")
    return data


def parse_response(result: MediaResult) -> dict:
    return {
        "id": result.id, "title": result.title, "duration": result.duration,
        "thumbnail": result.thumbnail, "uploader": result.uploader,
        "webpage_url": result.download_source or result.webpage_url,
        "images": result.images, "formats": result.download_options,
    }
