from __future__ import annotations

import time
from collections import defaultdict, deque

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from .database import get_db
from .models import Feedback

router = APIRouter(prefix="/api", tags=["feedback"])

MAX_LENGTH = 300
# 内存级滑动窗口限流：按设备 60 秒 5 条；重启即清零，够用且无外部依赖
RATE_MAX = 5
RATE_WINDOW_SECONDS = 60.0

_rate_hits: dict[str, deque[float]] = defaultdict(deque)


class FeedbackIn(BaseModel):
    kind: str
    text: str
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


@router.post("/feedback", status_code=status.HTTP_201_CREATED)
async def submit_feedback(
    payload: FeedbackIn,
    device_id: str = Header(default="anonymous", alias="X-DownloadAny-Device-Id"),
    db: Session = Depends(get_db),
):
    hits = _rate_hits[device_id]
    now = time.monotonic()
    while hits and now - hits[0] > RATE_WINDOW_SECONDS:
        hits.popleft()
    if len(hits) >= RATE_MAX:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="反馈过于频繁，请稍后再试"
        )
    hits.append(now)

    feedback = Feedback(device_id=device_id[:64], **payload.model_dump())
    db.add(feedback)
    db.commit()
    db.refresh(feedback)
    return {"id": feedback.id, "created_at": feedback.created_at.isoformat()}
