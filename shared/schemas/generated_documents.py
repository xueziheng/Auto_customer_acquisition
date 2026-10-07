"""派生PDF中立技术存储契约；不包含报价授权、对象地址或删除能力。"""

from datetime import datetime
from typing import Annotated, Literal, Protocol

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from shared.errors import TradeOSError
from shared.schemas.identifiers import ArtifactId, IdempotencyKey, RunId, TenantId
from shared.schemas.quote_facts import fact_text, fact_utc

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_Positive = Annotated[int, Field(gt=0, le=2**63 - 1)]
_Text = Annotated[str, Field(min_length=1, max_length=200), AfterValidator(fact_text)]
GeneratedDocumentErrorCode = Literal[
    "not_found",
    "invalid_binding",
    "conflict",
    "unavailable",
    "commit_unknown",
    "corrupt",
    "read_limit",
    "bounded_unavailable",
]


class GeneratedDocumentError(TradeOSError):
    """仅固定技术失败，不接收底层异常文本。"""

    def __init__(self, code: GeneratedDocumentErrorCode) -> None:
        messages = {
            "not_found": "派生文件不存在",
            "invalid_binding": "派生文件绑定无效",
            "conflict": "派生文件幂等冲突",
            "unavailable": "派生文件存储不可用",
            "commit_unknown": "派生文件写入结果未知",
            "corrupt": "派生文件完整性校验失败",
            "read_limit": "派生文件超过读取限制",
            "bounded_unavailable": "派生文件有界读取不可用",
        }
        if code not in messages:
            raise ValueError("无效派生文件错误码")
        self.code = code
        super().__init__(messages[code])


class GeneratedDocumentMeta(BaseModel):
    """严格技术metadata，artifact_hash只表示bytes SHA256。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    tenant_id: TenantId = Field(pattern=rf"^tn_{_ULID}$")
    artifact_id: ArtifactId = Field(pattern=rf"^art_{_ULID}$")
    kind: Literal["quote_pdf"]
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: _Positive
    mime_type: Literal["application/pdf"]
    workflow_run_id: RunId = Field(pattern=rf"^run_{_ULID}$")
    subject_ref: _Text
    sequence_number: _Positive
    idempotency_key: Annotated[
        IdempotencyKey, Field(min_length=1, max_length=200), AfterValidator(fact_text)
    ]
    generated_by: _Text
    generated_at: Annotated[datetime, AfterValidator(fact_utc)]


class GeneratedDocumentPayload(BaseModel):
    """仅可信进程内交接；bytes不进repr或自动HTTP注册。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    meta: GeneratedDocumentMeta
    content: bytes = Field(repr=False)


class QuotePdfWriteLimits(BaseModel):
    """显式单次SDK预算，不声称deadline能杀死线程。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    connect_timeout_ms: _Positive
    read_timeout_ms: _Positive
    total_timeout_ms: _Positive
    maximum_attempts: Literal[1]

    @field_validator("maximum_attempts", mode="before")
    @classmethod
    def _one_attempt(cls, value: object) -> object:
        """Literal的bool相等性不能开启隐式重试或类型转换。"""
        if type(value) is not int or value != 1:
            raise ValueError("报价文件写入必须恰一次尝试")
        return value


class GeneratedDocumentMetadataReader(Protocol):
    """显式恢复仅获得此独立对象，不持宽Store换窄注解。"""

    async def get_meta_by_key(
        self, tenant_id: TenantId, key: IdempotencyKey
    ) -> GeneratedDocumentMeta | None:
        """原键安全metadata读取，None不证明从未写对象。"""
        ...


class GeneratedDocumentStore(GeneratedDocumentMetadataReader, Protocol):
    """仅报价PDF的有界读与幂等写，无删除/raw/get能力。"""

    async def get_bounded(
        self, tenant_id: TenantId, artifact_id: ArtifactId, *, maximum_bytes: int
    ) -> GeneratedDocumentPayload:
        """实际读取受限并验证长度与SHA256。"""
        ...

    async def put_pdf(
        self,
        tenant_id: TenantId,
        content: bytes,
        *,
        workflow_run_id: RunId,
        subject_ref: str,
        sequence_number: int,
        idempotency_key: IdempotencyKey,
        generated_by: str,
    ) -> GeneratedDocumentMeta:
        """保留候选未知提交，winner只读且不可删除。"""
        ...
