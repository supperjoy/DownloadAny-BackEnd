"""Single URL validation and provider dispatch path for both interfaces."""


from urllib.parse import urlparse

from .common import validate_source_media_url
from ..config import Settings
from .providers import select_provider
from .responses import direct_response, parse_response


def extract_media(raw_url: str, settings: Settings, *, purpose: str):
    url = validate_source_media_url(raw_url, settings)
    provider = select_provider((urlparse(url).hostname or "").lower())
    return provider(url, settings, purpose=purpose)


def resolve_direct_video(raw_url: str, settings: Settings) -> dict:
    return direct_response(extract_media(raw_url, settings, purpose="resolve"))


def parse_media(raw_url: str, settings: Settings) -> dict:
    return parse_response(extract_media(raw_url, settings, purpose="parse"))
