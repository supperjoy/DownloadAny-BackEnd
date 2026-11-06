from typing import Literal
from urllib.parse import urlparse

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import get_db
from .dependencies import client_ip, optional_current_user
from .models import AnalyticsEvent, User


router = APIRouter(prefix="/api/analytics", tags=["analytics"])

EventType = Literal[
    "app_open",
    "parse_requested",
    "parse_succeeded",
    "parse_failed",
    "download_started",
    "download_completed",
    "download_failed",
    "download_cancelled",
]


class AnalyticsEventRequest(BaseModel):
    event_id: str = Field(min_length=36, max_length=36)
    event_type: EventType
    device_id: str = Field(min_length=36, max_length=36)
    client_session_id: str = Field(min_length=36, max_length=36)
    app_version: str = Field(default="", max_length=32)
    platform: str = Field(default="", max_length=32)
    media_id: str | None = Field(default=None, max_length=255)
    media_title: str | None = Field(default=None, max_length=500)
    media_url: str | None = Field(default=None, max_length=4096)
    format_id: str | None = Field(default=None, max_length=100)
    format_label: str | None = Field(default=None, max_length=100)
    error: str | None = Field(default=None, max_length=2000)

    @field_validator("event_id", "device_id", "client_session_id")
    @classmethod
    def validate_uuid(cls, value: str) -> str:
        from uuid import UUID

        return str(UUID(value))

    @field_validator("media_url")
    @classmethod
    def validate_media_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("media_url 必须是有效的 http(s) 地址")
        return value


class AnalyticsAccepted(BaseModel):
    accepted: bool = True


@router.post("/events", response_model=AnalyticsAccepted, status_code=202)
def record_event(
    body: AnalyticsEventRequest,
    db: Session = Depends(get_db),
    user: User | None = Depends(optional_current_user),
    ip: str = Depends(client_ip),
):
    hostname = urlparse(body.media_url).hostname if body.media_url else None
    row = AnalyticsEvent(
        event_id=body.event_id,
        event_type=body.event_type,
        device_id=body.device_id,
        client_session_id=body.client_session_id,
        user_id=user.id if user else None,
        app_version=body.app_version,
        platform=body.platform,
        media_site=hostname.lower()[:100] if hostname else None,
        media_id=body.media_id,
        media_title=body.media_title,
        media_url=body.media_url,
        format_id=body.format_id,
        format_label=body.format_label,
        error=body.error,
        ip_address=ip,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # event_id 唯一，网络重试不会重复计数。
        db.rollback()
    return AnalyticsAccepted()
