from datetime import timedelta

from sqlalchemy import select

from downloadany_backend.models import ActionToken, AuthSession, CaptchaChallenge, User, utcnow
from downloadany_backend.security import sha256
import pytest


pytestmark = pytest.mark.asyncio


EMAIL = "alice@example.com"
PASSWORD = "correct-horse-123"


async def register_and_verify(client, db):
    response = await client.post("/api/auth/register", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 202
    user = db.scalar(select(User).where(User.email == EMAIL))
    token = "known-verification-token"
    db.add(
        ActionToken(
            user_id=user.id,
            purpose="verify_email",
            token_hash=sha256(token),
            code_hash=sha256(f"{EMAIL}:123456"),
            expires_at=utcnow() + timedelta(minutes=10),
        )
    )
    db.commit()
    assert (await client.post("/api/auth/verification/link", json={"token": token})).status_code == 200
    db.refresh(user)
    return user


def add_email_login_code(db, user, code="654321"):
    db.add(
        ActionToken(
            user_id=user.id,
            purpose="email_login",
            token_hash=sha256(f"token-{user.id}-{code}"),
            code_hash=sha256(f"{user.email}:{code}"),
            expires_at=utcnow() + timedelta(minutes=10),
        )
    )
    db.commit()


async def test_full_password_auth_lifecycle(client, db):
    user = await register_and_verify(client, db)
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


async def test_verification_code_is_one_time(client, db):
    await register_and_verify(client, db)
    result = await client.post("/api/auth/verification/code", json={"email": EMAIL, "code": "123456"})
    assert result.status_code == 400


async def test_registration_and_reset_do_not_enumerate_email(client, db):
    await register_and_verify(client, db)
    registered = await client.post("/api/auth/forgot-password", json={"email": EMAIL})
    missing = await client.post("/api/auth/forgot-password", json={"email": "missing@example.com"})
    assert registered.status_code == missing.status_code == 202
    assert registered.json() == missing.json()

    duplicate = await client.post("/api/auth/register", json={"email": EMAIL, "password": "something-else-123"})
    fresh = await client.post(
        "/api/auth/register", json={"email": "fresh@example.com", "password": "something-else-123"}
    )
    assert duplicate.status_code == fresh.status_code == 202
    assert duplicate.json() == fresh.json()


async def test_failed_logins_require_captcha(client, db):
    await register_and_verify(client, db)
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
    user = await register_and_verify(client, db)
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


async def test_email_code_first_login_creates_and_activates_passwordless_user(client, db):
    email = "code-user@example.com"
    sent = await client.post("/api/auth/login/code/send", json={"email": email})
    assert sent.status_code == 202
    user = db.scalar(select(User).where(User.email == email))
    assert user.password_hash is None
    assert user.email_verified is False
    assert db.scalar(
        select(ActionToken).where(ActionToken.user_id == user.id, ActionToken.purpose == "email_login")
    )

    add_email_login_code(db, user)
    logged_in = await client.post("/api/auth/login/code", json={"email": email, "code": "654321"})
    assert logged_in.status_code == 200
    assert logged_in.json()["expires_in"] == 1800
    assert logged_in.json()["refresh_token"]
    db.refresh(user)
    assert user.email_verified is True

    reused = await client.post("/api/auth/login/code", json={"email": email, "code": "654321"})
    assert reused.status_code == 401


async def test_passwordless_user_can_set_password(client, db):
    email = "passwordless@example.com"
    user = User(email=email, password_hash=None, email_verified=True)
    db.add(user)
    db.commit()
    add_email_login_code(db, user)
    login = await client.post("/api/auth/login/code", json={"email": email, "code": "654321"})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert (await client.get("/api/auth/me", headers=headers)).json()["has_password"] is False
    changed = await client.post(
        "/api/auth/change-password",
        headers=headers,
        json={"new_password": "first-password-123"},
    )
    assert changed.status_code == 200
    assert (await client.get("/api/auth/me", headers=headers)).json()["has_password"] is True
    assert (await client.post(
        "/api/auth/login", json={"email": email, "password": "first-password-123"}
    )).status_code == 200


async def test_email_code_existing_user_and_brute_force_limit(client, db):
    user = await register_and_verify(client, db)
    original_password_hash = user.password_hash
    sent = await client.post("/api/auth/login/code/send", json={"email": EMAIL})
    assert sent.status_code == 202
    db.refresh(user)
    assert user.password_hash == original_password_hash

    for _ in range(5):
        result = await client.post("/api/auth/login/code", json={"email": EMAIL, "code": "000000"})
        assert result.status_code == 401
    limited = await client.post("/api/auth/login/code", json={"email": EMAIL, "code": "000000"})
    assert limited.status_code == 429
    db.refresh(user)
    assert user.failed_login_count == 5
    assert user.locked_until is not None
