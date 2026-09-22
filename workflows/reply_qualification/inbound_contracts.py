"""耐久入站技术事实与端口；不包含邮件内容或Provider游标解释。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from pydantic import Field

from shared.schemas.email_inbound import InboundDisposition, InboundDTO, InboundRoute
from shared.schemas.identifiers import SendingIdentityId


class InboundPageError(RuntimeError):
    def __init__(
        self,
        reason: Literal[
            "binding_conflict",
            "cursor_conflict",
            "page_integrity",
            "receipt_conflict",
            "commit_unconfirmed",
            "not_found",
            "not_archived",
        ],
    ):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class InboundCursor:
    route: InboundRoute
    cursor: str = field(repr=False)
    version: int
    bootstrap_started_at: datetime
    after_epoch: int
    confirmed_by: str
    confirmed_at: datetime
    last_succeeded_at: datetime | None = None
    blocked_reason: str | None = None
    next_retry_at: datetime | None = None


class InboundStatus(InboundDTO):
    state: Literal["disabled", "active", "blocked", "waiting"]
    version: int | None = None
    identity_id: SendingIdentityId | None = None
    reason: (
        Literal[
            "provider_transient",
            "rate_limited",
            "provider_permanent",
            "provider_auth_required",
            "page_integrity",
            "receipt_conflict",
            "domain_rejected",
            "storage_unavailable",
        ]
        | None
    ) = None
    last_succeeded_at: datetime | None = None
    next_retry_at: datetime | None = None


class InboundBindingRequest(InboundDTO):
    identity_id: SendingIdentityId = Field(pattern=r"^sid_[0-7][0-9A-HJKMNP-TV-Z]{25}$")


class InboundRetryRequest(InboundDTO):
    expected_version: int = Field(strict=True, ge=1)


class InboundReviewView(InboundDTO):
    review_id: str
    reason: InboundDisposition | Literal["unknown_outbound"]
    created_at: datetime
    archived: bool


class InboundCursorReader(Protocol):
    async def read_cursor(self) -> InboundCursor | None: ...


from contextlib import AbstractAsyncContextManager

from domains.conversations.service import ConversationService
from domains.outreach.service import OutreachService
from shared.schemas.email_inbound import ArchivedInboundItem, ArchivedInboundPage
from shared.schemas.identifiers import MessageId


class InboundCommitUnknown(RuntimeError):
    def __init__(self) -> None:
        super().__init__("commit_unconfirmed")


class InboundPageTransaction(Protocol):
    conversations: ConversationService
    outreach: OutreachService
    current: InboundCursor

    async def receipt(self, digest: str) -> str | None: ...
    async def record(
        self,
        item: ArchivedInboundItem,
        fingerprint: str,
        message_id: MessageId | None,
        reason: str | None,
    ) -> None: ...
    async def advance(self, next_cursor: str) -> None: ...


class InboundPageUowFactory(Protocol):
    def __call__(
        self, expected: InboundCursor
    ) -> AbstractAsyncContextManager[InboundPageTransaction]: ...


class InboundCommitVerifier(Protocol):
    async def verify_page(
        self,
        expected: InboundCursor,
        page: ArchivedInboundPage,
        fingerprints: dict[str, str],
    ) -> bool: ...
