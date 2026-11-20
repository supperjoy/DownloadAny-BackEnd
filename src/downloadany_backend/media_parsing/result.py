"""Provider-neutral media result shared by both API response adapters."""


from dataclasses import dataclass, field, fields


@dataclass
class MediaResult:
    id: str
    title: str
    platform: str = "generic"
    webpage_url: str = ""
    thumbnail: str | None = None
    uploader: str | None = None
    duration: float | None = None
    video_url: str | None = None
    resource_url: str | None = None
    quality: str | None = None
    ext: str | None = None
    filesize: int | None = None
    expires_at: str | None = None
    audio_url: str | None = None
    audio_formats: list[dict] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    formats: list[dict] = field(default_factory=list)
    download_options: list[dict] = field(default_factory=list)
    download_source: str | None = None

    @classmethod
    def from_direct(cls, data: dict):
        names = {item.name for item in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in names})
