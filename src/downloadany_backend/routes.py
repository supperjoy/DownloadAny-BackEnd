import secrets
from datetime import timedelta
from uuid import uuid4

import requests as http_requests
from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response, status
from google.auth.exceptions import GoogleAuthError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .auth_service import (
    GENERIC_EMAIL_MESSAGE,
    consume_action_code,
    consume_action_token,
    create_session,
    recent_failures,
    record_login,
    send_action_email,
)
from .config import Settings, get_settings
from .database import get_db
from .dependencies import client_ip, current_auth, current_user
from .models import AuditLog, AuthSession, CaptchaChallenge, LoginEvent, User, utcnow
from .schemas import (
    ActionTokenRequest,
    CaptchaResponse,
    ChangePasswordRequest,
    EmailRequest,
    GoogleCodeLoginRequest,
    GoogleLoginRequest,
    LoginRequest,
    MessageResponse,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UserResponse,
    VerifyCodeRequest,
)
from .security import (
    create_access_token,
    fingerprint,
    hash_password,
    normalize_email,
    sha256,
    verify_password,
)


router = APIRouter(prefix="/api/auth", tags=["authentication"])
INVALID_LOGIN = "邮箱或密码错误"


def token_response(
    response: Response, db: Session, user: User, ip: str, user_agent: str, settings: Settings
) -> TokenResponse:
    session, refresh = create_session(db, user, ip, user_agent, settings)
    access = create_access_token(user.id, session.id, settings)
    db.commit()
    response.set_cookie(
        "refresh_token",
        refresh,
        max_age=settings.refresh_token_days * 86400,
        httponly=True,
        secure=settings.environment == "production",
        samesite="lax",
        path="/api/auth",
    )
    return TokenResponse(
        access_token=access,
        expires_in=settings.access_token_minutes * 60,
        refresh_token=refresh,
    )


def valid_captcha(db: Session, challenge_id: str | None, answer: str | None, ip: str) -> bool:
    if not challenge_id or not answer:
        return False
    challenge = db.get(CaptchaChallenge, challenge_id)
    if (
        not challenge
        or challenge.ip_address != ip
        or challenge.consumed_at is not None
        or challenge.expires_at <= utcnow()
    ):
        return False
    challenge.consumed_at = utcnow()
    return secrets.compare_digest(challenge.answer_hash, sha256(answer.strip()))


@router.get("/captcha", response_model=CaptchaResponse)
def captcha(db: Session = Depends(get_db), ip: str = Depends(client_ip)):
    left, right = secrets.randbelow(8) + 2, secrets.randbelow(8) + 2
    challenge = CaptchaChallenge(
        id=str(uuid4()),
        answer_hash=sha256(str(left + right)),
        ip_address=ip,
        expires_at=utcnow() + timedelta(minutes=5),
    )
    db.add(challenge)
    db.commit()
    return CaptchaResponse(captcha_id=challenge.id, question=f"{left} + {right} = ?", expires_in=300)


@router.post("/register", response_model=MessageResponse, status_code=status.HTTP_202_ACCEPTED)
def register(
    body: RegisterRequest,
    db: Session = Depends(get_db),
    ip: str = Depends(client_ip),
    settings: Settings = Depends(get_settings),
):
    email = normalize_email(str(body.email))
    user = db.scalar(select(User).where(User.email == email))
    if not user:
        user = User(email=email, password_hash=hash_password(body.password))
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            user = db.scalar(select(User).where(User.email == email))
    elif not user.email_verified and user.password_hash:
        # Do not overwrite a pending account's password through an unauthenticated endpoint.
        hash_password(body.password)
    else:
        # Reduce timing differences between new and existing registrations.
        hash_password(body.password)
    if user and not user.email_verified:
        send_action_email(db, user, "verify_email", ip, settings)
    return MessageResponse(message=GENERIC_EMAIL_MESSAGE)


@router.post("/verification/resend", response_model=MessageResponse, status_code=202)
def resend_verification(
    body: EmailRequest,
    db: Session = Depends(get_db),
    ip: str = Depends(client_ip),
    settings: Settings = Depends(get_settings),
):
    email = normalize_email(str(body.email))
    user = db.scalar(select(User).where(User.email == email))
    if user and not user.email_verified:
        send_action_email(db, user, "verify_email", ip, settings)
    return MessageResponse(message=GENERIC_EMAIL_MESSAGE)


