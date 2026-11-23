import asyncio
from datetime import datetime, timedelta
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .database import get_db
from .dependencies import client_ip, current_user, optional_current_user
from .media_parsing.common import (
    MediaParseError,
    extract_source_url,
    validate_public_media_url,
)
from .media_parsing.providers import is_online_parse_supported
from .media_parsing.service import parse_media, resolve_direct_video
from .models import MediaResolveAttempt, User, utcnow


router = APIRouter(prefix="/api/media", tags=["media"])
parse_slots = asyncio.Semaphore(4)


def beijing_day_start(now: datetime) -> datetime:
    """now（naive UTC）所在北京时间自然日的零点，仍以 naive UTC 返回。"""
    return datetime.combine((now + timedelta(hours=8)).date(), datetime.min.time()) - timedelta(hours=8)


class ParseRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)

    @field_validator("url")
    @classmethod
    def extract_shared_url(cls, value: str) -> str:
        try:
            return extract_source_url(value)
        except MediaParseError as error:
            raise ValueError(str(error)) from error


class FormatOption(BaseModel):
    id: str
    label: str
    height: int | None = None
    ext: str | None = None
    filesize: int | None = None


class ImageOption(BaseModel):
    id: str
    label: str
    url: str
    ext: str
    width: int | None = None
    height: int | None = None


class VideoInfo(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    title: str
    duration: float | None = None
    thumbnail: str | None = None
    uploader: str | None = None
    webpage_url: str | None = None
    images: list[ImageOption] = Field(default_factory=list)
    formats: list[FormatOption]


class DirectVideoInfo(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    platform: str
    id: str
    title: str
    video_url: str | None = None
    resource_url: str | None = None
    webpage_url: str
    thumbnail: str | None = None
    uploader: str | None = None
    duration: float | None = None
    quality: str | None = None
    ext: str | None = None
    filesize: int | None = None
    expires_at: str | None = None
    audio_url: str | None = None
    audio_formats: list["DirectAudioFormatInfo"] = Field(default_factory=list)
    images: list["DirectImageInfo"] = Field(default_factory=list)
    formats: list["DirectFormatInfo"] = Field(default_factory=list)


class DirectFormatInfo(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    label: str
    url: str
    ext: str | None = None
    width: int | None = None
    height: int | None = None
    quality: int | None = None
    fps: float | None = None
    filesize: int | None = None
    vcodec: str | None = None
    acodec: str | None = None
    has_audio: bool
    expires_at: str | None = None


class DirectAudioFormatInfo(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    label: str
    url: str
    ext: str | None = None
    abr: float | None = None
    filesize: int | None = None
    acodec: str | None = None
    language: str | None = None
    expires_at: str | None = None


class DirectImageInfo(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    label: str
    url: str
    ext: str
    width: int | None = None
    height: int | None = None
    filesize: int | None = None
    expires_at: str | None = None


@router.post("/parse", response_model=VideoInfo, response_model_by_alias=True)
async def parse_video(
    body: ParseRequest,
    settings: Settings = Depends(get_settings),
    _user: User = Depends(current_user),
):
    try:
        async with parse_slots:
            result = await asyncio.wait_for(
                asyncio.to_thread(parse_media, body.url, settings),
                timeout=settings.ytdlp_parse_timeout,
            )
        return VideoInfo.model_validate(result)
    except MediaParseError as error:
        raise HTTPException(status_code=422, detail=str(error))
    except TimeoutError:
        raise HTTPException(status_code=504, detail="解析超时，请稍后重试")


@router.post("/resolve", response_model=DirectVideoInfo, response_model_by_alias=True)
async def resolve_video(
    body: ParseRequest,
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
    user: User | None = Depends(optional_current_user),
    ip: str = Depends(client_ip),
):
    # 先判断官网能力，桌面端引导不占用解析额度或解析并发槽。
    # 此处只做本地 URL 校验；实际解析仍执行完整的公网地址校验。
    try:
        url = validate_public_media_url(body.url, resolve_hostname=False)
    except MediaParseError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if not is_online_parse_supported(urlparse(url).hostname or ""):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "client_only",
                "message": "该平台暂不支持官网在线解析，请使用桌面客户端",
            },
        )

    now = utcnow()
    rate_since = now - timedelta(seconds=settings.parse_rate_window_seconds)
    identity_filter = (
        MediaResolveAttempt.user_id == user.id
        if user
        else (
            MediaResolveAttempt.user_id.is_(None)
            & (MediaResolveAttempt.ip_address == ip)
        )
    )
    recent_attempts = db.scalar(
        select(func.count()).select_from(MediaResolveAttempt).where(
            identity_filter,
            MediaResolveAttempt.created_at >= rate_since,
        )
    ) or 0
    if recent_attempts >= settings.parse_rate_limit:
        retry_after = max(1, settings.parse_rate_window_seconds)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"请求过于频繁，请在 {retry_after} 秒后重试",
            headers={"Retry-After": str(retry_after)},
        )

    if not user:
        # 最近仍在执行的请求也占用额度，防止并发绕过；超时遗留占位会自动失效。
        pending_since = now - timedelta(seconds=settings.ytdlp_parse_timeout + 30)
        used = db.scalar(
            select(func.count()).select_from(MediaResolveAttempt).where(
                MediaResolveAttempt.user_id.is_(None),
                MediaResolveAttempt.ip_address == ip,
                MediaResolveAttempt.created_at >= beijing_day_start(now),
                or_(
                    MediaResolveAttempt.successful.is_(True),
                    (
                        MediaResolveAttempt.successful.is_(None)
                        & (MediaResolveAttempt.created_at >= pending_since)
                    ),
                ),
            )
        ) or 0
        if used >= settings.anonymous_daily_parse_limit:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "code": "anonymous_daily_limit",
                    "message": f"今日免费解析次数已用完（共 {settings.anonymous_daily_parse_limit} 次），登录后可继续解析",
                },
            )

    attempt = MediaResolveAttempt(user_id=user.id if user else None, ip_address=ip)
    db.add(attempt)
    db.commit()
    try:
        async with parse_slots:
            result = await asyncio.wait_for(
                asyncio.to_thread(resolve_direct_video, body.url, settings),
                timeout=settings.ytdlp_parse_timeout,
            )
        attempt.successful = True
        db.commit()
        payload = DirectVideoInfo.model_validate(result)
        if payload.resource_url:
            payload.video_url = None
        return payload
    except MediaParseError as error:
        attempt.successful = False
        db.commit()
        raise HTTPException(status_code=422, detail=str(error)) from error
    except TimeoutError as error:
        attempt.successful = False
        db.commit()
        raise HTTPException(status_code=504, detail="解析超时，请稍后重试") from error
    except Exception:
        db.execute(
            update(MediaResolveAttempt)
            .where(MediaResolveAttempt.id == attempt.id)
            .values(successful=False)
        )
        db.commit()
        raise
