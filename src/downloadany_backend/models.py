from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    google_subject: Mapped[str | None] = mapped_column(String(255), unique=True, index=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    sessions: Mapped[list[AuthSession]] = relationship(back_populates="user")


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    refresh_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    ip_address: Mapped[str] = mapped_column(String(45))
    user_agent: Mapped[str] = mapped_column(String(500), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    user: Mapped[User] = relationship(back_populates="sessions")


class ActionToken(Base):
    __tablename__ = "action_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(32), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    code_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LoginEvent(Base):
    __tablename__ = "login_events"
    __table_args__ = (
        Index("ix_login_event_email_time", "email_fingerprint", "created_at"),
        Index("ix_login_event_ip_time", "ip_address", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email_fingerprint: Mapped[str] = mapped_column(String(64))
    ip_address: Mapped[str] = mapped_column(String(45))
    successful: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(64), default="invalid_credentials")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CaptchaChallenge(Base):
    __tablename__ = "captcha_challenges"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    answer_hash: Mapped[str] = mapped_column(String(64))
    ip_address: Mapped[str] = mapped_column(String(45), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class EmailSendLog(Base):
    __tablename__ = "email_send_logs"
    __table_args__ = (
        Index("ix_email_log_recipient_time", "recipient_fingerprint", "created_at"),
        Index("ix_email_log_ip_time", "ip_address", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    recipient_fingerprint: Mapped[str] = mapped_column(String(64))
    ip_address: Mapped[str] = mapped_column(String(45))
    purpose: Mapped[str] = mapped_column(String(32))
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    event: Mapped[str] = mapped_column(String(64), index=True)
    ip_address: Mapped[str] = mapped_column(String(45))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AnalyticsEvent(Base):
    """桌面端产品行为事件，供日活、解析和下载统计使用。"""

    __tablename__ = "analytics_events"
    __table_args__ = (
        Index("ix_analytics_event_type_time", "event_type", "created_at"),
        Index("ix_analytics_device_time", "device_id", "created_at"),
        Index("ix_analytics_user_time", "user_id", "created_at"),
        Index("ix_analytics_site_time", "media_site", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    event_id: Mapped[str] = mapped_column(String(36), unique=True)
    device_id: Mapped[str] = mapped_column(String(36))
    client_session_id: Mapped[str] = mapped_column(String(36))
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    app_version: Mapped[str] = mapped_column(String(32), default="")
    platform: Mapped[str] = mapped_column(String(32), default="")
    media_site: Mapped[str | None] = mapped_column(String(100))
    media_id: Mapped[str | None] = mapped_column(String(255))
    media_title: Mapped[str | None] = mapped_column(String(500))
    media_url: Mapped[str | None] = mapped_column(Text)
    format_id: Mapped[str | None] = mapped_column(String(100))
    format_label: Mapped[str | None] = mapped_column(String(100))
    error: Mapped[str | None] = mapped_column(Text)
    ip_address: Mapped[str] = mapped_column(String(45))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DesktopRelease(Base):
    """A signed Tauri desktop release published to one update channel."""

    __tablename__ = "desktop_releases"
    __table_args__ = (
        Index("ix_desktop_release_lookup", "target", "arch", "channel", "active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[str] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(16), default="stable")
    target: Mapped[str] = mapped_column(String(64))
    arch: Mapped[str] = mapped_column(String(32))
    artifact_path: Mapped[str] = mapped_column(Text)
    signature: Mapped[str] = mapped_column(Text)
    notes: Mapped[str] = mapped_column(Text, default="")
    rollout_percent: Mapped[int] = mapped_column(Integer, default=100)
    minimum_version: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    published_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ComponentRelease(Base):
    """Independently signed binary component, initially used for yt-dlp."""

    __tablename__ = "component_releases"
    __table_args__ = (
        Index("ix_component_release_lookup", "component", "target", "channel", "active"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    component: Mapped[str] = mapped_column(String(32))
    version: Mapped[str] = mapped_column(String(64))
    channel: Mapped[str] = mapped_column(String(16), default="stable")
    target: Mapped[str] = mapped_column(String(64))
    artifact_path: Mapped[str] = mapped_column(Text)
    signature: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    rollout_percent: Mapped[int] = mapped_column(Integer, default=100)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    published_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
