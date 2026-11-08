from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from urllib.parse import urljoin

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .database import get_db
from .models import ComponentRelease, DesktopRelease, utcnow


router = APIRouter(prefix="/api/updates", tags=["updates"])


def _version(value: str) -> Version:
    try:
        return Version(value.removeprefix("v"))
    except InvalidVersion as exc:
        raise HTTPException(status_code=422, detail=f"无效版本号: {value}") from exc


def _eligible(device_id: str, release_key: str, percent: int) -> bool:
    if percent >= 100:
        return True
    if percent <= 0:
        return False
    digest = hashlib.sha256(f"{release_key}:{device_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 100 < percent


def _asset_url(path: str, settings: Settings) -> str:
    if path.startswith(("https://", "http://")):
        return path
    if not settings.update_cdn_base_url:
        raise HTTPException(status_code=503, detail="UPDATE_CDN_BASE_URL 未配置")
    return urljoin(settings.update_cdn_base_url.rstrip("/") + "/", path.lstrip("/"))


def _channels(channel: str) -> list[str]:
    if channel == "stable":
        return ["stable"]
    if channel == "beta":
        return ["stable", "beta"]
    raise HTTPException(status_code=400, detail="channel 必须是 stable 或 beta")


def _require_admin(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    expected = settings.update_admin_token
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="发布管理令牌无效")


class DesktopReleaseIn(BaseModel):
    version: str
    channel: str = "stable"
    target: str
    arch: str
    artifact_path: str
    signature: str
    notes: str = ""
    rollout_percent: int = Field(default=100, ge=0, le=100)
    minimum_version: str | None = None
    active: bool = True
    published_at: datetime | None = None

    @field_validator("channel")
    @classmethod
    def valid_channel(cls, value: str) -> str:
        if value not in {"stable", "beta"}:
            raise ValueError("channel must be stable or beta")
        return value


class ComponentReleaseIn(BaseModel):
    component: str = "yt-dlp"
    version: str
    channel: str = "stable"
    target: str
    artifact_path: str
    signature: str
    sha256: str
    rollout_percent: int = Field(default=100, ge=0, le=100)
    active: bool = True
    published_at: datetime | None = None

    @field_validator("channel")
    @classmethod
    def valid_channel(cls, value: str) -> str:
        if value not in {"stable", "beta"}:
            raise ValueError("channel must be stable or beta")
        return value


class ReleaseControl(BaseModel):
    rollout_percent: int | None = Field(default=None, ge=0, le=100)
    minimum_version: str | None = None
    active: bool | None = None


@router.get("/desktop/{target}/{arch}/{current_version}")
async def desktop_update(
    target: str,
    arch: str,
    current_version: str,
    response: Response,
    channel: str = Header(default="stable", alias="X-DownloadAny-Channel"),
    device_id: str = Header(default="anonymous", alias="X-DownloadAny-Device-Id"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    current = _version(current_version)
    releases = db.scalars(
        select(DesktopRelease).where(
            DesktopRelease.active.is_(True),
            DesktopRelease.target == target,
            DesktopRelease.arch == arch,
            DesktopRelease.channel.in_(_channels(channel)),
        )
    ).all()
    newer = [item for item in releases if _version(item.version) > current]
    newer.sort(key=lambda item: _version(item.version), reverse=True)
    for item in newer:
        forced = bool(item.minimum_version and current < _version(item.minimum_version))
        if not forced and not _eligible(device_id, f"desktop:{item.id}", item.rollout_percent):
            continue
        return {
            "version": item.version.removeprefix("v"),
            "url": _asset_url(item.artifact_path, settings),
            "signature": item.signature,
            "notes": item.notes,
            "pub_date": item.published_at.isoformat() + "Z",
        }
    response.status_code = status.HTTP_204_NO_CONTENT
    return None


@router.get("/components/{component}/{target}/{current_version}")
async def component_update(
    component: str,
    target: str,
    current_version: str,
    response: Response,
    channel: str = Header(default="stable", alias="X-DownloadAny-Channel"),
    device_id: str = Header(default="anonymous", alias="X-DownloadAny-Device-Id"),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    current = _version(current_version)
    releases = db.scalars(
        select(ComponentRelease).where(
            ComponentRelease.active.is_(True),
            ComponentRelease.component == component,
            ComponentRelease.target == target,
            ComponentRelease.channel.in_(_channels(channel)),
        )
    ).all()
    newer = [item for item in releases if _version(item.version) > current]
    newer.sort(key=lambda item: _version(item.version), reverse=True)
    for item in newer:
        if not _eligible(device_id, f"component:{item.id}", item.rollout_percent):
            continue
        return {
            "component": item.component,
            "version": item.version,
            "url": _asset_url(item.artifact_path, settings),
            "signature": item.signature,
            "sha256": item.sha256,
            "published_at": item.published_at.isoformat() + "Z",
        }
    response.status_code = status.HTTP_204_NO_CONTENT
    return None


@router.post("/admin/desktop", dependencies=[Depends(_require_admin)])
async def publish_desktop(payload: DesktopReleaseIn, db: Session = Depends(get_db)):
    _version(payload.version)
    if payload.minimum_version:
        _version(payload.minimum_version)
    release = DesktopRelease(**payload.model_dump(exclude={"published_at"}))
    release.published_at = payload.published_at or utcnow()
    db.add(release)
    db.commit()
    db.refresh(release)
    return {"id": release.id, "version": release.version}


@router.post("/admin/components", dependencies=[Depends(_require_admin)])
async def publish_component(payload: ComponentReleaseIn, db: Session = Depends(get_db)):
    _version(payload.version)
    if len(payload.sha256) != 64 or any(c not in "0123456789abcdefABCDEF" for c in payload.sha256):
        raise HTTPException(status_code=422, detail="sha256 必须是 64 位十六进制")
    release = ComponentRelease(**payload.model_dump(exclude={"published_at"}))
    release.published_at = payload.published_at or utcnow()
    db.add(release)
    db.commit()
    db.refresh(release)
    return {"id": release.id, "component": release.component, "version": release.version}


@router.delete("/admin/desktop/{release_id}", dependencies=[Depends(_require_admin)])
async def disable_desktop(release_id: int, db: Session = Depends(get_db)):
    release = db.get(DesktopRelease, release_id)
    if not release:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    release.active = False
    db.commit()
    return {"id": release.id, "active": False}


@router.patch("/admin/desktop/{release_id}", dependencies=[Depends(_require_admin)])
async def control_desktop(
    release_id: int,
    payload: ReleaseControl,
    db: Session = Depends(get_db),
):
    release = db.get(DesktopRelease, release_id)
    if not release:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    changes = payload.model_dump(exclude_unset=True)
    if "minimum_version" in changes and changes["minimum_version"]:
        _version(changes["minimum_version"])
    for key, value in changes.items():
        setattr(release, key, value)
    db.commit()
    return {
        "id": release.id,
        "active": release.active,
        "rollout_percent": release.rollout_percent,
        "minimum_version": release.minimum_version,
    }


@router.delete("/admin/components/{release_id}", dependencies=[Depends(_require_admin)])
async def disable_component(release_id: int, db: Session = Depends(get_db)):
    release = db.get(ComponentRelease, release_id)
    if not release:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    release.active = False
    db.commit()
    return {"id": release.id, "active": False}


@router.patch("/admin/components/{release_id}", dependencies=[Depends(_require_admin)])
async def control_component(
    release_id: int,
    payload: ReleaseControl,
    db: Session = Depends(get_db),
):
    release = db.get(ComponentRelease, release_id)
    if not release:
        raise HTTPException(status_code=404, detail="发布记录不存在")
    changes = payload.model_dump(exclude_unset=True, exclude={"minimum_version"})
    for key, value in changes.items():
        setattr(release, key, value)
    db.commit()
    return {
        "id": release.id,
        "active": release.active,
        "rollout_percent": release.rollout_percent,
    }
