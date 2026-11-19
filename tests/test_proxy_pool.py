import urllib.parse

import pytest
import requests

from downloadany_backend import proxy_pool
from downloadany_backend.config import Settings


def pool_settings(**overrides):
    base = {
        "jwt_secret": "test-secret-that-is-at-least-32-characters-long",
        "mihomo_controller_url": "http://127.0.0.1:9090",
    }
    base.update(overrides)
    return Settings(**base)


# 服务器实测的 mihomo /proxies 结构样本：Selector 组里混着内置策略、
# URLTest 子组、订阅信息伪节点和真实 Trojan 叶子节点。
GROUP_STATE = {
    "proxies": {
        "节点选择": {
            "type": "Selector",
            "now": "自动选择",
            "all": [
                "DIRECT",
                "REJECT",
                "自动选择",
                "剩余流量：189.38 GB",
                "套餐到期：长期有效",
                "🇭🇰|香港-IEPL 03",
                "🇯🇵|日本-中转 02",
                "🇺🇸|美国-家宽 02",
                "🇩🇪|德国 01",
            ],
        },
        "自动选择": {
            "type": "URLTest",
            "all": ["🇭🇰|香港-IEPL 03", "🇯🇵|日本-中转 02"],
        },
        "🇭🇰|香港-IEPL 03": {"type": "Trojan"},
        "🇯🇵|日本-中转 02": {"type": "Trojan"},
        "🇺🇸|美国-家宽 02": {"type": "Trojan"},
        "🇩🇪|德国 01": {"type": "Trojan"},
    }
}
ALIVE_NODES = {"🇭🇰|香港-IEPL 03", "🇯🇵|日本-中转 02"}


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload or {}
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeRequests:
    """替掉 proxy_pool 模块内的 requests：delay 探测按 dead 名单应答。"""

    RequestException = requests.RequestException
    HTTPError = requests.HTTPError

    def __init__(self, dead=(), unreachable=False):
        self.dead = set(dead)
        self.unreachable = unreachable
        self.puts = []

    def get(self, url, **kwargs):
        if self.unreachable:
            raise requests.ConnectionError("controller down")
        if url.endswith("/proxies"):
            return FakeResponse(GROUP_STATE)
        if url.endswith("/delay"):
            name = urllib.parse.unquote(url.split("/proxies/")[1].rsplit("/delay", 1)[0])
            if name in self.dead:
                return FakeResponse({"error": "timeout"}, status=503)
            return FakeResponse({"delay": 88})
        return FakeResponse(status=404)

    def put(self, url, json=None, **kwargs):
        self.puts.append(json["name"])
        return FakeResponse()


def test_switch_picks_randomly_from_alive_leaf_nodes_only(monkeypatch):
    fake = FakeRequests(dead={"🇺🇸|美国-家宽 02", "🇩🇪|德国 01"})
    monkeypatch.setattr(proxy_pool, "requests", fake)

    chosen = proxy_pool.switch_to_random_active(pool_settings())

    assert chosen in ALIVE_NODES
    assert fake.puts == [chosen]


def test_switch_returns_none_and_skips_put_when_no_node_alive(monkeypatch):
    fake = FakeRequests(dead={"🇭🇰|香港-IEPL 03", "🇯🇵|日本-中转 02", "🇺🇸|美国-家宽 02", "🇩🇪|德国 01"})
    monkeypatch.setattr(proxy_pool, "requests", fake)

    assert proxy_pool.switch_to_random_active(pool_settings()) is None
    assert fake.puts == []


def test_switch_returns_none_when_controller_unreachable(monkeypatch):
    fake = FakeRequests(unreachable=True)
    monkeypatch.setattr(proxy_pool, "requests", fake)

    assert proxy_pool.switch_to_random_active(pool_settings()) is None
    assert fake.puts == []


def test_switch_refuses_non_selector_group(monkeypatch):
    fake = FakeRequests()
    monkeypatch.setattr(proxy_pool, "requests", fake)

    assert proxy_pool.switch_to_random_active(pool_settings(mihomo_group="自动选择")) is None
    assert fake.puts == []


def test_switch_disabled_without_controller_url(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("controller must not be contacted when pool is disabled")

    monkeypatch.setattr(proxy_pool, "requests", forbidden)
    settings = Settings(
        jwt_secret="test-secret-that-is-at-least-32-characters-long",
        ytdlp_proxy="http://127.0.0.1:7891",
    )

    assert proxy_pool.enabled(settings) is False
    assert proxy_pool.switch_to_random_active(settings) is None


def test_switch_can_avoid_previous_node(monkeypatch):
    fake = FakeRequests()
    monkeypatch.setattr(proxy_pool, "requests", fake)

    first = proxy_pool.switch_to_random_active(pool_settings())
    second = proxy_pool.switch_to_random_active(pool_settings(), avoid=first)

    assert first is not None
    assert second is not None and second != first


def test_switch_falls_back_to_alive_when_avoid_exhausted(monkeypatch):
    # 只有 HK/JP 活，avoid 掉选中那个之后不应放弃切换。
    fake = FakeRequests(dead={"🇺🇸|美国-家宽 02", "🇩🇪|德国 01"})
    monkeypatch.setattr(proxy_pool, "requests", fake)

    first = proxy_pool.switch_to_random_active(pool_settings())
    second = proxy_pool.switch_to_random_active(pool_settings(), avoid=first)

    assert second in ALIVE_NODES


def test_switch_sends_secret_header_when_configured(monkeypatch):
    seen_headers = []

    class HeaderSpy(FakeRequests):
        def put(self, url, json=None, **kwargs):
            seen_headers.append(kwargs["headers"])
            return super().put(url, json=json, **kwargs)

    monkeypatch.setattr(proxy_pool, "requests", HeaderSpy())

    proxy_pool.switch_to_random_active(pool_settings(mihomo_secret="top-secret"))

    assert seen_headers[0]["Authorization"] == "Bearer top-secret"
