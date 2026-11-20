"""Opt-in request-scoped proxy selection for providers that need pool egress."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from .. import proxy_pool
from ..config import Settings


@dataclass
class ProxyRoute:
    settings: Settings
    url: str = ""
    node: str | None = None

    @classmethod
    def select(cls, settings: Settings) -> "ProxyRoute":
        route = cls(settings=settings, url=settings.ytdlp_proxy)
        if proxy_pool.enabled(settings):
            route.node = proxy_pool.switch_to_random_active(settings)
            if route.node:
                route.url = settings.mihomo_proxy_url
        return route

    def rotate(self) -> str:
        if not proxy_pool.enabled(self.settings):
            return self.url
        node = proxy_pool.switch_to_random_active(self.settings, avoid=self.node)
        if node:
            self.node = node
            self.url = self.settings.mihomo_proxy_url
        else:
            self.url = self.settings.ytdlp_proxy
        return self.url


_current_route: ContextVar[ProxyRoute | None] = ContextVar(
    "media_proxy_route", default=None
)


@contextmanager
def proxy_scope(settings: Settings):
    token = _current_route.set(ProxyRoute.select(settings))
    try:
        yield
    finally:
        _current_route.reset(token)


def current_proxy_url(settings: Settings) -> str:
    route = _current_route.get()
    return route.url if route and route.settings is settings else settings.ytdlp_proxy


def rotate_current_proxy(settings: Settings) -> str:
    route = _current_route.get()
    return route.rotate() if route and route.settings is settings else current_proxy_url(settings)
