"""受信取证的中立契约；不作金额、单位或资料授权判断。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from shared.errors import ConnectorError
from shared.schemas.identifiers import (
    ArtifactId,
    ConversationId,
    EmployeeId,
    MessageId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.provenance import FactualField
from shared.schemas.quote_facts import fact_identity, fact_utc

Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
EvidenceProfile = Literal["pdf-text-v1", "rfc822-plain-v1"]
_Positive = Annotated[int, Field(gt=0)]
_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_PREFIXES = {
    "tenant_id": "tn",
    "artifact_id": "art",
    "message_id": "msg",
    "conversation_id": "con",
    "account_id": "acc",
    "need_id": "need",
}
_SOURCE = re.compile(rf"(?:upload:upl_{_ULID}|message:msg_{_ULID})")
QuoteEvidenceErrorCode = Literal[
    "invalid_input",
    "permission_denied",
    "source_unsupported",
    "source_unavailable",
    "source_limit_exceeded",
    "source_integrity_failed",
    "parse_unavailable",
    "parse_unsupported",
    "parse_timeout",
    "parse_limit_exceeded",
    "locator_mismatch",
    "gateway_unavailable",
]
_MESSAGES: dict[QuoteEvidenceErrorCode, str] = {
    "invalid_input": "来源请求不合法",
    "permission_denied": "当前无权读取来源",
    "source_unsupported": "不支持此来源",
    "source_unavailable": "来源暂不可用",
    "source_limit_exceeded": "来源大小超过限制",
    "source_integrity_failed": "来源完整性核验失败",
    "parse_unavailable": "受限解析能力不可用",
    "parse_unsupported": "来源内容不可按此格式解析",
    "parse_timeout": "来源解析超时",
    "parse_limit_exceeded": "来源解析超过资源限制",
    "locator_mismatch": "来源定位不匹配",
    "gateway_unavailable": "来源工具审计不可用",
}


class QuoteEvidenceError(ConnectorError):
    """只允许固定错误码和固定消息，无原文或外部异常上下文。"""

    is_retryable = False
    code: QuoteEvidenceErrorCode

    def __init__(self, code: QuoteEvidenceErrorCode) -> None:
        if type(code) is not str or code not in _MESSAGES:
            raise ValueError("来源错误码不合法")
        self.code = code
        super().__init__(_MESSAGES[code])


def _profile_page(profile: EvidenceProfile, page: int | None) -> None:
    if (profile == "pdf-text-v1" and (type(page) is not int or page < 1)) or (
        profile == "rfc822-plain-v1" and page is not None
    ):
        raise ValueError("解析页码不合法")


class DTO(BaseModel):
    """本模块内部严格基座。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    @model_validator(mode="after")
    def _identities(self) -> Self:
        for name, prefix in _PREFIXES.items():
            value = getattr(self, name, None)
            if value is not None and (
                type(value) is not str
                or re.fullmatch(rf"{prefix}_{_ULID}", value) is None
            ):
                raise ValueError("来源标识不合法")
        if "actor_id" in type(self).model_fields:
            fact_identity(getattr(self, "actor_id"))  # noqa: B009 - 内部基座按子DTO字段校验
        if "source_ref" in type(self).model_fields:
            source = getattr(self, "source_ref")  # noqa: B009 - 内部基座按子DTO字段校验
            if type(source) is not str or _SOURCE.fullmatch(source) is None:
                raise ValueError("来源引用不合法")
            scope = getattr(self, "scope")  # noqa: B009 - 内部基座按子DTO字段校验
            if (scope.purpose == "pricing") != source.startswith("upload:"):
                raise ValueError("来源用途不匹配")
        return self


class PricingEvidenceScope(DTO):
    """内部价格依据的用途，不批准任何业务写入。"""

    purpose: Literal["pricing"]


class NeedUnitEvidenceScope(DTO):
    """精确绑定Need及操作的原文读取用途。"""

    purpose: Literal["need_unit"]
    need_id: ValidatedNeedId
    action: Literal["read", "confirm"]


EvidenceScope = Annotated[
    PricingEvidenceScope | NeedUnitEvidenceScope, Field(discriminator="purpose")
]


