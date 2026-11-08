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


async def test_event_rejects_unknown_type_and_invalid_media_url(client):
    unknown = event_payload("unknown")
    assert (await client.post("/api/analytics/events", json=unknown)).status_code == 422

    invalid_url = event_payload("parse_requested") | {"media_url": "file:///secret"}
    assert (await client.post("/api/analytics/events", json=invalid_url)).status_code == 422


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
