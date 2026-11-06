import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from .config import Settings, get_settings
from .media import MediaParseError, parse_media


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


@router.post("/parse", response_model=VideoInfo, response_model_by_alias=True)
async def parse_video(body: ParseRequest, settings: Settings = Depends(get_settings)):
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
