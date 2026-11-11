import logging
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from .config import Settings
from .mailer import send_email
from .models import ActionToken, AuthSession, EmailSendLog, LoginEvent, User, utcnow
from .security import fingerprint, make_code, opaque_token, sha256


GENERIC_EMAIL_MESSAGE = "如果该邮箱可用，我们已发送后续操作邮件"
logger = logging.getLogger(__name__)


def email_allowed(db: Session, email: str, ip: str, settings: Settings) -> bool:
    now = utcnow()
    recipient = fingerprint(email, settings.jwt_secret)
    last = db.scalar(
        select(EmailSendLog)
        .where(EmailSendLog.recipient_fingerprint == recipient)
        .order_by(EmailSendLog.created_at.desc())
        .limit(1)
    )
    if last and (now - last.created_at).total_seconds() < settings.email_cooldown_seconds:
        return False
    hour_ago = now - timedelta(hours=1)
    recipient_count = db.scalar(
        select(func.count()).select_from(EmailSendLog).where(
            EmailSendLog.recipient_fingerprint == recipient,
            EmailSendLog.created_at >= hour_ago,
        )
    )
    ip_count = db.scalar(
        select(func.count()).select_from(EmailSendLog).where(
            EmailSendLog.ip_address == ip,
            EmailSendLog.created_at >= hour_ago,
        )
    )
    return recipient_count < settings.max_emails_per_hour and ip_count < settings.max_ip_emails_per_hour


def send_action_email(
    db: Session, user: User, purpose: str, ip: str, settings: Settings
) -> bool:
    """签发并发送一次性凭据（6 位验证码 + 链接）。

    注册与登录都已不再走邮件验证码，目前只有找回密码会用这里。
    """
    if purpose != "reset_password":
        raise ValueError(f"不支持的邮件用途：{purpose}")
    if not email_allowed(db, user.email, ip, settings):
        return False

    now = utcnow()
    db.execute(
        update(ActionToken)
        .where(
            ActionToken.user_id == user.id,
            ActionToken.purpose == purpose,
            ActionToken.consumed_at.is_(None),
        )
        .values(consumed_at=now)
    )
    raw_token = opaque_token()
    code = make_code()
    token = ActionToken(
        user_id=user.id,
        purpose=purpose,
        token_hash=sha256(raw_token),
        code_hash=sha256(f"{user.email}:{code}"),
        expires_at=now + timedelta(minutes=settings.action_token_minutes),
    )
    log = EmailSendLog(
        recipient_fingerprint=fingerprint(user.email, settings.jwt_secret),
        ip_address=ip,
        purpose=purpose,
        sent=False,
    )
    db.add_all([token, log])
    db.flush()

    link = f"{settings.frontend_url.rstrip('/')}/reset-password?token={raw_token}"
    subject = "重置您的密码"
    body = f"验证码：{code}\n验证链接：{link}\n\n该凭据将在 {settings.action_token_minutes} 分钟后失效。"
    try:
        send_email(user.email, subject, body, settings)
        log.sent = True
    except Exception:
        # Keep the public response uniform so SMTP state cannot reveal accounts.
        logger.exception("Unable to send %s email", purpose)
    finally:
        db.commit()
    return log.sent


def consume_action_token(db: Session, raw_token: str, purpose: str) -> User | None:
    row = db.scalar(
        select(ActionToken).where(
            ActionToken.token_hash == sha256(raw_token),
            ActionToken.purpose == purpose,
            ActionToken.consumed_at.is_(None),
            ActionToken.expires_at > utcnow(),
        )
    )
    if not row:
        return None
    row.consumed_at = utcnow()
    return db.get(User, row.user_id)


def consume_action_code(db: Session, email: str, code: str, purpose: str) -> User | None:
    row = db.scalar(
        select(ActionToken).where(
            ActionToken.code_hash == sha256(f"{email}:{code}"),
            ActionToken.purpose == purpose,
            ActionToken.consumed_at.is_(None),
            ActionToken.expires_at > utcnow(),
        )
    )
    if not row:
        return None
    row.consumed_at = utcnow()
    return db.get(User, row.user_id)


def create_session(db: Session, user: User, ip: str, user_agent: str, settings: Settings):
    raw_refresh = opaque_token()
    session = AuthSession(
        id=str(uuid4()),
        user_id=user.id,
        refresh_token_hash=sha256(raw_refresh),
        ip_address=ip,
        user_agent=user_agent[:500],
        expires_at=utcnow() + timedelta(days=settings.refresh_token_days),
    )
    db.add(session)
    db.flush()
    return session, raw_refresh


def recent_failures(db: Session, email: str, ip: str, settings: Settings) -> tuple[int, int]:
    since = utcnow() - timedelta(minutes=settings.login_window_minutes)
    email_fp = fingerprint(email, settings.jwt_secret)
    email_count = db.scalar(
        select(func.count()).select_from(LoginEvent).where(
            LoginEvent.email_fingerprint == email_fp,
            LoginEvent.successful.is_(False),
            LoginEvent.reason == "invalid_credentials",
            LoginEvent.created_at >= since,
        )
    )
    ip_count = db.scalar(
        select(func.count()).select_from(LoginEvent).where(
            LoginEvent.ip_address == ip,
            LoginEvent.successful.is_(False),
            LoginEvent.reason == "invalid_credentials",
            LoginEvent.created_at >= since,
        )
    )
    return email_count, ip_count


def record_login(db: Session, email: str, ip: str, success: bool, reason: str, settings: Settings) -> None:
    db.add(
        LoginEvent(
            email_fingerprint=fingerprint(email, settings.jwt_secret),
            ip_address=ip,
            successful=success,
            reason=reason,
        )
    )
