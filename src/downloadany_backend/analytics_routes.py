from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from urllib.parse import urlparse
from collections import OrderedDict, deque
from threading import Lock
import time as monotonic_time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import case, distinct, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .database import get_db
from .dependencies import client_ip, optional_current_user
from .models import AnalyticsEvent, User


router = APIRouter(prefix="/api/analytics", tags=["analytics"])

# 官网事件在写库前限流；限制缓存键数量，避免随机来源使内存持续增长。
WEB_EVENT_LIMIT = 120
WEB_EVENT_WINDOW = 60
_web_hits: OrderedDict[str, deque[float]] = OrderedDict()
_web_lock = Lock()


def _check_web_event_rate(ip: str) -> None:
    now = monotonic_time.monotonic()
    with _web_lock:
        hits = _web_hits.setdefault(ip, deque())
        _web_hits.move_to_end(ip)
        while hits and now - hits[0] >= WEB_EVENT_WINDOW:
            hits.popleft()
        if len(hits) >= WEB_EVENT_LIMIT:
            raise HTTPException(status_code=429, detail="事件上报过于频繁", headers={"Retry-After": "60"})
        hits.append(now)
        if len(_web_hits) > 10000:
            _web_hits.popitem(last=False)

EventType = Literal[
    "app_open",
    "parse_requested",
    "parse_succeeded",
    "parse_failed",
    "download_started",
    "download_completed",
    "download_failed",
    "download_cancelled",
    "page_view",
    "parse_client_only",
    "parse_blocked",
    "parse_cancelled",
    "download_clicked",
    "desktop_guide_viewed",
    "desktop_guide_clicked",
    "desktop_download_clicked",
    "login_succeeded",
    "signup_succeeded",
    "feedback_submitted",
    "screen_view",
    "dialog_opened",
    "setting_changed",
    "download_cancel_requested",
    "download_enqueue_failed",
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


class AnalyticsMetric(BaseModel):
    value: int | float
    change: float | None = None


class AnalyticsDailyPoint(BaseModel):
    date: date
    active_devices: int
    app_opens: int
    parse_requests: int
    downloads_completed: int


class AnalyticsRecentEvent(BaseModel):
    event_type: EventType
    platform: str
    media_site: str | None
    occurred_at: datetime


class AnalyticsOverview(BaseModel):
    days: int
    active_devices: AnalyticsMetric
    parse_requests: AnalyticsMetric
    downloads_completed: AnalyticsMetric
    download_success_rate: AnalyticsMetric
    daily: list[AnalyticsDailyPoint]
    recent_events: list[AnalyticsRecentEvent]


def _percentage_change(current: int | float, previous: int | float) -> float | None:
    if previous == 0:
        return None if current == 0 else 100.0
    return round((current - previous) / previous * 100, 1)


def _period_metrics(db: Session, start: datetime, end: datetime) -> dict[str, int | float]:
    row = db.execute(
        select(
            func.count(distinct(AnalyticsEvent.device_id)).label("active_devices"),
            func.sum(case((AnalyticsEvent.event_type == "parse_requested", 1), else_=0)).label("parse_requests"),
            func.sum(case((AnalyticsEvent.event_type == "download_started", 1), else_=0)).label("downloads_started"),
            func.sum(case((AnalyticsEvent.event_type == "download_completed", 1), else_=0)).label("downloads_completed"),
        ).where(AnalyticsEvent.created_at >= start, AnalyticsEvent.created_at < end)
    ).one()
    started = int(row.downloads_started or 0)
    completed = int(row.downloads_completed or 0)
    return {
        "active_devices": int(row.active_devices or 0),
        "parse_requests": int(row.parse_requests or 0),
        "downloads_completed": completed,
        "download_success_rate": round(completed / started * 100, 1) if started else 0.0,
    }


@router.post("/events", response_model=AnalyticsAccepted, status_code=202)
def record_event(
    body: AnalyticsEventRequest,
    db: Session = Depends(get_db),
    user: User | None = Depends(optional_current_user),
    ip: str = Depends(client_ip),
):
    if body.platform == "web":
        _check_web_event_rate(ip)
        # 服务端也保证官网事件只存平台域名，不保留路径、签名和媒体标题。
        if body.media_url:
            parsed = urlparse(body.media_url)
            body.media_url = f"{parsed.scheme}://{parsed.hostname}/"
        body.media_title = None
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


@router.get("/overview", response_model=AnalyticsOverview)
def analytics_overview(days: int = 7, db: Session = Depends(get_db)):
    """返回后台仪表盘所需的匿名聚合统计，不暴露 IP、URL 或用户标识。"""
    days = min(max(days, 1), 90)
    today = datetime.now(UTC).date()
    period_end = datetime.combine(today + timedelta(days=1), time.min)
    period_start = period_end - timedelta(days=days)
    previous_start = period_start - timedelta(days=days)
    current = _period_metrics(db, period_start, period_end)
    previous = _period_metrics(db, previous_start, period_start)

    daily_rows = db.execute(
        select(
            func.date(AnalyticsEvent.created_at).label("day"),
            func.count(distinct(AnalyticsEvent.device_id)).label("active_devices"),
            func.sum(case((AnalyticsEvent.event_type == "app_open", 1), else_=0)).label("app_opens"),
            func.sum(case((AnalyticsEvent.event_type == "parse_requested", 1), else_=0)).label("parse_requests"),
            func.sum(case((AnalyticsEvent.event_type == "download_completed", 1), else_=0)).label("downloads_completed"),
        )
        .where(AnalyticsEvent.created_at >= period_start, AnalyticsEvent.created_at < period_end)
        .group_by(func.date(AnalyticsEvent.created_at))
        .order_by(func.date(AnalyticsEvent.created_at))
    ).all()
    by_day = {str(row.day): row for row in daily_rows}
    daily = []
    for offset in range(days):
        day = period_start.date() + timedelta(days=offset)
        row = by_day.get(str(day))
        daily.append(AnalyticsDailyPoint(
            date=day,
            active_devices=int(row.active_devices or 0) if row else 0,
            app_opens=int(row.app_opens or 0) if row else 0,
            parse_requests=int(row.parse_requests or 0) if row else 0,
            downloads_completed=int(row.downloads_completed or 0) if row else 0,
        ))

    recent_rows = db.scalars(
        select(AnalyticsEvent)
        .where(AnalyticsEvent.created_at >= period_start)
        .order_by(AnalyticsEvent.created_at.desc())
        .limit(8)
    ).all()
    metric = lambda key: AnalyticsMetric(
        value=current[key], change=_percentage_change(current[key], previous[key])
    )
    return AnalyticsOverview(
        days=days,
        active_devices=metric("active_devices"),
        parse_requests=metric("parse_requests"),
        downloads_completed=metric("downloads_completed"),
        download_success_rate=metric("download_success_rate"),
        daily=daily,
        recent_events=[AnalyticsRecentEvent(
            event_type=row.event_type,
            platform=row.platform or "unknown",
            media_site=row.media_site,
            occurred_at=row.created_at,
        ) for row in recent_rows],
    )
