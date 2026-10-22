"""QQ 单次受控验收的非秘密契约；不提供启用、网络或发送能力。"""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal, Protocol

from pydantic import Field, model_validator

from shared.schemas.email_inbound import InboundDTO, InboundRoute
from shared.schemas.identifiers import EmployeeId, UserId


class QQTrialStopped(Exception):
    """固定终止信号；调用者不得将其解释为可重试故障。"""

    def __init__(self, reason: str = "trial_stopped") -> None:
        self.reason = reason
        super().__init__("QQ 受控验收已停止")


class QQTrialLimits(InboundDTO):
    duration_seconds: int = Field(default=600, strict=True, ge=1, le=600)
    interval_seconds: int = Field(default=30, strict=True, ge=30)
    sessions: int = Field(default=20, strict=True, ge=1, le=20)
    searches: int = Field(default=20, strict=True, ge=1, le=20)
    headers: int = Field(default=20, strict=True, ge=1, le=20)
    bodies: int = Field(default=1, strict=True, ge=1, le=1)
    body_bytes: int = Field(default=262144, strict=True, ge=1, le=262144)
    classifications: int = Field(default=1, strict=True, ge=1, le=1)
    provider_requests: int = Field(default=4, strict=True, ge=1, le=4)
    model_input_bytes: int = Field(default=32768, strict=True, ge=1, le=32768)
    model_output_tokens: int = Field(default=512, strict=True, ge=1, le=512)
    model_timeout_seconds: int = Field(default=180, strict=True, ge=1, le=180)


class QQTrialScope(InboundDTO):
    trial_id: str = Field(pattern=r"^qqt_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    route: InboundRoute
    attempt_id: str = Field(pattern=r"^mat_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    employee_id: EmployeeId
    user_id: UserId
    classifier_employee_id: EmployeeId
    classifier_user_id: UserId
    model: str
    model_configuration_version: str
    sender: Literal["1249583590@qq.com"] = Field(
        default="1249583590@qq.com", repr=False
    )
    recipient: Literal["986647123@qq.com"] = Field(
        default="986647123@qq.com", repr=False
    )
    canonical_header: str = Field(
        repr=False,
        pattern=r"^<[a-z0-9-]{1,32}\.[0-9a-f]{64}@messages\.tradeos\.invalid>$",
    )
    provider_header: str = Field(
        repr=False, pattern=r"^<[^<>\s@]+@[^<>\s@]+>$", max_length=998
    )
    alias_evidence_id: str
    alias_source: Literal["sender_sent", "controlled_recipient_imap"] = "sender_sent"
    delivery_employee_id: EmployeeId | None = None
    delivery_user_id: UserId | None = None
    authorization_ref: str = Field(min_length=1, max_length=200)
    authorization_source: Literal["explicit_user_request"]
    starts_at: datetime
    expires_at: datetime
    limits: QQTrialLimits

    @model_validator(mode="after")
    def bounded_scope(self) -> "QQTrialScope":
        if self.alias_source == "controlled_recipient_imap":
            if (not self.delivery_employee_id or not self.delivery_user_id
                or self.delivery_employee_id == self.employee_id
                or self.delivery_user_id == self.user_id):
                raise ValueError("收件方原件必须绑定独立且明确的当前员工")
        elif self.delivery_employee_id is not None or self.delivery_user_id is not None:
            raise ValueError("发件箱来源不能混入收件方身份")
        if (
            self.starts_at.tzinfo is None
            or self.expires_at.tzinfo is None
            or not 0
            < (self.expires_at - self.starts_at).total_seconds()
            <= self.limits.duration_seconds
            or self.employee_id == self.classifier_employee_id
            or self.provider_header == self.canonical_header
            or not self.canonical_header.startswith("<" + self.route.route_id + ".")
            or self.route.mailbox_alias == "qq-project-reply"
        ):
            raise ValueError("受控验收范围无效；不能复用历史邮箱游标")
        return self


class QQTrialWatermark(InboundDTO):
    uidvalidity: int = Field(strict=True, ge=1, le=4294967295)
    high_uid: int = Field(strict=True, ge=0, le=4294967295)


BudgetKind = Literal[
    "sessions", "searches", "headers", "bodies", "classifications", "provider_requests"
]


class QQTrialGate(Protocol):
    """在原 Gateway 内使用的持久门禁；未知预留不退款、不重新领取。"""

    scope: QQTrialScope

    async def check(self) -> None: ...
    async def claim_scan(self) -> bool: ...
    async def watermark(self) -> QQTrialWatermark | None: ...
    async def initialize(self, value: QQTrialWatermark) -> None: ...
    async def reserve(self, kind: BudgetKind, *, input_bytes: int = 0) -> None: ...
    async def stop(self, reason: str) -> None: ...
    async def record_address_compatibility(self, fields: tuple[str, ...]) -> None: ...
    async def require_message(self, message_id: str) -> None: ...


class QQProviderRoundBudget(Protocol):
    async def run(
        self,
        body: dict,
        dispatch: Callable[[dict], Awaitable[dict]],
        *,
        output_key: Literal["max_tokens", "max_output_tokens"] = "max_output_tokens",
    ) -> dict: ...
