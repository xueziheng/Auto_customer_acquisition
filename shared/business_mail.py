"""普通收件插件的进程内交接；正文与MIME不得进入工具账本。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class BusinessMailPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    uidvalidity: int = Field(strict=True, ge=1, le=4294967295)
    uid: int = Field(strict=True, ge=1, le=4294967295)
    sender: str = Field(repr=False, exclude=True, max_length=1000)
    subject: str = Field(repr=False, exclude=True, max_length=2000)
    body_text: str = Field(repr=False, exclude=True, max_length=50000)
    message_header: str | None = Field(
        default=None, repr=False, exclude=True, max_length=1000
    )
    received_at: datetime
    parse_failure: str | None = None
    source_kind: Literal["full_mime", "headers_only"] = "full_mime"


class RawBusinessMail(BusinessMailPayload):
    raw_mime: bytes = Field(repr=False, exclude=True, max_length=4 * 1024 * 1024)


class ArchivedBusinessMail(BusinessMailPayload):
    artifact_id: str
    mime_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class BusinessFetchPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    uidvalidity: int
    after_uid: int
    next_uid: int
    snapshot_end_uid: int | None = None
    messages: tuple[RawBusinessMail, ...] = Field(repr=False, exclude=True)


class BusinessArchivedPage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    uidvalidity: int
    after_uid: int
    next_uid: int
    snapshot_end_uid: int | None = None
    messages: tuple[ArchivedBusinessMail, ...] = Field(repr=False, exclude=True)
