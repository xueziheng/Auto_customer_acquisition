"""潜客域实体。（浅域：验证状态与法律依据写全，其余骨架）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum

from domains.prospecting.errors import MissingAssessmentRefError
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ContactPointId,
    ProspectAccountId,
    ProspectContactId,
    TenantId,
)


class VerificationStatus(str, Enum):
    """联系方式可达性验证状态（硬边界 6）。"""

    UNVERIFIED = "unverified"
    VERIFIED = "verified"
    """唯一允许进序列的状态。"""

    RISKY = "risky"
    """验证服务返回"接受所有邮件"或类似模糊结果。**按不可用处理**：
    risky 地址的实际退信率不可预测，赌它可用的代价是域名信誉。"""

    INVALID = "invalid"


class ContactPointKind(str, Enum):
    """Phase 1 支持的联系方式类型；社交渠道明确不在范围内。"""

    EMAIL = "email"
    PHONE = "phone"


class LegalBasisType(str, Enum):
    LEGITIMATE_INTEREST = "legitimate_interest"
    """正当利益。B2B 冷触达的常用依据，**必须指向已完成的评估记录**
    （``assessment_ref``）——监管问询时要能拿出来，
    「我们以为可以」不是答案。"""

    CONSENT = "consent"
    EXISTING_CUSTOMER = "existing_customer"


class SubjectType(str, Enum):
    """收件主体类型。欧洲和英国的规则按这个分叉：法人、独资经营者、
    自然人适用不同要求，职务邮箱与个人邮箱也不同。"""

    LEGAL_ENTITY = "legal_entity"
    SOLE_TRADER = "sole_trader"
    NATURAL_PERSON = "natural_person"


class ContactType(str, Enum):
    ROLE_BASED = "role_based"
    """职务邮箱（info@、sales@）。个人数据属性最弱。"""

    PERSONAL_BUSINESS = "personal_business"
    """具名业务邮箱（john.smith@company.com）。**是个人数据**，
    即使来自公开网站——公开不等于可以随意处理。"""


@dataclass(frozen=True)
class LegalBasisRecord:
    """处理依据留痕。每个联系方式一条，Phase 1 就要有。

    字段：
        basis, subject_type, contact_type
        source:          数据来自哪（company_website / provider 名）
        source_url
        collected_at
        assessment_ref:  正当利益评估的引用（basis 为 LI 时必填）
    """

    basis: LegalBasisType
    subject_type: SubjectType
    contact_type: ContactType
    source: str
    collected_at: datetime
    source_url: str | None = None
    assessment_ref: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.source, "法律依据字段无效")
        _require_optional_text(self.source_url, "法律依据字段无效")
        _require_optional_text(self.assessment_ref, "法律依据字段无效")
        _require_utc(self.collected_at, "法律依据时间必须为 UTC")
        if (
            self.basis is LegalBasisType.LEGITIMATE_INTEREST
            and self.assessment_ref is None
        ):
            raise MissingAssessmentRefError("正当利益依据缺少评估引用")


@dataclass
class ProspectAccount:
    """潜在企业。

    ``website_domain`` 是消歧主键：同一家公司会以「Acme Mfg」和
    「Acme Manufacturing Inc.」两种写法出现，不消歧就会建两个
    account、分给两个员工、发两套邮件。域名相同即同一企业。
    """

    account_id: ProspectAccountId
    tenant_id: TenantId
    name: str
    country: str
    created_at: datetime
    website_domain: str | None = None
    entity_type: str | None = None
    industry: str | None = None
    size_hint: str | None = None
    source_signal_refs: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        _require_text(str(self.account_id), "潜在企业字段无效")
        _require_text(str(self.tenant_id), "潜在企业字段无效")
        _require_text(self.name, "潜在企业字段无效")
        _require_text(self.country, "潜在企业字段无效")
        _require_optional_text(self.website_domain, "潜在企业字段无效")
        _require_optional_text(self.entity_type, "潜在企业字段无效")
        _require_optional_text(self.industry, "潜在企业字段无效")
        _require_optional_text(self.size_hint, "潜在企业字段无效")
        _require_utc(self.created_at, "潜在企业时间必须为 UTC")
        if any(not _is_exact_nonblank(item) for item in self.source_signal_refs):
            raise ValidationError("潜在企业来源引用无效")


@dataclass
class ProspectContact:
    """潜在联系人（人）。"""

    contact_id: ProspectContactId
    tenant_id: TenantId
    account_id: ProspectAccountId
    created_at: datetime
    full_name: str | None = None
    role_title: str | None = None
    language: str | None = None

    def __post_init__(self) -> None:
        _require_text(str(self.contact_id), "潜在联系人字段无效")
        _require_text(str(self.tenant_id), "潜在联系人字段无效")
        _require_text(str(self.account_id), "潜在联系人字段无效")
        _require_optional_text(self.full_name, "潜在联系人字段无效")
        _require_optional_text(self.role_title, "潜在联系人字段无效")
        _require_optional_text(self.language, "潜在联系人字段无效")
        _require_utc(self.created_at, "潜在联系人时间必须为 UTC")


@dataclass
class ContactPoint:
    """联系方式（邮箱/电话）。验证与法律依据都挂在这一级。

    字段：
        contact_point_id, tenant_id, contact_id
        kind:            email / phone
        value
        verification:    验证状态
        verified_at
        verification_provider
        verification_checked_at
        verification_cost_note
        legal_basis:     处理依据（**必填**，没有依据的联系方式
                         不允许入库——入库即处理）
        enrichment_cost_note: 获取成本记录
    """

    contact_point_id: ContactPointId
    tenant_id: TenantId
    contact_id: ProspectContactId
    kind: ContactPointKind
    value: str
    value_hash: str
    legal_basis: LegalBasisRecord
    created_at: datetime
    verification: VerificationStatus = VerificationStatus.UNVERIFIED
    verified_at: datetime | None = None
    verification_provider: str | None = None
    verification_checked_at: datetime | None = None
    verification_cost_note: str | None = None
    enrichment_cost_note: str | None = None

    def may_enter_sequence(self) -> bool:
        """能否进序列。仅 ``VERIFIED``。RISKY 不行——赌它可用的
        代价是域名信誉（硬边界 6）。"""
        return self.verification is VerificationStatus.VERIFIED

    def __post_init__(self) -> None:
        _require_text(str(self.contact_point_id), "联系方式字段无效")
        _require_text(str(self.tenant_id), "联系方式字段无效")
        _require_text(str(self.contact_id), "联系方式字段无效")
        if not isinstance(self.kind, ContactPointKind):
            raise ValidationError("联系方式类型无效")
        _require_text(self.value, "联系方式字段无效")
        if re.fullmatch(r"[0-9a-f]{64}", self.value_hash) is None:
            raise ValidationError("联系方式指纹无效")
        _require_utc(self.created_at, "联系方式时间必须为 UTC")
        _require_optional_text(
            self.verification_provider, "联系方式验证状态无效"
        )
        _require_optional_text(
            self.verification_cost_note, "联系方式验证观察无效"
        )
        _require_optional_text(self.enrichment_cost_note, "联系方式字段无效")
        observed = self.verification_checked_at is not None
        complete_observation = (
            self.verification_provider is not None
            and self.verification_cost_note is not None
        )
        if observed != complete_observation or (
            not observed and self.verification_cost_note is not None
        ):
            raise ValidationError("联系方式验证观察无效")
        if self.verification_checked_at is not None:
            _require_utc(
                self.verification_checked_at, "联系方式验证观察无效"
            )
        if self.verification is VerificationStatus.UNVERIFIED:
            valid_shape = (
                self.verified_at is None
                and (
                    self.verification_provider is None
                    or self.verification_checked_at is not None
                )
            )
        elif self.verification is VerificationStatus.VERIFIED:
            valid_shape = (
                self.verified_at is not None
                and self.verification_provider is not None
                and _is_utc(self.verified_at)
            )
        else:
            valid_shape = (
                self.verified_at is None and self.verification_provider is not None
            )
        if not valid_shape:
            raise ValidationError("联系方式验证状态无效")


def _is_exact_nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _require_text(value: object, message: str) -> None:
    if not _is_exact_nonblank(value):
        raise ValidationError(message)


def _require_optional_text(value: str | None, message: str) -> None:
    if value is not None:
        _require_text(value, message)


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0)


def _require_utc(value: datetime, message: str) -> None:
    if not _is_utc(value):
        raise ValidationError(message)
