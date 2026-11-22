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