def mark_verified(db: Session, user: User | None) -> MessageResponse:
    if not user:
        raise HTTPException(status_code=400, detail="验证凭据无效或已过期")
    user.email_verified = True
    db.commit()
    return MessageResponse(message="邮箱验证成功")


@router.post("/verification/code", response_model=MessageResponse)
def verify_code(
    body: VerifyCodeRequest,
    db: Session = Depends(get_db),
    ip: str = Depends(client_ip),
    settings: Settings = Depends(get_settings),
):
    email = normalize_email(str(body.email))
    since = utcnow() - timedelta(minutes=settings.login_window_minutes)
    email_fp = fingerprint(email, settings.jwt_secret)
    failed = db.scalar(
        select(func.count()).select_from(LoginEvent).where(
            LoginEvent.email_fingerprint == email_fp,
            LoginEvent.ip_address == ip,
            LoginEvent.reason == "verification_code",
            LoginEvent.created_at >= since,
        )
    )
    if failed >= settings.max_login_failures:
        raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")
    user = consume_action_code(db, email, body.code, "verify_email")
    if not user:
        db.add(
            LoginEvent(
                email_fingerprint=email_fp,
                ip_address=ip,
                successful=False,
                reason="verification_code",
            )
        )
        db.commit()
    return mark_verified(db, user)


@router.post("/verification/link", response_model=MessageResponse)
def verify_link(body: ActionTokenRequest, db: Session = Depends(get_db)):
    return mark_verified(db, consume_action_token(db, body.token, "verify_email"))


@router.post("/login/code/send", response_model=MessageResponse, status_code=202)
def send_login_code(
    body: EmailRequest,
    db: Session = Depends(get_db),
    ip: str = Depends(client_ip),
    settings: Settings = Depends(get_settings),
):
    email = normalize_email(str(body.email))
    user = db.scalar(select(User).where(User.email == email))
    if not user:
        user = User(email=email, password_hash=None, email_verified=False)
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            user = db.scalar(select(User).where(User.email == email))
    if user and user.is_active:
        send_action_email(db, user, "email_login", ip, settings)
    return MessageResponse(message=GENERIC_EMAIL_MESSAGE)


@router.post("/login/code", response_model=TokenResponse)
def login_with_code(
    body: VerifyCodeRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    ip: str = Depends(client_ip),
    settings: Settings = Depends(get_settings),
):
    email = normalize_email(str(body.email))
    email_fp = fingerprint(email, settings.jwt_secret)
    since = utcnow() - timedelta(minutes=settings.login_window_minutes)
    failure_filter = (
        LoginEvent.successful.is_(False),
        LoginEvent.reason == "email_login_code",
        LoginEvent.created_at >= since,
    )
    email_failures = db.scalar(
        select(func.count()).select_from(LoginEvent).where(
            LoginEvent.email_fingerprint == email_fp, *failure_filter
        )
    )
    ip_failures = db.scalar(
        select(func.count()).select_from(LoginEvent).where(
            LoginEvent.ip_address == ip, *failure_filter
        )
    )
    if email_failures >= settings.max_login_failures or ip_failures >= settings.max_ip_login_failures:
        raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")

    account = db.scalar(select(User).where(User.email == email))
    locked = bool(account and account.locked_until and account.locked_until > utcnow())
    user = None if locked else consume_action_code(db, email, body.code, "email_login")
    if not user or not user.is_active:
        db.add(
            LoginEvent(
                email_fingerprint=email_fp,
                ip_address=ip,
                successful=False,
                reason="email_login_code",
            )
        )
        if account:
            account.failed_login_count = email_failures + 1
            if account.failed_login_count >= settings.max_login_failures:
                account.locked_until = utcnow() + timedelta(minutes=settings.account_lock_minutes)
        db.add(
            AuditLog(
                user_id=account.id if account else None,
                event="email_code_login_failed",
                ip_address=ip,
                detail="invalid_or_expired_code",
            )
        )
        db.commit()
        raise HTTPException(status_code=401, detail="验证码无效或已过期")

    user.email_verified = True
    user.failed_login_count = 0
    user.locked_until = None
    record_login(db, email, ip, True, "email_login", settings)
    db.add(AuditLog(user_id=user.id, event="email_code_login_success", ip_address=ip))
    return token_response(response, db, user, ip, request.headers.get("user-agent", ""), settings)