class EvidenceRawMeta(DTO):
    """只携原件安全元数据。"""

    tenant_id: TenantId
    artifact_id: ArtifactId
    kind: Literal["pdf", "email_raw"]
    mime_type: Literal["application/pdf", "message/rfc822"]
    content_hash: Hash
    size_bytes: _Positive
    observed_at: datetime

    @model_validator(mode="after")
    def _binding(self) -> Self:
        if (
            self.mime_type
            != {"pdf": "application/pdf", "email_raw": "message/rfc822"}[self.kind]
        ):
            raise ValueError("原件类型不匹配")
        object.__setattr__(self, "observed_at", fact_utc(self.observed_at))
        return self


class EvidenceRawContent(DTO):
    """只在调用内持有经完整长度与SHA-256核验的bytes。"""

    meta: EvidenceRawMeta
    content: bytes = Field(repr=False, exclude=True)

    @model_validator(mode="after")
    def _integrity(self) -> Self:
        if (
            len(self.content) != self.meta.size_bytes
            or hashlib.sha256(self.content).hexdigest() != self.meta.content_hash
        ):
            raise ValueError("原件完整性不匹配")
        return self


class QuoteMessageReferenceFact(DTO):
    """消息与会话的真实引用投影，不授予阅读权限。"""

    tenant_id: TenantId
    message_id: MessageId
    conversation_id: ConversationId
    account_id: ProspectAccountId
    channel: str
    direction: str
    artifact_id: ArtifactId


class NeedQuantitySourceFact(DTO):
    """保留完整数量来源，仅供内部确认核对。"""

    tenant_id: TenantId
    need_id: ValidatedNeedId
    account_id: ProspectAccountId
    quantity: FactualField[int] | None = Field(repr=False, exclude=True)

    @model_validator(mode="after")
    def _provenance_times(self) -> Self:
        if self.quantity is not None:
            provenance = self.quantity.provenance
            if provenance.confirmed_by is not None:
                fact_identity(provenance.confirmed_by)
            normalized = replace(provenance, extracted_at=fact_utc(provenance.extracted_at), confirmed_at=fact_utc(provenance.confirmed_at) if provenance.confirmed_at is not None else None)
            object.__setattr__(self, "quantity", replace(self.quantity, provenance=normalized))
        return self


class AuthorizedEvidenceReference(DTO):
    """当次授权结果；调用前后仍需重新授权。"""

    tenant_id: TenantId
    actor_id: EmployeeId
    source_ref: str
    scope: EvidenceScope
    raw: EvidenceRawMeta
    message_id: MessageId | None
    conversation_id: ConversationId | None
    account_id: ProspectAccountId | None

    @model_validator(mode="after")
    def _binding(self) -> Self:
        ids = (self.message_id, self.conversation_id, self.account_id)
        upload = self.scope.purpose == "pricing"
        if self.tenant_id != self.raw.tenant_id or self.raw.kind != (
            "pdf" if upload else "email_raw"
        ):
            raise ValueError("原件绑定不匹配")
        if upload and any(value is not None for value in ids):
            raise ValueError("上传不具备已核验业务归属")
        if not upload and (
            any(value is None for value in ids)
            or self.source_ref != f"message:{self.message_id}"
        ):
            raise ValueError("消息绑定不匹配")
        return self


class ParsedEvidenceText(DTO):
    """受限页或正文；不代表业务事实确认。"""

    profile: EvidenceProfile
    page: int | None
    text: str = Field(repr=False, exclude=True)

    @model_validator(mode="after")
    def _page(self) -> Self:
        _profile_page(self.profile, self.page)
        if not self.text.strip():
            raise ValueError("正文为空")
        self.text.encode("utf-8", errors="strict")
        return self


class EvidenceSelection(DTO):
    """Python Unicode code point坐标，不是byte或UTF-16偏移。"""

    profile: EvidenceProfile
    page: int | None
    start: Annotated[int, Field(ge=0)]
    end: _Positive
    excerpt_hash: Hash

    @model_validator(mode="after")
    def _range(self) -> Self:
        _profile_page(self.profile, self.page)
        if self.start >= self.end:
            raise ValueError("选区不合法")
        return self


