"""报价文件专用持久预留与执行历史端口，不提供内存后备。"""

from typing import Annotated, Literal, Protocol

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from shared.errors import TradeOSError
from shared.schemas.identifiers import IdempotencyKey, TenantId, UserId
from shared.schemas.quote_facts import fact_text
from tool_gateway.repository import ToolCallId

ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
Positive = Annotated[int, Field(gt=0, le=2**63 - 1)]
Label = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,99}$")]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Key = Annotated[
    IdempotencyKey, Field(min_length=1, max_length=200), AfterValidator(fact_text)
]
Actor = Annotated[UserId, Field(max_length=40), AfterValidator(fact_text)]
CallId = Annotated[ToolCallId, Field(pattern=rf"^tcl_{ULID}$")]


class FileDTO(BaseModel):
    """技术边界拒绝强转及额外字段。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class QuoteFileRateLimits(FileDTO):
    """显式资源预算，不设生产默认。"""

    maximum_admissions: Positive
    window_seconds: Positive
    lock_timeout_ms: Positive
    statement_timeout_ms: Positive


class QuoteFileRateRequest(FileDTO):
    """claim之后的真实canonical身份；actor是本次员工而非原call员工。"""

    canonical_call_id: CallId
    tool_version: Label
    idempotency_key: Key
    request_fingerprint: Hash
    fingerprint_version: Label
    actor_id: Actor


class QuoteFileRateDecision(FileDTO):
    """预留成功必须携带已确定提交的真实事件。"""

    outcome: Literal["reserved", "limited"]
    reservation_event_id: Annotated[str, Field(pattern=rf"^tce_{ULID}$")] | None
    retry_after_seconds: Annotated[int, Field(ge=1, le=86400)] | None

    @model_validator(mode="after")
    def consistent(self) -> "QuoteFileRateDecision":
        """互斥结果不能被布尔值或缺失事件伪造。"""
        if self.outcome == "reserved":
            valid = (
                self.reservation_event_id is not None
                and self.retry_after_seconds is None
            )
        else:
            valid = (
                self.reservation_event_id is None
                and self.retry_after_seconds is not None
            )
        if not valid:
            raise ValueError("文件限速结果无效")
        return self


class QuoteFileRateError(TradeOSError):
    """限速边界只输出固定安全错误。"""

    def __init__(
        self,
        code: Literal[
            "invalid_input", "claim_invalid", "storage_unavailable", "commit_unknown"
        ],
    ) -> None:
        messages = {
            "invalid_input": "文件限速输入无效",
            "claim_invalid": "文件调用认领无效",
            "storage_unavailable": "文件限速存储不可用",
            "commit_unknown": "文件限速预留结果未知",
        }
        if code not in messages:
            raise ValueError("文件限速错误码无效")
        self.code = code
        super().__init__(messages[code])


class QuoteFileGenerationRateLimiter(Protocol):
    """独立已提交事件才授予一次执行准入。"""

    async def reserve(
        self, tenant_id: TenantId, request: QuoteFileRateRequest
    ) -> QuoteFileRateDecision: ...


class QuoteFileExecutionHistoryReader(Protocol):
    """只看真实ledger/executing事件，不信可被覆盖的错误category。"""

    async def has_prior_execution(
        self, tenant_id: TenantId, request: QuoteFileRateRequest
    ) -> bool: ...