@router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    ip: str = Depends(client_ip),
    settings: Settings = Depends(get_settings),
):
    email = normalize_email(str(body.email))
    email_failures, ip_failures = recent_failures(db, email, ip, settings)
    if ip_failures >= settings.max_ip_login_failures:
        raise HTTPException(status_code=429, detail="尝试次数过多，请稍后再试")
    if email_failures >= settings.captcha_after_failures and not valid_captcha(
        db, body.captcha_id, body.captcha_answer, ip
    ):
        db.commit()
        raise HTTPException(status_code=428, detail="需要有效的验证码", headers={"X-Captcha-Required": "true"})

    user = db.scalar(select(User).where(User.email == email))
    password_ok = verify_password(body.password, user.password_hash if user else None)
    locked = bool(user and user.locked_until and user.locked_until > utcnow())
    if not user or not password_ok or not user.email_verified or not user.is_active or locked:
        record_login(db, email, ip, False, "invalid_credentials", settings)
        if user and password_ok and not user.email_verified:
            reason = "unverified"
        else:
            reason = "invalid_credentials"
        if user:
            user.failed_login_count = email_failures + 1
            if user.failed_login_count >= settings.max_login_failures:
                user.locked_until = utcnow() + timedelta(minutes=settings.account_lock_minutes)
        db.add(AuditLog(user_id=user.id if user else None, event="login_failed", ip_address=ip, detail=reason))
        db.commit()
        raise HTTPException(status_code=401, detail=INVALID_LOGIN)

    user.failed_login_count = 0
    user.locked_until = None
    record_login(db, email, ip, True, "success", settings)
    db.add(AuditLog(user_id=user.id, event="login_success", ip_address=ip))
    return token_response(response, db, user, ip, request.headers.get("user-agent", ""), settings)


@router.post("/forgot-password", response_model=MessageResponse, status_code=202)
def forgot_password(
    body: EmailRequest,
    db: Session = Depends(get_db),
    ip: str = Depends(client_ip),
    settings: Settings = Depends(get_settings),
):
    email = normalize_email(str(body.email))
    user = db.scalar(select(User).where(User.email == email))
    if user and user.is_active:
        send_action_email(db, user, "reset_password", ip, settings)
    return MessageResponse(message=GENERIC_EMAIL_MESSAGE)


@router.post("/reset-password", response_model=MessageResponse)
def reset_password(body: ResetPasswordRequest, db: Session = Depends(get_db)):
    user = consume_action_token(db, body.token, "reset_password")
    if not user:
        raise HTTPException(status_code=400, detail="重置凭据无效或已过期")
    user.password_hash = hash_password(body.new_password)
    user.password_changed_at = utcnow()
    db.execute(update(AuthSession).where(AuthSession.user_id == user.id).values(revoked_at=utcnow()))
    db.commit()
    return MessageResponse(message="密码已重置，请重新登录")


@router.post("/change-password", response_model=MessageResponse)
def change_password(
    body: ChangePasswordRequest,
    auth: tuple[User, AuthSession] = Depends(current_auth),
    db: Session = Depends(get_db),
):
    user, current_session = auth
    had_password = user.password_hash is not None
    if had_password and (
        not body.current_password or not verify_password(body.current_password, user.password_hash)
    ):
        raise HTTPException(status_code=400, detail="当前密码不正确")
    if had_password and verify_password(body.new_password, user.password_hash):
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")
    user.password_hash = hash_password(body.new_password)
    user.password_changed_at = utcnow()
    db.execute(
        update(AuthSession)
        .where(AuthSession.user_id == user.id, AuthSession.id != current_session.id)
        .values(revoked_at=utcnow())
    )
    db.commit()
    message = "密码修改成功，其他设备已退出登录" if had_password else "密码设置成功"
    return MessageResponse(message=message)


def google_http_session(settings: Settings) -> http_requests.Session:
    session = http_requests.Session()
    if settings.google_proxy:
        session.proxies.update({"http": settings.google_proxy, "https": settings.google_proxy})
    return session


