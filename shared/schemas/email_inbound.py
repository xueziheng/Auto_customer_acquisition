"""ADR0026：只在受信调用栈内交接的入站候选契约。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.schemas.identifiers import ArtifactId, SendingIdentityId, TenantId

MIME_BYTES = 4 * 1024 * 1024
PAGE_BYTES = 8 * 1024 * 1024
PAGE_ITEMS = 20
HEADER_BYTES = 64 * 1024
MIME_PARTS = 100
MIME_DEPTH = 20
CANDIDATE_BYTES = 256 * 1024
CURSOR_BYTES = 32 * 1024


class InboundError(RuntimeError):
    """固定安全失败，不接收provider原文或异常上下文。"""

    def __init__(self) -> None:
        super().__init__("email_inbound_unavailable")


class InboundDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class InboundRoute(InboundDTO):
    """只由composition提供的路由，不从邮件或调用参数猜测。"""

    tenant_id: TenantId = Field(pattern=r"^tn_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    mailbox_alias: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    configured_identity_id: SendingIdentityId = Field(
        pattern=r"^sid_[0-7][0-9A-HJKMNP-TV-Z]{25}$"
    )
    route_id: str = Field(pattern=r"^[a-z0-9-]{1,32}$")
    config_version: str = Field(pattern=r"^[a-z0-9-]{1,32}$")


class InboundDisposition(StrEnum):
    CANDIDATE = "candidate"
    TOO_LARGE = "too_large"
    MESSAGE_GONE = "message_gone"
    MALFORMED = "malformed"
    HEADER_LIMIT = "header_limit"
    MIME_LIMIT = "mime_limit"
    TEXT_TOO_LARGE = "text_too_large"
    MISSING_MESSAGE_ID = "missing_message_id"
    INVALID_MESSAGE_ID = "invalid_message_id"
    INVALID_IN_REPLY_TO = "invalid_in_reply_to"
    INVALID_SENT_AT = "invalid_sent_at"
    NO_BODY = "no_body"
    SKIPPED_LABEL = "skipped_label"
    SKIPPED_DELIVERY_REPORT = "skipped_delivery_report"
    CREDENTIAL_MARKER = "credential_marker"


class InboundItem(InboundDTO):
    provider_ref_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: InboundDisposition
    external_message_id: str | None = Field(default=None, repr=False, exclude=True)
    in_reply_to: str | None = Field(default=None, repr=False, exclude=True)
    sent_at: datetime | None = None
    parser_version: str = Field(default="inbound-v1", pattern=r"^inbound-v1$")


class InboundContent(InboundDTO):
    """纯解析结果，不代表已过guard，不包含Provider/关联事实。"""

    disposition: InboundDisposition
    parser_version: str = Field(default="inbound-v1", pattern=r"^inbound-v1$")
    subject: str | None = Field(default=None, repr=False, exclude=True)
    body: str = Field(default="", repr=False, exclude=True)
    guard_body: str = Field(default="", repr=False, exclude=True)


class ProviderInboundItem(InboundItem):
    """完整候选仅供Gateway归档与guard，不可作为模型输入DTO。"""

    raw_mime: bytes | None = Field(
        default=None, repr=False, exclude=True, max_length=MIME_BYTES
    )
    subject: str | None = Field(default=None, repr=False, exclude=True)
    body: str = Field(default="", repr=False, exclude=True)
    guard_body: str = Field(default="", repr=False, exclude=True)
    internal_date: datetime | None = Field(default=None, repr=False, exclude=True)


class ArchivedInboundRaw(InboundDTO):
    tenant_id: TenantId
    artifact_id: ArtifactId
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0, le=MIME_BYTES)


class ArchivedInboundItem(InboundItem):
    raw: ArchivedInboundRaw | None = None


class ProviderInboundPage(InboundDTO):
    route: InboundRoute
    starting_cursor: str = Field(repr=False, exclude=True, max_length=CURSOR_BYTES)
    next_cursor: str = Field(repr=False, exclude=True, max_length=CURSOR_BYTES)
    items: tuple[ProviderInboundItem, ...] = Field(max_length=PAGE_ITEMS)

    @model_validator(mode="after")
    def bounded_page(self) -> ProviderInboundPage:
        if sum(len(item.raw_mime or b"") for item in self.items) > PAGE_BYTES:
            raise ValueError("inbound_page_limit")
        return self


class ArchivedInboundPage(InboundDTO):
    route: InboundRoute
    starting_cursor: str = Field(repr=False, exclude=True, max_length=CURSOR_BYTES)
    next_cursor: str = Field(repr=False, exclude=True, max_length=CURSOR_BYTES)
    items: tuple[ArchivedInboundItem, ...] = Field(max_length=PAGE_ITEMS)

    @model_validator(mode="after")
    def bounded_page(self) -> ArchivedInboundPage:
        if sum(
            item.raw.size_bytes if item.raw else 0 for item in self.items
        ) > PAGE_BYTES or any(
            item.raw and item.raw.tenant_id != self.route.tenant_id
            for item in self.items
        ):
            raise ValueError("inbound_page_integrity")
        return self
