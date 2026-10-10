from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime


@dataclass(slots=True)
class Video:
    id: int
    video_number: int | None
    category: str | None
    telegram_file_id: str | None  # None for videos found by the scanner; delivery then copies the post
    telegram_file_unique_id: str
    caption: str | None
    created_at: datetime
    is_active: bool
    last_error: str | None
    channel_id: int | None
    telegram_message_id: int | None
    message_date: datetime | None
    duration: int | None
    file_size: int | None
    mime_type: str | None
    width: int | None
    height: int | None

    @classmethod
    def from_record(cls, record) -> "Video":
        return cls(*(record[f.name] for f in fields(cls)))


@dataclass(slots=True)
class ChannelVideo:
    """Metadata of one video post in the source channel (no video data, ever)."""
    channel_id: int
    message_id: int
    file_unique_id: str
    file_id: str | None            # only known when seen through the Bot API
    message_date: datetime | None
    caption: str | None
    duration: int | None
    file_size: int | None
    mime_type: str | None
    width: int | None
    height: int | None
    category: str = "old"


@dataclass(slots=True)
class UpsertResult:
    added: int = 0
    duplicates: int = 0   # posts that were already registered (same channel + message id)
    repeated: int = 0     # newly stored posts whose video already exists in another post
