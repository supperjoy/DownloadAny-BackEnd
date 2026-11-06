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
