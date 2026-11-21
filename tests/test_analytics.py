from uuid import uuid4

import pytest
from sqlalchemy import func, select

from downloadany_backend.models import AnalyticsEvent


pytestmark = pytest.mark.asyncio


def event_payload(event_type: str = "app_open") -> dict:
    return {
        "event_id": str(uuid4()),
        "event_type": event_type,
        "device_id": str(uuid4()),
        "client_session_id": str(uuid4()),
        "app_version": "0.1.0",
        "platform": "Win32",
    }


async def test_anonymous_event_is_recorded_and_duplicate_is_idempotent(client, db):
    payload = event_payload("download_started") | {
        "media_id": "abc123",
        "media_title": "Example video",
        "media_url": "https://www.youtube.com/watch?v=abc123",
        "format_id": "__video_1080p__",
        "format_label": "1080p",
    }

    first = await client.post("/api/analytics/events", json=payload)
    duplicate = await client.post("/api/analytics/events", json=payload)

    assert first.status_code == 202
    assert duplicate.status_code == 202
    assert db.scalar(select(func.count()).select_from(AnalyticsEvent)) == 1
    row = db.scalar(select(AnalyticsEvent))
    assert row.user_id is None
    assert row.media_site == "www.youtube.com"
    assert row.media_title == "Example video"


async def test_event_rejects_malformed_type_and_invalid_media_url(client):
    # 拒的是"不成形"，不是"没见过"：桌面端与后台独立发版，新事件类型必然先到
    for malformed in ["NOT_A_TYPE", "with space", "<script>", "a", "x" * 33, ""]:
        response = await client.post("/api/analytics/events", json=event_payload(malformed))
        assert response.status_code == 422, malformed

    invalid_url = event_payload("parse_requested") | {"media_url": "file:///secret"}
    assert (await client.post("/api/analytics/events", json=invalid_url)).status_code == 422


# 与 DownloadAny/src/lib/analytics.ts 的 AnalyticsEventType 保持一致
DESKTOP_EVENT_TYPES = [
    "app_open",
    "parse_requested",
    "parse_succeeded",
    "parse_failed",
    "download_started",
    "download_completed",
    "download_failed",
    "download_cancelled",
    "screen_view",
    "dialog_opened",
    "setting_changed",
    "parse_blocked",
    "login_succeeded",
    "signup_succeeded",
    "feedback_submitted",
    "download_cancel_requested",
    "download_enqueue_failed",
]


@pytest.mark.parametrize("event_type", DESKTOP_EVENT_TYPES)
async def test_every_desktop_event_type_is_accepted(client, db, event_type):
    """后台漏收事件类型是静默的：客户端 catch 掉一切失败，故这里逐条钉住。"""
    response = await client.post("/api/analytics/events", json=event_payload(event_type))
    assert response.status_code == 202
    assert db.scalar(select(AnalyticsEvent).where(AnalyticsEvent.event_type == event_type))


async def test_overview_survives_an_unseen_but_well_formed_event_type(client):
    """未登录过的新类型也得能被仪表盘读出：响应模型若收窄成枚举，overview 会整页 500。"""
    payload = event_payload("widget_clicked")
    assert (await client.post("/api/analytics/events", json=payload)).status_code == 202

    response = await client.get("/api/analytics/overview?days=7")
    assert response.status_code == 200
    recent_types = {item["event_type"] for item in response.json()["recent_events"]}
    assert "widget_clicked" in recent_types


async def test_analytics_overview_aggregates_events_without_sensitive_fields(client, db):
    device_id = str(uuid4())
    session_id = str(uuid4())
    for event_type in ["app_open", "parse_requested", "download_started", "download_completed"]:
        payload = event_payload(event_type) | {
            "device_id": device_id,
            "client_session_id": session_id,
            "media_url": "https://www.youtube.com/watch?v=abc123",
        }
        assert (await client.post("/api/analytics/events", json=payload)).status_code == 202

    response = await client.get("/api/analytics/overview?days=7")
    assert response.status_code == 200
    data = response.json()
    assert data["active_devices"]["value"] == 1
    assert data["parse_requests"]["value"] == 1
    assert data["downloads_completed"]["value"] == 1
    assert data["download_success_rate"]["value"] == 100.0
    assert len(data["daily"]) == 7
    assert len(data["recent_events"]) == 4
    assert "media_url" not in data["recent_events"][0]
    assert "ip_address" not in data["recent_events"][0]


async def test_analytics_overview_clamps_date_range(client, db):
    response = await client.get("/api/analytics/overview?days=999")
    assert response.status_code == 200
    assert response.json()["days"] == 90
    assert len(response.json()["daily"]) == 90
