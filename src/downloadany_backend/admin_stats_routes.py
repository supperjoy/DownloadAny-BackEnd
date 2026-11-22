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
from .models import (
    AnalyticsEvent,
    ComponentRelease,
    DesktopRelease,
    Feedback,
    MediaResolveAttempt,
    User,
)


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


def _event_type_counts(
        db: Session, start: datetime, end: datetime, names: tuple[str, ...] = _CARD_EVENT_TYPES
    ) -> dict[str, int]:
    cols = [
        func.sum(case((AnalyticsEvent.event_type == t, 1), else_=0)).label(t)
        for t in names
    ]
    row = db.execute(
        select(*cols).where(AnalyticsEvent.created_at >= start, AnalyticsEvent.created_at < end)
    ).one()
    return {t: int(getattr(row, t) or 0) for t in names}


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


def _mask_ip(ip: str) -> str:
    parts = (ip or "").split(".")
    if len(parts) == 4 and all(part.isdigit() and 0 <= int(part) <= 255 for part in parts):
        return f"{parts[0]}.{parts[1]}.*.*"
    return "*.*"


def _mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    local, _, domain = email.partition("@")
    return f"{local[0]}***@{domain}" if local else None


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


_FUNNEL_STAGES = ("parse_requested", "parse_succeeded", "download_started", "download_completed")


class AdminStage(BaseModel):
    name: str
    count: int


class AdminSlice(BaseModel):
    name: str
    count: int


class AdminFailureRow(BaseModel):
    error: str
    count: int
    last_seen: datetime


class AdminDownloads(BaseModel):
    days: int
    funnel: list[AdminStage]
    sites: list[AdminSlice]
    formats: list[AdminSlice]
    failures: list[AdminFailureRow]


def _bucket(db: Session, col, start: datetime, end: datetime, fallback: str) -> list[AdminSlice]:
    name = func.coalesce(col, fallback)
    cnt = func.count().label("c")
    rows = db.execute(
        select(name.label("n"), cnt)
        .where(
            AnalyticsEvent.event_type == "download_completed",
            AnalyticsEvent.created_at >= start,
            AnalyticsEvent.created_at < end,
        )
        .group_by(name)
        .order_by(cnt.desc(), "n")
    ).all()
    return [AdminSlice(name=str(r.n), count=int(r.c)) for r in rows]


@router.get("/downloads", response_model=AdminDownloads)
def admin_downloads(days: int = 30, db: Session = Depends(get_db)):
    days = _clamp_days(days)
    period_end = _period_end()
    start = period_end - timedelta(days=days)

    counts = _event_type_counts(db, start, period_end, _FUNNEL_STAGES)
    failure_label = func.coalesce(func.nullif(AnalyticsEvent.error, ""), "未分类")
    fcnt = func.count().label("c")
    failure_rows = db.execute(
        select(failure_label.label("e"), fcnt, func.max(AnalyticsEvent.created_at).label("last_seen"))
        .where(
            AnalyticsEvent.event_type == "download_failed",
            AnalyticsEvent.created_at >= start,
            AnalyticsEvent.created_at < period_end,
        )
        .group_by(failure_label)
        .order_by(fcnt.desc(), "e")
        .limit(10)
    ).all()

    return AdminDownloads(
        days=days,
        funnel=[AdminStage(name=n, count=counts[n]) for n in _FUNNEL_STAGES],
        sites=_bucket(db, AnalyticsEvent.media_site, start, period_end, "unknown"),
        formats=_bucket(db, AnalyticsEvent.format_label, start, period_end, "unknown"),
        failures=[
            AdminFailureRow(error=str(r.e), count=int(r.c), last_seen=r.last_seen) for r in failure_rows
        ],
    )


class AdminTopMediaRow(BaseModel):
    title: str
    site: str | None
    format_label: str | None
    completed_count: int
    last_seen: datetime


class AdminTopMedia(BaseModel):
    days: int
    items: list[AdminTopMediaRow]


def _latest_nonnull_per_title(
    db: Session, base: tuple, col, titles: list[str]
) -> dict[str, str]:
    """每 title 在 base 过滤下、col 非空行中最新一条的 col 值（两段式，无窗口函数）。"""
    if not titles:
        return {}
    inner = (
        select(func.max(AnalyticsEvent.id).label("mid"))
        .where(*base, col.is_not(None), col != "", AnalyticsEvent.media_title.in_(titles))
        .group_by(AnalyticsEvent.media_title)
        .subquery()
    )
    rows = db.execute(
        select(AnalyticsEvent.media_title, col).where(AnalyticsEvent.id.in_(select(inner.c.mid)))
    ).all()
    return {str(t): str(v) for t, v in rows}