def complete_google_login(
    credential: str,
    request: Request,
    response: Response,
    db: Session,
    ip: str,
    settings: Settings,
) -> TokenResponse:
    if not settings.google_client_id:
        raise HTTPException(status_code=503, detail="Google 登录尚未配置")
    try:
        claims = id_token.verify_oauth2_token(
            credential,
            google_requests.Request(session=google_http_session(settings)),
            settings.google_client_id,
        )
    except ValueError:
        raise HTTPException(status_code=401, detail="Google 凭据无效")
    except (GoogleAuthError, http_requests.RequestException):
        raise HTTPException(status_code=502, detail="无法连接 Google 身份验证服务")
    if not claims.get("email_verified") or not claims.get("email") or not claims.get("sub"):
        raise HTTPException(status_code=401, detail="Google 邮箱尚未验证")
    email = normalize_email(claims["email"])
    user = db.scalar(select(User).where(User.email == email))
    if user and user.google_subject and user.google_subject != claims["sub"]:
        raise HTTPException(status_code=409, detail="该邮箱已关联其他 Google 账号")
    if not user:
        user = User(email=email, google_subject=claims["sub"], email_verified=True)
        db.add(user)
        db.flush()
    else:
        user.google_subject = claims["sub"]
        user.email_verified = True
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号不可用")
    db.add(AuditLog(user_id=user.id, event="google_login", ip_address=ip))
    return token_response(response, db, user, ip, request.headers.get("user-agent", ""), settings)


@router.post("/google", response_model=TokenResponse)
def google_login(
    body: GoogleLoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    ip: str = Depends(client_ip),
    settings: Settings = Depends(get_settings),
):
    return complete_google_login(body.credential, request, response, db, ip, settings)


@router.post("/google/code", response_model=TokenResponse)
def google_code_login(
    body: GoogleCodeLoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    ip: str = Depends(client_ip),
    settings: Settings = Depends(get_settings),
):
    if not settings.google_client_id or not settings.google_client_secret:
        raise HTTPException(status_code=503, detail="Google 登录的服务端密钥尚未配置")
    try:
        token_response_from_google = google_http_session(settings).post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "code": body.code,
                "code_verifier": body.code_verifier,
                "grant_type": "authorization_code",
                "redirect_uri": body.redirect_uri,
            },
            timeout=15,
        )
        token_payload = token_response_from_google.json()
    except (http_requests.RequestException, ValueError):
        raise HTTPException(status_code=502, detail="无法连接 Google 身份验证服务")
    if not token_response_from_google.ok:
        detail = token_payload.get("error_description") or token_payload.get("error")
        raise HTTPException(status_code=401, detail=f"Google 授权码交换失败：{detail or '未知错误'}")
    credential = token_payload.get("id_token")
    if not credential:
        raise HTTPException(status_code=401, detail="Google 未返回身份令牌")
    return complete_google_login(credential, request, response, db, ip, settings)


@router.post("/refresh", response_model=TokenResponse)
def refresh(
    body: RefreshRequest,
    request: Request,
    response: Response,
    cookie_token: str | None = Cookie(default=None, alias="refresh_token"),
    db: Session = Depends(get_db),
    ip: str = Depends(client_ip),
    settings: Settings = Depends(get_settings),
):
    raw = body.refresh_token or cookie_token
    session = db.scalar(select(AuthSession).where(AuthSession.refresh_token_hash == sha256(raw or "")))
    if not session or session.revoked_at is not None or session.expires_at <= utcnow():
        raise HTTPException(status_code=401, detail="刷新凭据无效或已过期")
    user = db.get(User, session.user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="刷新凭据无效或已过期")
    # Refresh token rotation detects replay: the old token becomes unusable immediately.
    session.revoked_at = utcnow()
    return token_response(response, db, user, ip, request.headers.get("user-agent", ""), settings)


@router.post("/logout", response_model=MessageResponse)
def logout(
    response: Response,
    auth: tuple[User, AuthSession] = Depends(current_auth),
    db: Session = Depends(get_db),
):
    _, session = auth
    session.revoked_at = utcnow()
    db.commit()
    response.delete_cookie("refresh_token", path="/api/auth")
    return MessageResponse(message="已退出登录")


@router.get("/me", response_model=UserResponse)
def me(user: User = Depends(current_user)):
    return UserResponse(
        id=user.id,
        email=user.email,
        email_verified=user.email_verified,
        has_password=user.password_hash is not None,
    )
