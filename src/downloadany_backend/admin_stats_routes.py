"""后台管理统计接口：/api/analytics/admin/* 全部只读、统一管理令牌保护。

设计文档 docs/superpowers/specs/2026-11-22-admin-dashboard-design.md：
§5.2 端点形状、§7 指标口径、§5.3 脱敏。窗口算法与 analytics_routes.overview 同源
（naive UTC 存储，锚定"今 UTC 日+1 天 00:00"），dau/wau/mau 为固定 1/7/30 天窗口。
"""

from datetime import UTC, date, datetime, time, timedelta

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import and_, case, distinct, func, select
from sqlalchemy.orm import Session

from .analytics_routes import AnalyticsMetric, _percentage_change
from .database import get_db
from .dependencies import require_admin
from .models import AnalyticsEvent, MediaResolveAttempt, User


router = APIRouter(
    prefix="/api/analytics/admin",
    tags=["admin-stats"],
    dependencies=[Depends(require_admin)],
)

_CARD_EVENT_TYPES = (
    "parse_requested",
    "download_started",
    "download_completed",
    "download_failed",
    "download_cancelled",
)


def _clamp_days(days: int) -> int:
    return min(max(days, 1), 366)


def _period_end() -> datetime:
    return datetime.combine(datetime.now(UTC).date() + timedelta(days=1), time.min)


def _event_type_counts(db: Session, start: datetime, end: datetime) -> dict[str, int]:
    cols = [
        func.sum(case((AnalyticsEvent.event_type == t, 1), else_=0)).label(t)
        for t in _CARD_EVENT_TYPES
    ]
    row = db.execute(
        select(*cols).where(AnalyticsEvent.created_at >= start, AnalyticsEvent.created_at < end)
    ).one()
    return {t: int(getattr(row, t) or 0) for t in _CARD_EVENT_TYPES}


def _active(db: Session, start: datetime, end: datetime, user_only: bool = False) -> int:
    col = AnalyticsEvent.user_id if user_only else AnalyticsEvent.device_id
    stmt = select(func.count(distinct(col))).where(
        AnalyticsEvent.created_at >= start, AnalyticsEvent.created_at < end
    )
    if user_only:
        stmt = stmt.where(col.is_not(None))
    return int(db.scalar(stmt) or 0)


def _user_count(db: Session, start: datetime, end: datetime) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(User.created_at >= start, User.created_at < end)
        )
        or 0
    )


def _resolve_count(db: Session, start: datetime, end: datetime, only_ok: bool = False) -> int:
    stmt = select(func.count()).select_from(MediaResolveAttempt).where(
        MediaResolveAttempt.created_at >= start, MediaResolveAttempt.created_at < end
    )
    if only_ok:
        stmt = stmt.where(MediaResolveAttempt.successful.is_(True))
    return int(db.scalar(stmt) or 0)


class AdminOverview(BaseModel):
    days: int
    total_users: int
    new_users: AnalyticsMetric
    dau_devices: int
    wau_devices: int
    mau_devices: int
    dau_users: int
    wau_users: int
    mau_users: int
    downloads_started: AnalyticsMetric
    downloads_completed: AnalyticsMetric
    downloads_failed: AnalyticsMetric
    downloads_cancelled: AnalyticsMetric
    download_success_rate: AnalyticsMetric
    parse_requests: AnalyticsMetric
    website_resolves: AnalyticsMetric
    website_resolves_ok: AnalyticsMetric


@router.get("/overview", response_model=AdminOverview)
def admin_overview(days: int = 30, db: Session = Depends(get_db)):
    days = _clamp_days(days)
    period_end = _period_end()
    start = period_end - timedelta(days=days)
    prev_start = start - timedelta(days=days)

    cur = _event_type_counts(db, start, period_end)
    prev = _event_type_counts(db, prev_start, start)

    def metric(key: str) -> AnalyticsMetric:
        return AnalyticsMetric(value=cur[key], change=_percentage_change(cur[key], prev[key]))

    def rate(counts: dict[str, int]) -> float:
        return round(counts["download_completed"] / counts["download_started"] * 100, 1) if counts["download_started"] else 0.0

    new_cur, new_prev = _user_count(db, start, period_end), _user_count(db, prev_start, start)

    def resolve_metric(only_ok: bool) -> AnalyticsMetric:
        c = _resolve_count(db, start, period_end, only_ok)
        p = _resolve_count(db, prev_start, start, only_ok)
        return AnalyticsMetric(value=c, change=_percentage_change(c, p))

    active_users = lambda days_: _active(db, period_end - timedelta(days=days_), period_end, user_only=True)
    active_devices = lambda days_: _active(db, period_end - timedelta(days=days_), period_end)

    return AdminOverview(
        days=days,
        total_users=int(db.scalar(select(func.count()).select_from(User)) or 0),
        new_users=AnalyticsMetric(value=new_cur, change=_percentage_change(new_cur, new_prev)),
        dau_devices=active_devices(1),
        wau_devices=active_devices(7),
        mau_devices=active_devices(30),
        dau_users=active_users(1),
        wau_users=active_users(7),
        mau_users=active_users(30),
        downloads_started=metric("download_started"),
        downloads_completed=metric("download_completed"),
        downloads_failed=metric("download_failed"),
        downloads_cancelled=metric("download_cancelled"),
        download_success_rate=AnalyticsMetric(
            value=rate(cur), change=_percentage_change(rate(cur), rate(prev))
        ),
        parse_requests=metric("parse_requested"),
        website_resolves=resolve_metric(False),
        website_resolves_ok=resolve_metric(True),
    )