@router.get("/top-media", response_model=AdminTopMedia)
def admin_top_media(days: int = 30, limit: int = 20, db: Session = Depends(get_db)):
    """热门视频榜（§7）：按 media_title 分组；排除空标题与 `任务 #N`/`Task #N` 占位标题。

    两段式（不用窗口函数，SQLite/MySQL 语义一致）：先分组计数取 Top n，
    再用 `max(id)` per title 回查最新一行的 site/format_label。
    """
    days = _clamp_days(days)
    limit = min(max(limit, 1), 100)
    period_end = _period_end()
    start = period_end - timedelta(days=days)

    title = AnalyticsEvent.media_title
    base = (
        AnalyticsEvent.event_type == "download_completed",
        AnalyticsEvent.created_at >= start,
        AnalyticsEvent.created_at < period_end,
        title.is_not(None),
        title != "",
        title.not_like("任务 #%"),
        title.not_like("Task #%"),
    )
    cnt = func.count().label("c")
    last_seen = func.max(AnalyticsEvent.created_at).label("last_seen")
    stage1 = db.execute(
        select(title.label("title"), cnt, last_seen)
        .where(*base)
        .group_by(title)
        .order_by(cnt.desc(), last_seen.desc(), title)
        .limit(limit)
    ).all()

    titles = [r.title for r in stage1]
    # spec §7：site/format_label 各列独立取"组内最近一条非空值"（终审 I-1，
    # 桌面端 completed 事件 format_label 可缺，不能塌成 NULL）
    site_by_title = _latest_nonnull_per_title(db, base, AnalyticsEvent.media_site, titles)
    format_by_title = _latest_nonnull_per_title(db, base, AnalyticsEvent.format_label, titles)

    items = [
        AdminTopMediaRow(
            title=r.title,
            site=site_by_title.get(r.title),
            format_label=format_by_title.get(r.title),
            completed_count=int(r.c),
            last_seen=r.last_seen,
        )
        for r in stage1
    ]
    return AdminTopMedia(days=days, items=items)


class AdminEventRow(BaseModel):
    id: int
    occurred_at: datetime
    event_type: str
    site: str | None
    title: str | None
    format_label: str | None
    platform: str
    app_version: str
    masked_ip: str
    email_masked: str | None
    error: str | None


class AdminEventsPage(BaseModel):
    total: int
    offset: int
    limit: int
    items: list[AdminEventRow]


