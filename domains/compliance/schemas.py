"""国家政策包的公共 DTO 与严格人工来源输入边界。"""

from __future__ import annotations

import re
import unicodedata
from datetime import UTC, datetime
from enum import Enum
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.schemas.identifiers import (
    ApprovalId,
    CountryPolicyActivationId,
    CountryPolicyVersionId,
    EmployeeId,
)
from shared.schemas.provenance import Provenance, SourceType

_SOURCE_ID_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9._:-]{0,199}\Z")
_REQUIREMENT_PATTERN = re.compile(r"[a-z][a-z0-9._:-]{0,127}\Z")
_LOWER_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_COUNTRY_POLICY_VERSION_ID_PATTERN = re.compile(r"cpp_[0-7][0-9A-HJKMNP-TV-Z]{25}\Z")
_TRUSTED_SOURCE_TYPES = frozenset(
    {SourceType.WEB_PAGE, SourceType.UPLOAD, SourceType.EMPLOYEE_INPUT}
)
CountryPolicySourceType = Literal[
    SourceType.WEB_PAGE,
    SourceType.UPLOAD,
    SourceType.EMPLOYEE_INPUT,
]


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _normalized_text(value: object, *, field_name: str, maximum: int) -> str:
    if not isinstance(value, str):
        # Pydantic v2 只把 ValueError 纳入结构化字段错误；TypeError 会越过边界。
        raise ValueError(f"{field_name} 必须是字符串")  # noqa: TRY004
    if _has_control(value):
        raise ValueError(f"{field_name} 不得包含控制字符")
    normalized = " ".join(unicodedata.normalize("NFKC", value).strip().split())
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"{field_name} 不能为空且最长 {maximum} 字符")
    return normalized


def normalize_country_key(country: str) -> str:
    """生成只用于精确匹配的国家键，不推断别名或 ISO 等价关系。"""
    return _normalized_text(country, field_name="country", maximum=64).casefold()


def _aware_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} 必须是带时区时间")
    return value.astimezone(UTC)


def _valid_hash(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or _LOWER_HASH_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} 必须是 64 位小写 SHA-256")
    return value


class CountryPolicyField(str, Enum):
    PUBLIC_RESEARCH_ALLOWED = "public_research_allowed"
    CONTACT_ENRICHMENT_ALLOWED = "contact_enrichment_allowed"
    COLD_B2B_EMAIL_ALLOWED = "cold_b2b_email_allowed"
    PERSONAL_DATA_BASIS_REQUIRED = "personal_data_basis_required"
    SUBJECT_TYPE_AFFECTS_JUDGMENT = "subject_type_affects_judgment"
    CONTACT_TYPE_AFFECTS_JUDGMENT = "contact_type_affects_judgment"
    OPT_OUT_DEADLINE_DAYS = "opt_out_deadline_days"
    LOCAL_REPRESENTATIVE_REQUIRED = "local_representative_required"
    REQUIREMENTS = "requirements"


DECISION_FIELDS = frozenset(CountryPolicyField)


class CountryPolicyAction(str, Enum):
    PUBLIC_RESEARCH = "public_research"
    CONTACT_ENRICHMENT = "contact_enrichment"
    COLD_B2B_EMAIL = "cold_b2b_email"


