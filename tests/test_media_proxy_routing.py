from downloadany_backend.config import Settings
from downloadany_backend.media_parsing import bilibili, tiktok, wechat, xiaohongshu, ytdlp
from downloadany_backend.media_parsing.network import proxy_scope


def pool_settings():
    return Settings(
        jwt_secret="test-secret-that-is-at-least-32-characters-long",
        ytdlp_proxy="http://127.0.0.1:7891",
        mihomo_controller_url="http://127.0.0.1:9090",
        mihomo_proxy_url="http://127.0.0.1:7890",
    )


def test_tiktok_uses_selected_pool_proxy(monkeypatch):
    settings = pool_settings()
    seen = {}
    monkeypatch.setattr(
        "downloadany_backend.proxy_pool.switch_to_random_active",
        lambda settings, avoid=None: "节点 A",
    )
    monkeypatch.setattr(
        tiktok,
        "extract_tiktok",
        lambda url, **kwargs: seen.update(kwargs) or {
            "platform": "tiktok",
            "id": "1",
            "title": "clip",
            "webpage_url": url,
            "video_url": "https://v16.tokcdn.com/clip.mp4",
        },
    )

    tiktok.extract("https://www.tiktok.com/@u/video/1", settings, purpose="resolve")

    assert seen["proxy"] == settings.mihomo_proxy_url


def test_ytdlp_retries_rotate_the_scoped_pool_node(monkeypatch):
    settings = pool_settings()
    switched = []
    monkeypatch.setattr(
        "downloadany_backend.proxy_pool.switch_to_random_active",
        lambda settings, avoid=None: switched.append(avoid) or f"节点 {len(switched)}",
    )

    with proxy_scope(settings):
        first, refresh = ytdlp._build_proxy_setup(settings)
        second = refresh()

    assert first == settings.mihomo_proxy_url
    assert second == settings.mihomo_proxy_url
    assert switched == [None, "节点 1"]


def test_unselected_platforms_do_not_touch_pool(monkeypatch):
    settings = pool_settings()

    def forbidden(*args, **kwargs):
        raise AssertionError("该平台不应启用 mihomo 代理池")

    monkeypatch.setattr(
        "downloadany_backend.proxy_pool.switch_to_random_active", forbidden
    )
    monkeypatch.setattr(
        bilibili,
        "resolve_bilibili",
        lambda url, settings: {
            "platform": "bilibili",
            "id": "BV1",
            "title": "clip",
            "webpage_url": url,
            "video_url": "https://cdn.example/b.mp4",
        },
    )
    monkeypatch.setattr(
        wechat,
        "resolve_wechat_channels",
        lambda url, settings: {
            "platform": "wechat",
            "id": "wx1",
            "title": "clip",
            "webpage_url": url,
            "video_url": "https://cdn.example/w.mp4",
        },
    )
    monkeypatch.setattr(
        ytdlp,
        "load",
        lambda url, settings, **kwargs: {
            "id": "xhs1",
            "title": "clip",
            "webpage_url": url,
            "url": "https://cdn.example/x.mp4",
            "ext": "mp4",
            "vcodec": "h264",
            "acodec": "aac",
        },
    )

    assert bilibili.extract(
        "https://www.bilibili.com/video/BV1", settings, purpose="resolve"
    ).id == "BV1"
    assert wechat.extract(
        "https://weixin.qq.com/sph/abc", settings, purpose="resolve"
    ).id == "wx1"
    assert xiaohongshu.extract(
        "https://www.xiaohongshu.com/explore/abc12345",
        settings,
        purpose="parse",
    ).id == "xhs1"


def test_generic_ytdlp_uses_fixed_proxy_without_selecting_pool(monkeypatch):
    settings = pool_settings()
    monkeypatch.setattr(
        "downloadany_backend.proxy_pool.switch_to_random_active",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("通用平台不应启用 mihomo 代理池")
        ),
    )
    assert ytdlp._build_proxy_setup(settings)[0] == settings.ytdlp_proxy