def _escape_like(q: str) -> str:
    # 顺序要紧：先转义转义符本身，再转义 % 与 _
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@router.get("/events", response_model=AdminEventsPage)
def admin_events(
    days: int = 30,
    event_type: str | None = None,
    site: str | None = None,
    q: str | None = None,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """明细检索：q 对 media_title 字面 LIKE（Review Focus 4），IP/邮箱脱敏。"""
    days = _clamp_days(days)
    offset = max(offset, 0)
    limit = min(max(limit, 1), 200)
    period_end = _period_end()
    start = period_end - timedelta(days=days)

    conds = [AnalyticsEvent.created_at >= start, AnalyticsEvent.created_at < period_end]
    if event_type:
        conds.append(AnalyticsEvent.event_type == event_type)
    if site:
        conds.append(AnalyticsEvent.media_site == site.lower())
    if q and q.strip():
        conds.append(AnalyticsEvent.media_title.like(f"%{_escape_like(q)}%", escape="\\"))

    total = int(db.scalar(select(func.count()).select_from(AnalyticsEvent).where(*conds)) or 0)
    rows = db.execute(
        select(AnalyticsEvent, User.email)
        .outerjoin(User, AnalyticsEvent.user_id == User.id)
        .where(*conds)
        .order_by(AnalyticsEvent.created_at.desc(), AnalyticsEvent.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()

    items = []
    for event, email in rows:
        error = event.error
        items.append(
            AdminEventRow(
                id=event.id,
                occurred_at=event.created_at,
                event_type=event.event_type,
                site=event.media_site,
                title=event.media_title,
                format_label=event.format_label,
                platform=event.platform,
                app_version=event.app_version,
                masked_ip=_mask_ip(event.ip_address),
                email_masked=_mask_email(email),
                error=(error[:200] + ("…" if len(error) > 200 else "")) if error else None,
            )
        )
    return AdminEventsPage(total=total, offset=offset, limit=limit, items=items)


class AdminFeedbackRow(BaseModel):
    kind: str
    text: str
    app_version: str
    platform: str
    created_at: datetime


class AdminReleaseRow(BaseModel):
    kind: str  # "desktop" | "component"
    name: str
    channel: str
    version: str
    published_at: datetime


class AdminSystem(BaseModel):
    days: int
    platforms: list[AdminSlice]
    versions: list[AdminSlice]
    feedback_count: int
    feedback_recent: list[AdminFeedbackRow]
    releases: list[AdminReleaseRow]


def _slices(counter: dict[str, int]) -> list[AdminSlice]:
    return [AdminSlice(name=k, count=v) for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))]


def _latest_events_per_device(db: Session, start: datetime, end: datetime) -> list[AnalyticsEvent]:
    """窗口内每 device_id 最新一条事件（max(id)，SQLite/MySQL 语义一致）。"""
    inner = (
        select(func.max(AnalyticsEvent.id).label("mid"))
        .where(AnalyticsEvent.created_at >= start, AnalyticsEvent.created_at < end)
        .group_by(AnalyticsEvent.device_id)
        .subquery()
    )
    # 终审 I-3a：只投影分布所需两列，避免每设备实例化完整 ORM 行
    return [
        (platform or "unknown", app_version or "unknown")
        for platform, app_version in db.execute(
            select(AnalyticsEvent.platform, AnalyticsEvent.app_version).where(
                AnalyticsEvent.id.in_(select(inner.c.mid))
            )
        ).all()
    ]


@router.get("/system", response_model=AdminSystem)
def admin_system(days: int = 30, db: Session = Depends(get_db)):
    days = _clamp_days(days)
    period_end = _period_end()
    start = period_end - timedelta(days=days)

    platforms: dict[str, int] = {}
    versions: dict[str, int] = {}
    for platform, app_version in _latest_events_per_device(db, start, period_end):
        platforms[platform] = platforms.get(platform, 0) + 1
        versions[app_version] = versions.get(app_version, 0) + 1

    feedback_count = int(
        db.scalar(
            select(func.count()).select_from(Feedback).where(
                Feedback.created_at >= start, Feedback.created_at < period_end
            )
        )
        or 0
    )
    recent = db.execute(
        select(Feedback)
        .where(Feedback.created_at >= start, Feedback.created_at < period_end)
        .order_by(Feedback.created_at.desc(), Feedback.id.desc())
        .limit(10)
    ).scalars().all()

    releases: list[AdminReleaseRow] = []
    latest_desktop: dict[tuple[str, str, str], DesktopRelease] = {}
    for r in db.execute(
        select(DesktopRelease).where(DesktopRelease.active.is_(True)).order_by(
            DesktopRelease.published_at.asc()
        )
    ).scalars():
        latest_desktop[(r.target, r.arch, r.channel)] = r
    for (target, arch, channel), r in latest_desktop.items():
        releases.append(AdminReleaseRow(kind="desktop", name=f"{target}/{arch}",
                                        channel=channel, version=r.version, published_at=r.published_at))
    latest_component: dict[tuple[str, str], ComponentRelease] = {}
    for r in db.execute(
        select(ComponentRelease).where(ComponentRelease.active.is_(True)).order_by(
            ComponentRelease.published_at.asc()
        )
    ).scalars():
        latest_component[(r.component, r.channel)] = r
    for (component, channel), r in latest_component.items():
        releases.append(AdminReleaseRow(kind="component", name=component,
                                        channel=channel, version=r.version, published_at=r.published_at))

    return AdminSystem(
        days=days,
        platforms=_slices(platforms),
        versions=_slices(versions),
        feedback_count=feedback_count,
        feedback_recent=[
            AdminFeedbackRow(
                kind=f.kind,
                text=f.text[:300] + ("…" if len(f.text) > 300 else ""),
                app_version=f.app_version,
                platform=f.platform,
                created_at=f.created_at,
            )
            for f in recent
        ],
        releases=releases,
    )