class AdminDailyPoint(BaseModel):
    date: date
    active_devices: int
    active_users: int
    app_opens: int
    parse_requests: int
    downloads_completed: int
    new_users: int


class AdminTimeseries(BaseModel):
    days: int
    daily: list[AdminDailyPoint]


@router.get("/timeseries", response_model=AdminTimeseries)
def admin_timeseries(days: int = 30, db: Session = Depends(get_db)):
    days = _clamp_days(days)
    period_end = _period_end()
    start = period_end - timedelta(days=days)

    day = func.date(AnalyticsEvent.created_at).label("day")
    rows = db.execute(
        select(
            day,
            func.count(distinct(AnalyticsEvent.device_id)).label("active_devices"),
            func.count(distinct(AnalyticsEvent.user_id)).label("active_users"),
            func.sum(case((AnalyticsEvent.event_type == "app_open", 1), else_=0)).label("app_opens"),
            func.sum(case((AnalyticsEvent.event_type == "parse_requested", 1), else_=0)).label("parse_requests"),
            func.sum(case((AnalyticsEvent.event_type == "download_completed", 1), else_=0)).label("downloads_completed"),
        )
        .where(AnalyticsEvent.created_at >= start, AnalyticsEvent.created_at < period_end)
        .group_by(day)
    ).all()
    by_day = {str(r.day): r for r in rows}

    user_rows = db.execute(
        select(func.date(User.created_at).label("day"), func.count().label("n"))
        .where(User.created_at >= start, User.created_at < period_end)
        .group_by("day")
    ).all()
    new_by_day = {str(r.day): int(r.n) for r in user_rows}

    daily = []
    for offset in range(days):
        d = (start + timedelta(days=offset)).date()
        r = by_day.get(str(d))
        daily.append(
            AdminDailyPoint(
                date=d,
                active_devices=int(r.active_devices or 0) if r else 0,
                active_users=int(r.active_users or 0) if r else 0,
                app_opens=int(r.app_opens or 0) if r else 0,
                parse_requests=int(r.parse_requests or 0) if r else 0,
                downloads_completed=int(r.downloads_completed or 0) if r else 0,
                new_users=new_by_day.get(str(d), 0),
            )
        )
    return AdminTimeseries(days=days, daily=daily)


class AdminChannelRow(BaseModel):
    kind: str  # "password" | "google" | "both"（§7：users 表字段组合，全表聚合）
    count: int


class AdminDayCount(BaseModel):
    date: date
    count: int


class AdminUsers(BaseModel):
    days: int
    total_users: int
    new_users: AnalyticsMetric
    channels: list[AdminChannelRow]
    new_daily: list[AdminDayCount]


@router.get("/users", response_model=AdminUsers)
def admin_users(days: int = 30, db: Session = Depends(get_db)):
    days = _clamp_days(days)
    period_end = _period_end()
    start = period_end - timedelta(days=days)
    prev_start = start - timedelta(days=days)

    has_password = User.password_hash.is_not(None)
    has_google = User.google_subject.is_not(None)
    kind = case((and_(has_password, has_google), "both"), (has_password, "password"), else_="google")
    channel_rows = db.execute(
        select(kind.label("kind"), func.count().label("n")).group_by(kind).order_by("kind")
    ).all()

    new_cur, new_prev = _user_count(db, start, period_end), _user_count(db, prev_start, start)
    user_rows = db.execute(
        select(func.date(User.created_at).label("day"), func.count().label("n"))
        .where(User.created_at >= start, User.created_at < period_end)
        .group_by("day")
    ).all()
    new_by_day = {str(r.day): int(r.n) for r in user_rows}
    new_daily = [
        AdminDayCount(
            date=(start + timedelta(days=offset)).date(),
            count=new_by_day.get(str((start + timedelta(days=offset)).date()), 0),
        )
        for offset in range(days)
    ]

    return AdminUsers(
        days=days,
        total_users=int(db.scalar(select(func.count()).select_from(User)) or 0),
        new_users=AnalyticsMetric(value=new_cur, change=_percentage_change(new_cur, new_prev)),
        channels=[AdminChannelRow(kind=r.kind, count=int(r.n)) for r in channel_rows],
        new_daily=new_daily,
    )
