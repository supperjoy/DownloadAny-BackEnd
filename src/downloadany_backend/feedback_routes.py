from __future__ import annotations

import time
from collections import defaultdict, deque
from datetime import timedelta
from threading import Lock

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, EmailStr, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .database import get_db
from .dependencies import client_ip
from .models import Feedback, utcnow

router = APIRouter(prefix="/api", tags=["feedback"])

MAX_LENGTH = 300
# 先用进程内滑动窗口挡住瞬时并发，再用数据库小时窗口覆盖重启和多进程部署。
RATE_MAX = 5
RATE_WINDOW_SECONDS = 600.0
RATE_HOURLY_MAX = 10

_rate_hits: dict[str, deque[float]] = defaultdict(deque)
_rate_lock = Lock()


class FeedbackIn(BaseModel):
    kind: str
    text: str
    email: EmailStr | None = None
    app_version: str = ""
    platform: str = ""

    @field_validator("kind")
    @classmethod
    def valid_kind(cls, value: str) -> str:
        if value not in {"bug", "feature"}:
            raise ValueError("kind 必须是 bug 或 feature")
        return value

    @field_validator("text")
    @classmethod
    def valid_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("反馈内容不能为空")
        if len(value) > MAX_LENGTH:
            raise ValueError(f"反馈内容最多 {MAX_LENGTH} 字")
        return value

    @field_validator("email", mode="before")
    @classmethod
    def empty_email_is_none(cls, value):
        return value.strip().lower() if isinstance(value, str) and value.strip() else None


@router.post("/feedback", status_code=status.HTTP_201_CREATED)
def submit_feedback(
    payload: FeedbackIn,
    device_id: str = Header(default="anonymous", alias="X-DownloadAny-Device-Id"),
    ip: str = Depends(client_ip),
    db: Session = Depends(get_db),
):
    device_id = device_id.strip()[:64] or "anonymous"
    now = time.monotonic()
    limited = False
    with _rate_lock:
        for key in (f"ip:{ip}", f"device:{device_id}"):
            hits = _rate_hits[key]
            while hits and now - hits[0] > RATE_WINDOW_SECONDS:
                hits.popleft()
            if len(hits) >= RATE_MAX:
                limited = True
        if not limited:
            _rate_hits[f"ip:{ip}"].append(now)
            _rate_hits[f"device:{device_id}"].append(now)

    since = utcnow() - timedelta(hours=1)
    hourly_count = db.scalar(
        select(func.count()).select_from(Feedback).where(
            Feedback.created_at >= since,
            or_(Feedback.ip_address == ip, Feedback.device_id == device_id),
        )
    ) or 0
    if limited or hourly_count >= RATE_HOURLY_MAX:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="反馈过于频繁，请稍后再试"
        )

    feedback = Feedback(device_id=device_id, ip_address=ip[:64], **payload.model_dump())
    db.add(feedback)
    db.commit()
    db.refresh(feedback)
    return {"id": feedback.id, "created_at": feedback.created_at.isoformat()}
