from datetime import timedelta

from sqlalchemy import select

from downloadany_backend.config import get_settings
from downloadany_backend.models import ActionToken, AuditLog, AuthSession, CaptchaChallenge, User, utcnow
from downloadany_backend.security import hash_password, sha256, verify_password
import pytest


pytestmark = pytest.mark.asyncio


EMAIL = "alice@example.com"
PASSWORD = "correct-horse-123"


async def register(client, email=EMAIL, password=PASSWORD):
    """注册三要素：邮箱 + 密码 + 确认密码；成功即登录，不再发邮件验证码。"""
    return await client.post(
        "/api/auth/register",
        json={"email": email, "password": password, "confirm_password": password},
    )


async def registered_user(client, db, email=EMAIL, password=PASSWORD) -> User:
    response = await register(client, email, password)
    assert response.status_code == 200
    user = db.scalar(select(User).where(User.email == email))
    db.refresh(user)
    return user


def add_reset_code(db, user, code):
    db.add(
        ActionToken(
            user_id=user.id,
            purpose="reset_password",
            token_hash=sha256(f"reset-token-{code}"),
            code_hash=sha256(f"{user.email}:{code}"),
            expires_at=utcnow() + timedelta(minutes=10),
        )
    )
    db.commit()


async def test_register_signs_the_user_in(client, db):
    tokens = (await register(client)).json()
    assert tokens["expires_in"] == 1800
    assert tokens["refresh_token"]
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    me = await client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["email"] == EMAIL
    assert me.json()["email_verified"] is True
    assert me.json()["has_password"] is True
    # 注册成功也能直接用密码登录
    assert (await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})).status_code == 200


async def test_register_requires_matching_confirmation(client, db):
    response = await client.post(
        "/api/auth/register",
        json={"email": EMAIL, "password": PASSWORD, "confirm_password": "different-pass-999"},
    )
    assert response.status_code == 422
    assert "不一致" in response.text
    assert db.scalar(select(User).where(User.email == EMAIL)) is None


async def test_register_rejects_missing_or_weak_password(client, db):
    missing = await client.post("/api/auth/register", json={"email": EMAIL, "password": PASSWORD})
    assert missing.status_code == 422

    weak = await client.post(
        "/api/auth/register",
        json={"email": EMAIL, "password": "abcdefghij", "confirm_password": "abcdefghij"},
    )
    assert weak.status_code == 422
    assert db.scalar(select(User).where(User.email == EMAIL)) is None


async def test_duplicate_register_points_to_login(client, db):
    await registered_user(client, db)
    duplicate = await register(client, EMAIL, "something-else-123")
    assert duplicate.status_code == 409
    assert "已注册" in duplicate.json()["detail"]
    # 注册接口不能替已注册账户改密码
    user = db.scalar(select(User).where(User.email == EMAIL))
    db.refresh(user)
    assert verify_password(PASSWORD, user.password_hash)


async def test_password_auth_lifecycle(client, db):
    await registered_user(client, db)
    login = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 200
    tokens = login.json()
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    assert (await client.get("/api/auth/me", headers=headers)).json()["email"] == EMAIL

    refreshed = await client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200
    assert (await client.post("/api/auth/refresh", json={"refresh_token": tokens["refresh_token"]})).status_code == 401

    second_headers = {"Authorization": f"Bearer {refreshed.json()['access_token']}"}
    changed = await client.post(
        "/api/auth/change-password",
        headers=second_headers,
        json={"current_password": PASSWORD, "new_password": "new-secure-456"},
    )
    assert changed.status_code == 200
    assert (await client.post("/api/auth/logout", headers=second_headers)).status_code == 200
    assert (await client.get("/api/auth/me", headers=second_headers)).status_code == 401

    assert (await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})).status_code == 401
    assert (await client.post(
        "/api/auth/login", json={"email": EMAIL, "password": "new-secure-456"}
    )).status_code == 200


async def test_login_unknown_email_is_told_to_register(client, db):
    response = await client.post("/api/auth/login", json={"email": "ghost@example.com", "password": PASSWORD})
    assert response.status_code == 404
    assert "尚未注册" in response.json()["detail"]
    assert db.scalar(select(User).where(User.email == "ghost@example.com")) is None


async def test_login_wrong_password_stays_generic(client, db):
    await registered_user(client, db)
    response = await client.post("/api/auth/login", json={"email": EMAIL, "password": "wrong-password-1"})
    assert response.status_code == 401
    assert response.json()["detail"] == "邮箱或密码错误"


async def test_login_google_only_account_is_not_told_to_register(client, db):
    db.add(User(email=EMAIL, password_hash=None, google_subject="google-sub-1", email_verified=True))
    db.commit()
    response = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 401
    assert response.json()["detail"] == "邮箱或密码错误"


async def test_legacy_shell_account_registers_with_a_password(client, db):
    """旧版邮箱验证码自动建号的空壳账户：登录引导注册，注册补设密码后即为正式账号。"""
    db.add(User(email=EMAIL, password_hash=None, email_verified=False))
    db.commit()

    login = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert login.status_code == 404
    assert "尚未注册" in login.json()["detail"]

    assert (await register(client)).status_code == 200
    assert (await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})).status_code == 200


async def test_email_code_login_endpoints_are_retired(client, db):
    for path in ("/api/auth/login/code/send", "/api/auth/login/code"):
        response = await client.post(path, json={"email": EMAIL, "code": "123456"})
        assert response.status_code == 404
    assert db.scalar(select(User).where(User.email == EMAIL)) is None


async def test_email_verification_endpoints_are_retired(client, db):
    for path in ("/api/auth/verification/resend", "/api/auth/verification/code", "/api/auth/verification/link"):
        response = await client.post(path, json={"email": EMAIL, "code": "123456", "token": "t"})
        assert response.status_code == 404