class EvidenceTextResult(DTO):
    """原文不可普通序列化，根hash用途不包含解析字段。"""

    reference: AuthorizedEvidenceReference
    profile: EvidenceProfile | None
    page: int | None
    text: str | None = Field(repr=False, exclude=True)
    text_hash: Hash | None
    selection: EvidenceSelection | None
    excerpt: str | None = Field(repr=False, exclude=True)
    locator: str | None

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.profile is None:
            if (
                self.locator != "$"
                or self.reference.scope.purpose != "pricing"
                or any(
                    v is not None
                    for v in (
                        self.page,
                        self.text,
                        self.text_hash,
                        self.selection,
                        self.excerpt,
                    )
                )
            ):
                raise ValueError("根定位结果不合法")
        else:
            _profile_page(self.profile, self.page)
            if (
                self.text is None
                or not self.text.strip()
                or hashlib.sha256(self.text.encode("utf-8")).hexdigest()
                != self.text_hash
            ):
                raise ValueError("正文完整性不匹配")
            if self.selection is None:
                if self.excerpt is not None or self.locator is not None:
                    raise ValueError("预览结果不合法")
            elif (
                (self.selection.profile, self.selection.page)
                != (self.profile, self.page)
                or self.excerpt != self.text[self.selection.start : self.selection.end]
                or self.selection.end > len(self.text)
                or hashlib.sha256(self.excerpt.encode("utf-8")).hexdigest()
                != self.selection.excerpt_hash
                or self.locator != make_evidence_locator(self.selection)
            ):
                raise ValueError("选区结果不匹配")
        return self


class EvidenceVerifyRequest(DTO):
    """只接受固定来源、用途与canonical定位。"""

    operation: Literal["verify"]
    source_ref: str
    scope: EvidenceScope
    locator: str

    @model_validator(mode="after")
    def _locator(self) -> Self:
        try:
            selection = parse_evidence_locator(self.locator)
        except QuoteEvidenceError:
            raise ValueError("定位不合法") from None
        if selection is None:
            if self.scope.purpose != "pricing":
                raise ValueError("根定位用途不合法")
        elif (selection.profile == "pdf-text-v1") != (self.scope.purpose == "pricing"):
            raise ValueError("解析用途不匹配")
        return self


class EvidencePreviewRequest(DTO):
    """只指定受限页或正文，不接受客户端抽字。"""

    operation: Literal["preview"]
    source_ref: str
    scope: EvidenceScope
    profile: EvidenceProfile
    page: int | None

    @model_validator(mode="after")
    def _page(self) -> Self:
        _profile_page(self.profile, self.page)
        if (self.profile == "pdf-text-v1") != (self.scope.purpose == "pricing"):
            raise ValueError("解析用途不匹配")
        return self


class EvidenceLocateRequest(DTO):
    """以原件及完整正文hash防止对旧预览定位。"""

    operation: Literal["locate"]
    source_ref: str
    scope: EvidenceScope
    profile: EvidenceProfile
    page: int | None
    start: Annotated[int, Field(ge=0)]
    end: _Positive
    expected_raw_hash: Hash
    expected_text_hash: Hash

    @model_validator(mode="after")
    def _range(self) -> Self:
        _profile_page(self.profile, self.page)
        if self.start >= self.end or (self.profile == "pdf-text-v1") != (
            self.scope.purpose == "pricing"
        ):
            raise ValueError("选区用途不合法")
        return self


EvidenceReadRequest = Annotated[
    EvidenceVerifyRequest | EvidencePreviewRequest | EvidenceLocateRequest,
    Field(discriminator="operation"),
]


class ObjectReadLimits(DTO):
    """来源专用网络预算；所有字段必须由部署显式提供。"""

    connect_timeout_ms: _Positive
    read_timeout_ms: _Positive
    total_timeout_ms: _Positive
    chunk_bytes: _Positive
    maximum_attempts: _Positive


