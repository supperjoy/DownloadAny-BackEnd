import pytest
from conftest import ADMIN_HEADERS

pytestmark = pytest.mark.asyncio


async def test_legacy_overview_requires_admin(client, admin_token):
    assert (await client.get("/api/analytics/overview")).status_code == 401
    ok = await client.get("/api/analytics/overview", headers=ADMIN_HEADERS)
    assert ok.status_code == 200
    # Review Focus 5：加保护后旧响应键集不变
    assert set(ok.json()) == {
        "days",
        "active_devices",
        "parse_requests",
        "downloads_completed",
        "download_success_rate",
        "daily",
        "recent_events",
    }


async def test_admin_fail_closed_when_token_unset(client, monkeypatch):
    # 显式置空，防仓库根 .env 把前提弄脏（pydantic 会读 env_file=".env"）
    from downloadany_backend.config import get_settings

    monkeypatch.setattr(get_settings(), "update_admin_token", "")
    # Review Focus 2：服务端未配置令牌时，无头与带头都必须 401
    assert (await client.get("/api/analytics/overview")).status_code == 401
    assert (await client.get("/api/analytics/overview", headers=ADMIN_HEADERS)).status_code == 401


# ---- 共用种子辅助：镜像生产 record_event 的 media_site 主机名派生 ----
def seed_event(db, event_type, *, created_at, device_id=None, user_id=None, **fields):
    from uuid import uuid4
    from urllib.parse import urlparse

    from downloadany_backend.models import AnalyticsEvent

    media_url = fields.get("media_url")
    row = AnalyticsEvent(
        event_id=str(uuid4()),
        event_type=event_type,
        device_id=device_id or str(uuid4()),
        client_session_id=str(uuid4()),
        user_id=user_id,
        app_version=fields.get("app_version", "0.1.0"),
        platform=fields.get("platform", "Win32"),
        media_site=urlparse(media_url).hostname.lower()[:100] if media_url else None,
        media_id=fields.get("media_id"),
        media_title=fields.get("media_title"),
        media_url=media_url,
        format_id=fields.get("format_id"),
        format_label=fields.get("format_label"),
        error=fields.get("error"),
        ip_address=fields.get("ip_address", "12.34.56.78"),
        created_at=created_at,
    )
    db.add(row)
    db.commit()
    return row


async def test_admin_endpoints_reject_no_token(client):
    # 继承性防呆：admin 路由若漏保护立刻红
    assert (await client.get("/api/analytics/admin/overview")).status_code == 401