async def test_forgot_password_does_not_enumerate_email(client, db):
    await registered_user(client, db)
    registered = await client.post("/api/auth/forgot-password", json={"email": EMAIL})
    missing = await client.post("/api/auth/forgot-password", json={"email": "missing@example.com"})
    assert registered.status_code == missing.status_code == 202
    assert registered.json() == missing.json()


async def test_reset_password_via_email_code(client, db):
    user = await registered_user(client, db)
    login = (await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})).json()
    add_reset_code(db, user, "111222")

    bad = await client.post(
        "/api/auth/reset-password/code",
        json={"email": EMAIL, "code": "000000", "new_password": "fresh-passwd-999"},
    )
    assert bad.status_code == 400

    result = await client.post(
        "/api/auth/reset-password/code",
        json={"email": EMAIL, "code": "111222", "new_password": "fresh-passwd-999"},
    )
    assert result.status_code == 200

    assert (await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})).status_code == 401
    assert (
        await client.post("/api/auth/login", json={"email": EMAIL, "password": "fresh-passwd-999"})
    ).status_code == 200
    # 重置成功即吊销所有旧会话。
    old_headers = {"Authorization": f"Bearer {login['access_token']}"}
    assert (await client.get("/api/auth/me", headers=old_headers)).status_code == 401


async def test_reset_by_code_clears_lock_and_counter(client, db):
    user = await registered_user(client, db)
    user.failed_login_count = 5
    user.locked_until = utcnow() + timedelta(minutes=10)
    db.commit()
    add_reset_code(db, user, "333444")

    result = await client.post(
        "/api/auth/reset-password/code",
        json={"email": EMAIL, "code": "333444", "new_password": "unlocked-passwd-1"},
    )
    assert result.status_code == 200
    db.refresh(user)
    assert user.failed_login_count == 0
    assert user.locked_until is None
    login = await client.post("/api/auth/login", json={"email": EMAIL, "password": "unlocked-passwd-1"})
    assert login.status_code == 200


async def test_reset_by_link_clears_lock_and_verifies_legacy_account(client, db):
    """老版本注册但未点验证链接的账户：找回密码即证明邮箱所有权，重置后能直接密码登录。"""
    user = User(email=EMAIL, password_hash=None, email_verified=False)
    db.add(user)
    db.commit()
    user.password_hash = hash_password("legacy-passwd-000")
    db.commit()

    blocked = await client.post("/api/auth/login", json={"email": EMAIL, "password": "legacy-passwd-000"})
    assert blocked.status_code == 401

    reset_token = "reset-link-clears-lock"
    db.add(
        ActionToken(
            user_id=user.id,
            purpose="reset_password",
            token_hash=sha256(reset_token),
            code_hash=None,
            expires_at=utcnow() + timedelta(minutes=10),
        )
    )
    user.failed_login_count = 5
    user.locked_until = utcnow() + timedelta(minutes=10)
    db.commit()

    result = await client.post(
        "/api/auth/reset-password", json={"token": reset_token, "new_password": "unlocked-passwd-2"}
    )
    assert result.status_code == 200
    db.refresh(user)
    assert user.failed_login_count == 0
    assert user.locked_until is None
    assert user.email_verified is True
    assert (
        await client.post("/api/auth/login", json={"email": EMAIL, "password": "unlocked-passwd-2"})
    ).status_code == 200


async def test_failed_logins_require_captcha(client, db):
    await registered_user(client, db)
    for _ in range(3):
        response = await client.post("/api/auth/login", json={"email": EMAIL, "password": "wrong"})
        assert response.status_code == 401
    required = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
    assert required.status_code == 428
    assert required.headers["x-captcha-required"] == "true"

    captcha = (await client.get("/api/auth/captcha")).json()
    answer = str(eval(captcha["question"].replace("= ?", ""), {"__builtins__": {}}))
    success = await client.post(
        "/api/auth/login",
        json={
            "email": EMAIL,
            "password": PASSWORD,
            "captcha_id": captcha["captcha_id"],
            "captcha_answer": answer,
        },
    )
    assert success.status_code == 200
    challenge = db.get(CaptchaChallenge, captcha["captcha_id"])
    assert challenge.consumed_at is not None


async def test_password_reset_revokes_all_sessions(client, db):
    user = await registered_user(client, db)
    login = (await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})).json()
    reset_token = "known-reset-token"
    db.add(
        ActionToken(
            user_id=user.id,
            purpose="reset_password",
            token_hash=sha256(reset_token),
            code_hash=None,
            expires_at=utcnow() + timedelta(minutes=10),
        )
    )
    db.commit()
    result = await client.post(
        "/api/auth/reset-password", json={"token": reset_token, "new_password": "reset-secure-789"}
    )
    assert result.status_code == 200
    session = db.scalar(select(AuthSession).where(AuthSession.user_id == user.id))
    db.refresh(session)
    assert session.revoked_at is not None
    old_headers = {"Authorization": f"Bearer {login['access_token']}"}
    assert (await client.get("/api/auth/me", headers=old_headers)).status_code == 401


async def test_register_rate_limit_per_ip(client, db):
    await registered_user(client, db, email="first@example.com")
    ip = db.scalar(
        select(AuditLog).where(AuditLog.event == "register").order_by(AuditLog.id.desc()).limit(1)
    ).ip_address
    already = get_settings().max_registrations_per_ip - 1
    for _ in range(already):
        db.add(AuditLog(event="register", ip_address=ip))
    db.commit()

    limited = await register(client, email="second@example.com")
    assert limited.status_code == 429
    assert db.scalar(select(User).where(User.email == "second@example.com")) is None
