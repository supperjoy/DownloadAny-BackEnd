import pytest

from downloadany_backend.database import SessionLocal
from downloadany_backend.models import DesktopRelease, utcnow


pytestmark = pytest.mark.asyncio


async def test_desktop_update_returns_tauri_payload(client, monkeypatch):
    from downloadany_backend.config import get_settings

    monkeypatch.setattr(get_settings(), "update_cdn_base_url", "https://cdn.example.com/downloadany")
    with SessionLocal() as db:
        db.add(DesktopRelease(
            version="0.2.0",
            channel="stable",
            target="windows",
            arch="x86_64",
            artifact_path="desktop/0.2.0/setup.exe",
            signature="signed-value",
            notes="test release",
            rollout_percent=100,
            active=True,
            published_at=utcnow(),
        ))
        db.commit()

    response = await client.get(
        "/api/updates/desktop/windows/x86_64/0.1.0",
        headers={"X-DownloadAny-Device-Id": "device-a"},
    )
    assert response.status_code == 200
    assert response.json()["version"] == "0.2.0"
    assert response.json()["url"] == "https://cdn.example.com/downloadany/desktop/0.2.0/setup.exe"


async def test_rollout_is_stable_and_minimum_version_bypasses_it(client, monkeypatch):
    from downloadany_backend.config import get_settings

    monkeypatch.setattr(get_settings(), "update_cdn_base_url", "https://cdn.example.com")
    with SessionLocal() as db:
        db.add(DesktopRelease(
            version="2.0.0",
            channel="stable",
            target="windows",
            arch="x86_64",
            artifact_path="setup.exe",
            signature="sig",
            rollout_percent=0,
            minimum_version="1.0.0",
            active=True,
            published_at=utcnow(),
        ))
        db.commit()

    old = await client.get("/api/updates/desktop/windows/x86_64/0.9.0")
    supported = await client.get("/api/updates/desktop/windows/x86_64/1.1.0")
    assert old.status_code == 200
    assert supported.status_code == 204


async def test_beta_channel_can_receive_beta_release(client, monkeypatch):
    from downloadany_backend.config import get_settings

    monkeypatch.setattr(get_settings(), "update_cdn_base_url", "https://cdn.example.com")
    with SessionLocal() as db:
        db.add(DesktopRelease(
            version="0.3.0b1",
            channel="beta",
            target="windows",
            arch="x86_64",
            artifact_path="beta.exe",
            signature="sig",
            rollout_percent=100,
            active=True,
            published_at=utcnow(),
        ))
        db.commit()

    stable = await client.get("/api/updates/desktop/windows/x86_64/0.2.0")
    beta = await client.get(
        "/api/updates/desktop/windows/x86_64/0.2.0",
        headers={"X-DownloadAny-Channel": "beta"},
    )
    assert stable.status_code == 204
    assert beta.status_code == 200