class CountryPolicyFieldSourceInput(BaseModel):
    """客户端可提交的最小安全来源；身份与时间均由服务端绑定。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    source_type: CountryPolicySourceType
    source_id: str
    source_url: str | None = None
    page_hash: str | None = None

    @field_validator("source_id")
    @classmethod
    def validate_source_id(cls, value: str) -> str:
        if _SOURCE_ID_PATTERN.fullmatch(value) is None:
            raise ValueError("source_id 必须是安全且有界的内部引用")
        return value

    @model_validator(mode="after")
    def validate_source_shape(self) -> CountryPolicyFieldSourceInput:
        if self.source_type not in _TRUSTED_SOURCE_TYPES:
            raise ValueError("国家政策字段只接受人工可信来源")
        if self.source_type is SourceType.WEB_PAGE:
            if self.source_url is None or self.page_hash is None:
                raise ValueError("WEB_PAGE 必须提供 HTTPS URL 与页面哈希")
            if len(self.source_url) > 2_048 or _has_control(self.source_url):
                raise ValueError("source_url 无效")
            parsed = urlsplit(self.source_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("WEB_PAGE URL 必须是无 userinfo 的 HTTPS URL")
            _valid_hash(self.page_hash, field_name="page_hash")
        elif self.source_url is not None or self.page_hash is not None:
            raise ValueError("非网页来源不得携带 source_url 或 page_hash")
        return self


class CountryPolicyProposalCreate(BaseModel):
    """不携带租户、身份、时间或幂等键的国家政策候选内容。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    country: str
    public_research_allowed: bool
    contact_enrichment_allowed: bool
    cold_b2b_email_allowed: bool
    personal_data_basis_required: bool
    subject_type_affects_judgment: bool
    contact_type_affects_judgment: bool
    opt_out_deadline_days: int | None
    local_representative_required: bool
    requirements: list[str] = Field(max_length=100)
    notes: str
    field_sources: dict[CountryPolicyField, CountryPolicyFieldSourceInput]

    @field_validator("country", mode="before")
    @classmethod
    def normalize_country_display(cls, value: object) -> str:
        return _normalized_text(value, field_name="country", maximum=64)

    @field_validator("opt_out_deadline_days")
    @classmethod
    def validate_opt_out_deadline(cls, value: int | None) -> int | None:
        if value is not None and (type(value) is not int or not 1 <= value <= 365):
            raise ValueError("opt_out_deadline_days 必须是 1..365 的整数或 null")
        return value

    @field_validator("requirements")
    @classmethod
    def normalize_requirements(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            requirement = _normalized_text(
                value, field_name="requirements", maximum=128
            ).casefold()
            if _REQUIREMENT_PATTERN.fullmatch(requirement) is None:
                raise ValueError("requirements 只能包含固定 action code")
            normalized.append(requirement)
        if len(set(normalized)) != len(normalized):
            raise ValueError("requirements 规范化后不得重复")
        return sorted(normalized)

    @field_validator("notes", mode="before")
    @classmethod
    def normalize_notes(cls, value: object) -> str:
        return _normalized_text(value, field_name="notes", maximum=4_000)

    @model_validator(mode="after")
    def require_exact_field_sources(self) -> CountryPolicyProposalCreate:
        if set(self.field_sources) != DECISION_FIELDS:
            raise ValueError("每个决策字段必须且只能提供一条来源")
        return self


class _FrozenModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class CountryPolicyVersionView(_FrozenModel):
    country_policy_version_id: CountryPolicyVersionId
    country: str
    country_key: str
    version_number: int = Field(ge=1)
    content_hash: str
    base_version_id: CountryPolicyVersionId | None
    base_content_hash: str | None
    public_research_allowed: bool
    contact_enrichment_allowed: bool
    cold_b2b_email_allowed: bool
    personal_data_basis_required: bool
    subject_type_affects_judgment: bool
    contact_type_affects_judgment: bool
    opt_out_deadline_days: int | None
    local_representative_required: bool
    requirements: tuple[str, ...]
    notes: str
    field_provenance: dict[CountryPolicyField, Provenance]
    proposed_by: EmployeeId
    proposed_at: datetime
    change_set_ref: str

    @model_validator(mode="after")
    def validate_version_facts(self) -> CountryPolicyVersionView:
        normalize_country_key(self.country_key)
        if self.country_key != normalize_country_key(self.country):
            raise ValueError("country_key 与 country 不一致")
        _valid_hash(self.content_hash, field_name="content_hash")
        if (self.base_version_id is None) != (self.base_content_hash is None):
            raise ValueError("base version/hash 必须同时存在或为空")
        if self.base_content_hash is not None:
            _valid_hash(self.base_content_hash, field_name="base_content_hash")
        if set(self.field_provenance) != DECISION_FIELDS:
            raise ValueError("字段 Provenance 必须完整")
        if any(
            provenance.source_type not in _TRUSTED_SOURCE_TYPES
            or not provenance.is_human_confirmed
            for provenance in self.field_provenance.values()
        ):
            raise ValueError("字段 Provenance 必须来自可信来源并经过人工确认")
        _aware_utc(self.proposed_at, field_name="proposed_at")
        expected_ref = (
            f"country_policy:{self.country_policy_version_id}:{self.content_hash}"
        )
        if self.change_set_ref != expected_ref:
            raise ValueError("change_set_ref 与候选版本不一致")
        return self


class CountryPolicyDecision(_FrozenModel):
    country_key: str
    action: CountryPolicyAction
    configured: bool
    allowed: bool
    active_version_id: CountryPolicyVersionId | None
    content_hash: str | None
    requirements: tuple[str, ...]

    @field_validator("active_version_id")
    @classmethod
    def validate_active_version_id(
        cls, value: CountryPolicyVersionId | None
    ) -> CountryPolicyVersionId | None:
        if value is not None and (
            not isinstance(value, str)
            or _COUNTRY_POLICY_VERSION_ID_PATTERN.fullmatch(value) is None
        ):
            raise ValueError("active_version_id 必须是规范的国家政策版本 ID")
        return value

    @model_validator(mode="after")
    def validate_decision_facts(self) -> CountryPolicyDecision:
        if self.country_key != normalize_country_key(self.country_key):
            raise ValueError("country_key 必须已经精确规范化")
        if (self.active_version_id is None) != (self.content_hash is None):
            raise ValueError("active_version_id 与 content_hash 必须成对")
        if self.content_hash is not None:
            _valid_hash(self.content_hash, field_name="content_hash")
        if not self.configured and (
            self.allowed
            or self.active_version_id is not None
            or self.content_hash is not None
            or self.requirements
        ):
            raise ValueError("未配置政策不得携带放行或 active facts")
        if self.configured and self.active_version_id is None:
            raise ValueError("已配置政策必须携带 active facts")
        return self


class CountryPolicyCoverage(_FrozenModel):
    active_policy_count: int = Field(ge=0)
    contact_enrichment_allowed_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_subset(self) -> CountryPolicyCoverage:
        if self.contact_enrichment_allowed_count > self.active_policy_count:
            raise ValueError("允许联系人补全的政策数不能超过生效政策数")
        return self


class CountryPolicyApprovalFact(_FrozenModel):
    approval_id: ApprovalId
    approval_type: str
    change_set_ref: str
    decided_by: EmployeeId
    decided_at: datetime

    @field_validator("decided_at")
    @classmethod
    def validate_decided_at(cls, value: datetime) -> datetime:
        return _aware_utc(value, field_name="decided_at")


class CountryPolicyChangeSnapshot(_FrozenModel):
    base: CountryPolicyVersionView | None
    current: CountryPolicyVersionView | None
    candidate: CountryPolicyVersionView
    base_is_current: bool


class CountryPolicyProposalResult(_FrozenModel):
    country_policy_version_id: CountryPolicyVersionId
    country_key: str
    version_number: int = Field(ge=1)
    content_hash: str
    change_set_ref: str


class CountryPolicyActivationView(_FrozenModel):
    activation_id: CountryPolicyActivationId
    activation_sequence: int = Field(ge=1)
    country_key: str
    country_policy_version_id: CountryPolicyVersionId
    content_hash: str
    approval_id: ApprovalId
    change_set_ref: str
    approved_by: EmployeeId
    approved_at: datetime
    activated_by: str
    activated_at: datetime

    @model_validator(mode="after")
    def validate_activation_facts(self) -> CountryPolicyActivationView:
        if self.country_key != normalize_country_key(self.country_key):
            raise ValueError("country_key 必须已经精确规范化")
        _valid_hash(self.content_hash, field_name="content_hash")
        approved_at = _aware_utc(self.approved_at, field_name="approved_at")
        activated_at = _aware_utc(self.activated_at, field_name="activated_at")
        if approved_at > activated_at:
            raise ValueError("approved_at 不能晚于 activated_at")
        expected_ref = (
            f"country_policy:{self.country_policy_version_id}:{self.content_hash}"
        )
        if self.change_set_ref != expected_ref:
            raise ValueError("change_set_ref 与激活版本不一致")
        return self


__all__ = (
    "CountryPolicyAction",
    "CountryPolicyActivationView",
    "CountryPolicyApprovalFact",
    "CountryPolicyChangeSnapshot",
    "CountryPolicyCoverage",
    "CountryPolicyDecision",
    "CountryPolicyField",
    "CountryPolicyFieldSourceInput",
    "CountryPolicyProposalCreate",
    "CountryPolicyProposalResult",
    "CountryPolicySourceType",
    "CountryPolicyVersionView",
    "normalize_country_key",
)
