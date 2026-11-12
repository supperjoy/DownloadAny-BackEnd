import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from .config import Settings, get_settings
from .dependencies import current_user
from .media import MediaParseError, parse_media, resolve_direct_video
from .models import User


router = APIRouter(prefix="/api/media", tags=["media"])
parse_slots = asyncio.Semaphore(4)


class ParseRequest(BaseModel):
    url: str = Field(min_length=8, max_length=4096)


class FormatOption(BaseModel):
    id: str
    label: str
    height: int | None = None
    ext: str | None = None
    filesize: int | None = None


class VideoInfo(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    id: str
    title: str
    duration: float | None = None
    thumbnail: str | None = None
    uploader: str | None = None
    webpage_url: str | None = None
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
async def resolve_video(body: ParseRequest, settings: Settings = Depends(get_settings)):
    try:
        async with parse_slots:
            result = await asyncio.wait_for(
                asyncio.to_thread(resolve_direct_video, body.url, settings),
                timeout=settings.ytdlp_parse_timeout,
            )
        return DirectVideoInfo.model_validate(result)
    except MediaParseError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except TimeoutError as error:
        raise HTTPException(status_code=504, detail="解析超时，请稍后重试") from error
