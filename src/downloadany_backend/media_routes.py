import asyncio
import re
import time
from datetime import timedelta
from urllib.parse import quote, urlparse

import requests
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .database import get_db
from .dependencies import client_ip, current_user, optional_current_user
from .media import (
    DOUYIN_DOWNLOAD_CDN_DOMAINS,
    DEFAULT_USER_AGENT,
    ClientOnlyMediaError,
    MediaParseError,
    _hostname_matches,
    _request_proxies,
    parse_media,
    resolve_direct_video,
    validate_public_media_url,
)
from .models import MediaResolveAttempt, User, utcnow


router = APIRouter(prefix="/api/media", tags=["media"])
parse_slots = asyncio.Semaphore(4)


def _download_proxy_url(url: str, filename: str) -> str:
    return f"/api/media/download?url={quote(url, safe='')}&filename={quote(filename, safe='')}"


def _proxy_douyin_result(result: dict) -> dict:
    if str(result.get("platform") or "").lower() != "douyin":
        return result
    title = str(result.get("title") or "LumeAny")
    proxied = dict(result)
    if result.get("video_url"):
        proxied["video_url"] = _download_proxy_url(
            str(result["video_url"]), f"{title}.{result.get('ext') or 'mp4'}"
        )
    if result.get("audio_url"):
        proxied["audio_url"] = _download_proxy_url(
            str(result["audio_url"]), f"{title}.mp3"
        )
    proxied["formats"] = [
        {
            **item,
            "url": _download_proxy_url(
                str(item["url"]),
                f"{title}-{item.get('label') or item.get('id')}.{item.get('ext') or 'mp4'}",
            ),
        }
        for item in result.get("formats") or []
    ]
    proxied["audio_formats"] = [
        {
            **item,
            "url": _download_proxy_url(
                str(item["url"]),
                f"{title}-{item.get('label') or item.get('id')}.{item.get('ext') or 'mp3'}",
            ),
        }
        for item in result.get("audio_formats") or []
    ]
    proxied["images"] = [
        {
            **item,
            "url": _download_proxy_url(
                str(item["url"]),
                f"{title}-{item.get('label') or item.get('id')}.{item.get('ext') or 'webp'}",
            ),
        }
        for item in result.get("images") or []
    ]
    return proxied


class ParseRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)


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
    video_url: str
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


@router.get("/download")
def download_douyin_media(
    url: str = Query(min_length=8, max_length=8192),
    filename: str = Query(default="LumeAny", min_length=1, max_length=180),
    settings: Settings = Depends(get_settings),
):
    # This endpoint accepts a browser-supplied URL, so always resolve DNS here;
    # the relaxed extracted-media validator is only safe for server-extracted URLs.
    safe_url = validate_public_media_url(url, resolve_hostname=True)
    hostname = (urlparse(safe_url).hostname or "").lower()
    if not _hostname_matches(hostname, DOUYIN_DOWNLOAD_CDN_DOMAINS):
        raise HTTPException(status_code=400, detail="仅支持代理下载抖音媒体地址")
    response = None
    last_error: requests.RequestException | None = None
    for attempt in range(3):
        try:
            candidate = requests.get(
                safe_url,
                headers={
                    "Referer": "https://www.douyin.com/",
                    "User-Agent": DEFAULT_USER_AGENT,
                },
                proxies=_request_proxies(settings),
                timeout=settings.ytdlp_socket_timeout,
                stream=True,
                allow_redirects=False,
            )
            if candidate.status_code in {403, 429} or candidate.status_code >= 500:
                candidate.close()
                raise requests.HTTPError(f"transient CDN status {candidate.status_code}")
            if not 200 <= candidate.status_code < 300:
                candidate.close()
                raise requests.HTTPError(f"unexpected CDN status {candidate.status_code}")
            response = candidate
            break
        except requests.RequestException as error:
            last_error = error
            if attempt < 2:
                time.sleep(0.2 * (attempt + 1))
    if response is None:
        raise HTTPException(status_code=502, detail="抖音媒体下载失败，请重新解析后重试") from last_error

    safe_name = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", filename).strip(". ") or "LumeAny"
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(safe_name)}",
        "Cache-Control": "private, no-store",
    }
    if response.headers.get("content-length"):
        headers["Content-Length"] = response.headers["content-length"]

    def chunks():
        try:
            yield from response.iter_content(chunk_size=256 * 1024)
        finally:
            response.close()

    return StreamingResponse(
        chunks(),
        media_type=response.headers.get("content-type") or "application/octet-stream",
        headers=headers,
    )


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
                or_(
                    MediaResolveAttempt.successful.is_(True),
                    (
                        MediaResolveAttempt.successful.is_(None)
                        & (MediaResolveAttempt.created_at >= pending_since)
                    ),
                ),
            )
        ) or 0
        if used >= settings.anonymous_parse_limit:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"免费解析次数已用完（共 {settings.anonymous_parse_limit} 次），登录后可继续解析",
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
        return DirectVideoInfo.model_validate(_proxy_douyin_result(result))
    except ClientOnlyMediaError as error:
        attempt.successful = False
        db.commit()
        raise HTTPException(
            status_code=422,
            detail={"code": "client_only", "message": str(error)},
        ) from error
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
