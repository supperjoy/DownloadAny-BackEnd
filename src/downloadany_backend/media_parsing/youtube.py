"""YouTube client retry policy."""


from .common import YOUTUBE_RETRY_CLIENTS
from .yt_adapter import normalize
from .network import proxy_scope
from . import ytdlp


def _is_youtube_retryable(error: Exception) -> bool:
    message = str(error).lower()
    return any(
        marker in message
        for marker in (
            "requested format is not available",
            "no video formats found",
            "unable to download api page",
            "unexpected_eof_while_reading",
            "unable to extract",
            "failed to parse json",
            "sign in to confirm you",
            "remote end closed connection",
            "timed out",
            "http error 403",
            "http error 429",
        )
    )


def policy():
    return ytdlp.RetryPolicy(
        attempts=tuple(
            {"extractor_args": {"youtube": {"player_client": list(clients)}}} if clients else {}
            for clients in YOUTUBE_RETRY_CLIENTS
        ),
        retryable=_is_youtube_retryable,
    )


def extract(url, settings, *, purpose):
    with proxy_scope(settings):
        info = ytdlp.load(url, settings, purpose=purpose, policy=policy())
        return normalize(info, url, direct=purpose == "resolve")