class EvidenceParseLimits(DTO):
    """每次解析和排队的有限预算，无生产默认值。"""

    maximum_input_bytes: _Positive
    maximum_pages: _Positive
    maximum_text_bytes: _Positive
    maximum_excerpt_bytes: _Positive
    cpu_seconds: _Positive
    address_space_bytes: _Positive
    wall_timeout_ms: _Positive
    maximum_result_bytes: _Positive
    maximum_concurrency: _Positive
    queue_timeout_ms: _Positive
    termination_grace_ms: _Positive

    @model_validator(mode="after")
    def _excerpt(self) -> Self:
        if self.maximum_excerpt_bytes > self.maximum_text_bytes:
            raise ValueError("选区预算超过正文预算")
        return self


class EvidenceProbeLimits(DTO):
    """独立有限探针预算，不从生产上限推导分配量。"""

    cpu_seconds: _Positive
    address_space_bytes: _Positive
    wall_timeout_ms: _Positive
    allocation_chunk_bytes: _Positive
    maximum_probe_bytes: _Positive
    maximum_result_bytes: _Positive
    termination_grace_ms: _Positive


class EvidenceParserCapability(DTO):
    """只供报告的能力摘要，不是可以反向载入的授权票据。"""

    status: Literal["available", "unavailable"]
    profile_version: Literal["evidence-worker-v1"]
    limits_hash: Hash
    runtime_hash: Hash
    failure: Literal["platform", "resource", "protocol", "runtime"] | None


_NUMBER = r"(?:0|[1-9][0-9]*)"
_PDF_LOCATOR = re.compile(
    rf"pdf-text-v1:p=([1-9][0-9]*);c=({_NUMBER}):({_NUMBER});h=([0-9a-f]{{64}})"
)
_EMAIL_LOCATOR = re.compile(
    rf"rfc822-plain-v1:c=({_NUMBER}):({_NUMBER});h=([0-9a-f]{{64}})"
)


def parse_evidence_locator(locator: str) -> EvidenceSelection | None:
    """解码唯一canonical格式；只有根符号返回None。"""
    if type(locator) is not str:
        raise QuoteEvidenceError("invalid_input")
    if locator == "$":
        return None
    pdf = _PDF_LOCATOR.fullmatch(locator)
    email = _EMAIL_LOCATOR.fullmatch(locator)
    try:
        if pdf:
            page, start, end, digest = pdf.groups()
            return EvidenceSelection(
                profile="pdf-text-v1",
                page=int(page),
                start=int(start),
                end=int(end),
                excerpt_hash=digest,
            )
        if email:
            start, end, digest = email.groups()
            return EvidenceSelection(
                profile="rfc822-plain-v1",
                page=None,
                start=int(start),
                end=int(end),
                excerpt_hash=digest,
            )
    except (ValueError, ValidationError):
        pass
    raise QuoteEvidenceError("invalid_input") from None


def make_evidence_locator(selection: EvidenceSelection) -> str:
    """将已验证选区编码为唯一稳定定位字符串。"""
    try:
        checked = EvidenceSelection.model_validate(selection.model_dump())
    except (AttributeError, ValidationError):
        raise QuoteEvidenceError("invalid_input") from None
    page = f"p={checked.page};" if checked.profile == "pdf-text-v1" else ""
    return f"{checked.profile}:{page}c={checked.start}:{checked.end};h={checked.excerpt_hash}"


def select_evidence_text(
    parsed: ParsedEvidenceText, start: int, end: int, *, maximum_excerpt_bytes: int
) -> tuple[EvidenceSelection, str]:
    """按Unicode code point选择受限原文。"""
    if (
        type(maximum_excerpt_bytes) is not int
        or maximum_excerpt_bytes <= 0
        or type(start) is not int
        or type(end) is not int
    ):
        raise QuoteEvidenceError("invalid_input")
    if not 0 <= start < end <= len(parsed.text):
        raise QuoteEvidenceError("locator_mismatch")
    excerpt = parsed.text[start:end]
    encoded = excerpt.encode("utf-8")
    if len(encoded) > maximum_excerpt_bytes:
        raise QuoteEvidenceError("parse_limit_exceeded")
    return EvidenceSelection(
        profile=parsed.profile,
        page=parsed.page,
        start=start,
        end=end,
        excerpt_hash=hashlib.sha256(encoded).hexdigest(),
    ), excerpt