async def test_admin_overview_empty_window(client, admin_token):
    # Review Focus 1：空库 → 全 0，daily 仍返回 days 个点，不 500
    r = await client.get("/api/analytics/admin/overview?days=30", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["total_users"] == 0 and body["dau_devices"] == 0
    assert body["downloads_completed"]["value"] == 0
    assert body["new_users"]["change"] is None  # 前后窗口皆空 → 无环比
    ts = (await client.get("/api/analytics/admin/timeseries?days=7", headers=ADMIN_HEADERS)).json()
    assert len(ts["daily"]) == 7 and all(p["active_devices"] == 0 for p in ts["daily"])


async def test_admin_overview_counts_and_windows(client, admin_token, db):
    # Review Focus 3：恰在窗口起点的事件计入，早 1 秒不计入
    from datetime import UTC, datetime, time, timedelta

    today = datetime.now(UTC).date()
    start = datetime.combine(today - timedelta(days=6), time.min)  # 固定 7 天窗 / days=7 窗共同起点
    seed_event(db, "download_completed", created_at=start)
    seed_event(db, "download_completed", created_at=start - timedelta(seconds=1))
    for _ in range(2):
        seed_event(db, "download_started", created_at=start)
    body = (await client.get("/api/analytics/admin/overview?days=7", headers=ADMIN_HEADERS)).json()
    assert body["downloads_completed"]["value"] == 1
    assert body["downloads_started"]["value"] == 2
    assert body["download_success_rate"]["value"] == 50.0
    assert body["wau_devices"] == 3  # 窗口外那条的 device 不计入
    # R1 裁决：dau/wau/mau 为固定 1/7/30 天窗口，不随 days 参数变；种子均在 6 天前
    assert body["dau_devices"] == 0
    assert body["mau_devices"] == 4


async def test_admin_timeseries_buckets_by_utc_day(client, admin_token, db):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    seed_event(db, "app_open", created_at=now.replace(hour=0, minute=0, second=0, microsecond=0))
    seed_event(db, "download_completed", created_at=now - timedelta(days=1, hours=1))
    daily = (await client.get("/api/analytics/admin/timeseries?days=3", headers=ADMIN_HEADERS)).json()["daily"]
    assert daily[-1]["app_opens"] == 1 and daily[-1]["active_devices"] == 1
    assert daily[-2]["downloads_completed"] == 1
    assert daily[-3]["active_devices"] == 0


def seed_user(db, *, created_at, with_password=False, with_google=False):
    from uuid import uuid4

    from downloadany_backend.models import User

    row = User(
        email=f"u-{uuid4()}@example.com",
        password_hash="x" * 40 if with_password else None,
        google_subject=str(uuid4()) if with_google else None,
        email_verified=True,
        created_at=created_at,
    )
    db.add(row)
    db.commit()
    return row


async def test_admin_users_channels_and_daily(client, admin_token, db):
    from datetime import UTC, datetime, time, timedelta

    today = datetime.now(UTC).date()
    start = datetime.combine(today - timedelta(days=6), time.min)
    seed_user(db, created_at=start - timedelta(seconds=1), with_password=True)  # 窗口外
    seed_user(db, created_at=start, with_password=True)
    seed_user(db, created_at=start + timedelta(hours=2), with_google=True)
    seed_user(db, created_at=start + timedelta(hours=3), with_password=True, with_google=True)

    r = await client.get("/api/analytics/admin/users?days=7", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["total_users"] == 4
    assert body["new_users"]["value"] == 3
    # 渠道是全表聚合：窗口外那个纯密码用户也要计入
    assert {c["kind"]: c["count"] for c in body["channels"]} == {
        "password": 2,
        "google": 1,
        "both": 1,
    }
    assert len(body["new_daily"]) == 7
    assert sum(p["count"] for p in body["new_daily"]) == 3
    assert (await client.get("/api/analytics/admin/users")).status_code == 401


async def test_admin_users_empty_table(client, admin_token):
    body = (await client.get("/api/analytics/admin/users?days=30", headers=ADMIN_HEADERS)).json()
    assert body["total_users"] == 0 and body["channels"] == []
    assert len(body["new_daily"]) == 30


async def test_admin_downloads_and_top_media(client, admin_token, db):
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC).replace(microsecond=0)
    yt = "https://www.youtube.com/"
    # 标题 A：4 条（1080p×2 → 720p×2，最新一条决定 format/site）
    for i, fmt in enumerate(["1080p", "1080p", "720p", "720p"]):
        seed_event(db, "download_completed", created_at=now - timedelta(minutes=40 - i),
                   media_title="A", media_url=yt, format_label=fmt)
    seed_event(db, "download_completed", created_at=now - timedelta(minutes=5), media_title="B")
    # 占位标题 5 条：计数最高但必须不进榜
    for i in range(5):
        seed_event(db, "download_completed", created_at=now - timedelta(minutes=30 - i),
                   media_title="任务 #12", media_url=yt, format_label="1080p")
    seed_event(db, "download_completed", created_at=now - timedelta(minutes=4), media_title=None)
    for _ in range(5):
        seed_event(db, "parse_requested", created_at=now, media_url=yt)
    for _ in range(3):
        seed_event(db, "parse_succeeded", created_at=now, media_url=yt)
    seed_event(db, "download_failed", created_at=now, error="proxyFailed")
    seed_event(db, "download_failed", created_at=now - timedelta(minutes=1), error="proxyFailed")
    seed_event(db, "download_failed", created_at=now, error=None)

    body = (await client.get("/api/analytics/admin/downloads?days=7", headers=ADMIN_HEADERS)).json()
    assert [s["name"] for s in body["funnel"]] == [
        "parse_requested", "parse_succeeded", "download_started", "download_completed",
    ]
    assert [s["count"] for s in body["funnel"]] == [5, 3, 0, 11]
    assert {"name": "www.youtube.com", "count": 9} in body["sites"]
    assert {"name": "unknown", "count": 2} in body["sites"]
    assert {"name": "1080p", "count": 7} in body["formats"]
    failures = {f["error"]: f["count"] for f in body["failures"]}
    assert failures == {"proxyFailed": 2, "未分类": 1}

    top = (await client.get("/api/analytics/admin/top-media?days=7", headers=ADMIN_HEADERS)).json()
    assert [i["title"] for i in top["items"]] == ["A", "B"]
    assert top["items"][0]["completed_count"] == 4
    assert top["items"][0]["format_label"] == "720p"  # 组内最新一条的值
    assert top["items"][0]["site"] == "www.youtube.com"

    for path in ["/api/analytics/admin/downloads", "/api/analytics/admin/top-media"]:
        assert (await client.get(path)).status_code == 401


async def test_admin_events_search_masking_and_truncation(client, admin_token, db):
    from datetime import UTC, datetime

    from downloadany_backend.models import User

    now = datetime.now(UTC)
    u = User(email="x@b.com", password_hash="y" * 40, email_verified=True, created_at=now)
    db.add(u)
    db.commit()

    seed_event(db, "download_completed", created_at=now, media_title="100% 真的_好看",
               media_url="https://www.youtube.com/", ip_address="12.34.56.78")
    seed_event(db, "parse_requested", created_at=now, media_title="普通标题",
               media_url="https://www.douyin.com/")
    seed_event(db, "app_open", created_at=now, user_id=u.id)
    seed_event(db, "download_failed", created_at=now, error="e" * 250)

    async def get_events(**params):
        r = await client.get("/api/analytics/admin/events",
                             headers=ADMIN_HEADERS, params={"days": 30, **params})
        assert r.status_code == 200
        return r.json()

    # Review Focus 4：`%`/`_` 按字面匹配，不得当通配符
    assert (await get_events(q="100% 真的_"))["total"] == 1
    assert (await get_events(q="%"))["total"] == 1
    assert (await get_events(q="_"))["total"] == 1
    # 组合过滤
    assert (await get_events(event_type="parse_requested", site="www.douyin.com"))["total"] == 1
    assert (await get_events(site="www.youtube.com"))["total"] == 1

    page = await get_events()
    assert page["total"] == 4 and page["limit"] == 50
    by_type = {row["event_type"]: row for row in page["items"]}
    assert by_type["download_completed"]["masked_ip"] == "12.34.*.*"
    assert by_type["app_open"]["email_masked"] == "x***@b.com"
    assert by_type["parse_requested"]["email_masked"] is None
    # error 截断 200 + 省略号
    err = by_type["download_failed"]["error"]
    assert len(err) == 201 and err.endswith("…")

    overflow = await get_events(offset=100)
    assert overflow["items"] == [] and overflow["total"] == 4

    assert (await client.get("/api/analytics/admin/events")).status_code == 401


async def test_admin_system_device_latest_releases_feedback(client, admin_token, db):
    from datetime import UTC, datetime, timedelta

    from downloadany_backend.models import (
        ComponentRelease,
        DesktopRelease,
        Feedback,
        utcnow,
    )

    now = datetime.now(UTC)
    d1 = "11111111-1111-1111-1111-111111111111"
    d2 = "22222222-2222-2222-2222-222222222222"
    seed_event(db, "app_open", created_at=now - timedelta(hours=2), device_id=d1,
               platform="Win32", app_version="0.1.0")
    seed_event(db, "app_open", created_at=now - timedelta(hours=1), device_id=d1,
               platform="Mac", app_version="0.2.0")
    seed_event(db, "app_open", created_at=now - timedelta(minutes=30), device_id=d2,
               platform="Mac", app_version="0.1.0")

    db.add(DesktopRelease(
        version="0.2.0", channel="stable", target="windows", arch="x86_64",
        artifact_path="desktop/0.2.0/setup.exe", signature="s", active=True,
        published_at=now - timedelta(days=2),
    ))
    db.add(DesktopRelease(  # 同三元组的更新版但未启用 → 不进发布状态
        version="0.3.0", channel="stable", target="windows", arch="x86_64",
        artifact_path="desktop/0.3.0/setup.exe", signature="s", active=False,
        published_at=now,
    ))
    db.add(ComponentRelease(
        component="yt-dlp", version="2026.10.1", channel="stable",
        target="aarch64-apple-darwin", artifact_path="components/yt-dlp/x",
        signature="s", sha256="a" * 64, active=True, published_at=now - timedelta(days=1),
    ))
    db.add(Feedback(kind="bug", text="x" * 400, app_version="0.1.0", platform="Mac",
                    created_at=now - timedelta(minutes=5)))
    db.add(Feedback(kind="feature", text="短建议", app_version="0.1.0", platform="Win32",
                    created_at=now))
    db.commit()

    body = (await client.get("/api/analytics/admin/system?days=7", headers=ADMIN_HEADERS)).json()
    assert {p["name"]: p["count"] for p in body["platforms"]} == {"Mac": 2}
    assert {v["name"]: v["count"] for v in body["versions"]} == {"0.2.0": 1, "0.1.0": 1}
    assert body["feedback_count"] == 2
    assert body["feedback_recent"][0]["text"] == "短建议"
    long_item = next(f for f in body["feedback_recent"] if len(f["text"]) > 100)
    assert len(long_item["text"]) == 301 and long_item["text"].endswith("…")
    releases = {(r["kind"], r["name"], r["version"]) for r in body["releases"]}
    assert releases == {("desktop", "windows/x86_64", "0.2.0"),
                        ("component", "yt-dlp", "2026.10.1")}

    assert (await client.get("/api/analytics/admin/system")).status_code == 401
