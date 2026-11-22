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
