import pytest

from downloadany_backend import feedback_routes
from downloadany_backend.database import SessionLocal
from downloadany_backend.models import Feedback

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def clear_rate_limit():
    feedback_routes._rate_hits.clear()
    yield
    feedback_routes._rate_hits.clear()


def headers(device: str = "f" * 36) -> dict[str, str]:
    return {"X-DownloadAny-Device-Id": device}


async def test_feedback_stored(client):
    response = await client.post(
        "/api/feedback",
        json={"kind": "bug", "text": "下载 YouTube 卡在 87%", "app_version": "0.1.2", "platform": "macos"},
        headers=headers(),
    )
    assert response.status_code == 201
    with SessionLocal() as db:
        row = db.query(Feedback).one()
        assert row.kind == "bug"
        assert row.app_version == "0.1.2"
        assert row.device_id == "f" * 36


async def test_text_limits(client):
    ok = await client.post("/api/feedback", json={"kind": "feature", "text": "字" * 300}, headers=headers("a" * 36))
    assert ok.status_code == 201
    too_long = await client.post("/api/feedback", json={"kind": "feature", "text": "字" * 301}, headers=headers("a" * 36))
    assert too_long.status_code == 422
    blank = await client.post("/api/feedback", json={"kind": "feature", "text": "   "}, headers=headers("a" * 36))
    assert blank.status_code == 422
    bad_kind = await client.post("/api/feedback", json={"kind": "spam", "text": "hi"}, headers=headers("a" * 36))
    assert bad_kind.status_code == 422


async def test_rate_limit_is_per_device(client):
    for i in range(5):
        response = await client.post("/api/feedback", json={"kind": "bug", "text": f"问题{i}"}, headers=headers("b" * 36))
        assert response.status_code == 201
    limited = await client.post("/api/feedback", json={"kind": "bug", "text": "第六条"}, headers=headers("b" * 36))
    assert limited.status_code == 429
    other = await client.post("/api/feedback", json={"kind": "bug", "text": "别的设备"}, headers=headers("c" * 36))
    assert other.status_code == 201
