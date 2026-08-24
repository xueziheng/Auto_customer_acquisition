"""组织域实体；版本内容与激活事实均不可变。"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from domains.organization.schemas import (
    PlaybookActivationView,
    PlaybookProposalCreate,
    PlaybookVersionView,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ApprovalId,
    EmployeeId,
    IdempotencyKey,
    PlaybookActivationId,
    PlaybookVersionId,
    TenantId,
)
from shared.schemas.money import CurrencyCode, Money
from shared.schemas.provenance import Provenance, SourceType


def _is_blank(value: object) -> bool:
    return not isinstance(value, str) or not value.strip()


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError(f"{field_name} 必须是带时区时间")


def _require_hash(value: str | None, field_name: str) -> None:
    if value is None:
        return
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValidationError(f"{field_name} 必须是小写 SHA-256")


def _canonical_decimal(amount: Decimal) -> str:
    """稳定序列化 Decimal；去无意义零，并把正负零统一为 ``0``。"""
    if amount.is_zero():
        return "0"
    rendered = format(amount, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _change_set_ref(version_id: PlaybookVersionId, content_hash: str) -> str:
    return f"playbook:{version_id}:{content_hash}"


def _canonical_content(command: PlaybookProposalCreate) -> dict[str, object]:
    return {
        "approval_requirements": list(command.approval_requirements),
        "company_type": command.company_type,
        "excluded_categories": list(command.excluded_categories),
        "excluded_countries": list(command.excluded_countries),
        "minimum_deal_amount": _canonical_decimal(command.minimum_deal_amount),
        "minimum_deal_currency": command.minimum_deal_currency,
        "monthly_budget_credits": command.monthly_budget_credits,
        "sourcing_regions": list(command.sourcing_regions),
        "supply_capabilities_note": command.supply_capabilities_note,
    }


@dataclass(frozen=True)
class Tenant:
    """租户。Phase 1 单租户运行，但实体仍显式携带租户边界。"""

    tenant_id: TenantId
    name: str
    created_at: datetime
    is_active: bool = True


@dataclass(frozen=True)
class CompanyPlaybookVersion:
    """一次不可变的人工作业版本；批准不会回写该事实。"""

    tenant_id: TenantId
    playbook_version_id: PlaybookVersionId
    version_number: int
    content_hash: str
    company_type: str
    minimum_deal_value: Money
    proposed_by: EmployeeId
    proposed_at: datetime
    idempotency_key: IdempotencyKey
    content_provenance: Provenance
    base_version_id: PlaybookVersionId | None = None
    base_content_hash: str | None = None
    excluded_categories: tuple[str, ...] = ()
    sourcing_regions: tuple[str, ...] = ()
    excluded_countries: tuple[str, ...] = ()
    monthly_budget_credits: int | None = None
    approval_requirements: tuple[str, ...] = ()
    supply_capabilities_note: str | None = None

    def __post_init__(self) -> None:
        if _is_blank(self.tenant_id) or _is_blank(self.playbook_version_id):
            raise ValidationError("Playbook 版本租户和版本 ID 不能为空")
        if isinstance(self.version_number, bool) or self.version_number < 1:
            raise ValidationError("Playbook 版本号必须是正整数")
        _require_hash(self.content_hash, "content_hash")
        if (self.base_version_id is None) != (self.base_content_hash is None):
            raise ValidationError("base_version_id 与 base_content_hash 必须成对")
        _require_hash(self.base_content_hash, "base_content_hash")
        if _is_blank(self.proposed_by) or _is_blank(self.idempotency_key):
            raise ValidationError("Playbook 提交人和幂等键不能为空")
        _require_aware(self.proposed_at, "proposed_at")
        expected = Provenance(
            source_type=SourceType.EMPLOYEE_INPUT,
            source_id=str(self.playbook_version_id),
            extracted_by=f"human:{self.proposed_by}",
            extracted_at=self.proposed_at,
        )
        if self.content_provenance != expected:
            raise ValidationError("Playbook 内容 Provenance 与版本提交事实不一致")

    @classmethod
    def content_hash_for(cls, command: PlaybookProposalCreate) -> str:
        canonical_json = json.dumps(
            _canonical_content(command),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()

    @classmethod
    def from_command(
        cls,
        *,
        tenant_id: TenantId,
        version_id: PlaybookVersionId,
        version_number: int,
        command: PlaybookProposalCreate,
        base_version_id: PlaybookVersionId | None,
        base_content_hash: str | None,
        proposed_by: EmployeeId,
        proposed_at: datetime,
        idempotency_key: IdempotencyKey,
    ) -> CompanyPlaybookVersion:
        _require_aware(proposed_at, "proposed_at")
        normalized_proposed_at = proposed_at.astimezone(UTC)
        provenance = Provenance(
            source_type=SourceType.EMPLOYEE_INPUT,
            source_id=str(version_id),
            extracted_by=f"human:{proposed_by}",
            extracted_at=normalized_proposed_at,
        )
        return cls(
            tenant_id=tenant_id,
            playbook_version_id=version_id,
            version_number=version_number,
            content_hash=cls.content_hash_for(command),
            company_type=command.company_type,
            minimum_deal_value=Money(
                command.minimum_deal_amount,
                CurrencyCode(command.minimum_deal_currency),
            ),
            proposed_by=proposed_by,
            proposed_at=normalized_proposed_at,
            idempotency_key=idempotency_key,
            content_provenance=provenance,
            base_version_id=base_version_id,
            base_content_hash=base_content_hash,
            excluded_categories=tuple(command.excluded_categories),
            sourcing_regions=tuple(command.sourcing_regions),
            excluded_countries=tuple(command.excluded_countries),
            monthly_budget_credits=command.monthly_budget_credits,
            approval_requirements=tuple(command.approval_requirements),
            supply_capabilities_note=command.supply_capabilities_note,
        )

    @property
    def change_set_ref(self) -> str:
        return _change_set_ref(self.playbook_version_id, self.content_hash)

    def to_view(self) -> PlaybookVersionView:
        return PlaybookVersionView(
            playbook_version_id=self.playbook_version_id,
            version_number=self.version_number,
            content_hash=self.content_hash,
            base_version_id=self.base_version_id,
            base_content_hash=self.base_content_hash,
            company_type=self.company_type,
            minimum_deal_amount=_canonical_decimal(self.minimum_deal_value.amount),
            minimum_deal_currency=str(self.minimum_deal_value.currency),
            excluded_categories=self.excluded_categories,
            sourcing_regions=self.sourcing_regions,
            excluded_countries=self.excluded_countries,
            monthly_budget_credits=self.monthly_budget_credits,
            approval_requirements=self.approval_requirements,
            supply_capabilities_note=self.supply_capabilities_note,
            proposed_by=self.proposed_by,
            proposed_at=self.proposed_at,
            content_provenance=self.content_provenance,
            change_set_ref=self.change_set_ref,
        )


@dataclass(frozen=True)
class PlaybookActivation:
    """审批确认和系统应用时间分离的 append-only 激活事实。"""

    tenant_id: TenantId
    activation_id: PlaybookActivationId
    playbook_version_id: PlaybookVersionId
    content_hash: str
    approval_id: ApprovalId
    change_set_ref: str
    approved_by: EmployeeId
    approved_at: datetime
    activated_by: str
    activated_at: datetime

    def __post_init__(self) -> None:
        for value in (
            self.tenant_id,
            self.activation_id,
            self.playbook_version_id,
            self.approval_id,
            self.change_set_ref,
            self.approved_by,
            self.activated_by,
        ):
            if _is_blank(value):
                raise ValidationError("Playbook 激活事实包含空标识")
        _require_hash(self.content_hash, "content_hash")
        _require_aware(self.approved_at, "approved_at")
        _require_aware(self.activated_at, "activated_at")
        if self.approved_at > self.activated_at:
            raise ValidationError("approved_at 不能晚于 activated_at")
        if self.change_set_ref != _change_set_ref(
            self.playbook_version_id, self.content_hash
        ):
            raise ValidationError("Playbook 激活 change_set_ref 不匹配")

    def to_view(self) -> PlaybookActivationView:
        return PlaybookActivationView(
            activation_id=self.activation_id,
            playbook_version_id=self.playbook_version_id,
            content_hash=self.content_hash,
            approval_id=self.approval_id,
            change_set_ref=self.change_set_ref,
            approved_by=self.approved_by,
            approved_at=self.approved_at,
            activated_by=self.activated_by,
            activated_at=self.activated_at,
        )


@dataclass(frozen=True)
class CompanyPlaybook:
    """由不可变版本与激活事实组合出的当前生效只读模型。"""

    tenant_id: TenantId
    playbook_version_id: PlaybookVersionId
    activation_id: PlaybookActivationId
    version_number: int
    content_hash: str
    base_version_id: PlaybookVersionId | None
    base_content_hash: str | None
    approval_id: ApprovalId
    change_set_ref: str
    company_type: str
    minimum_deal_value: Money
    proposed_by: EmployeeId
    proposed_at: datetime
    approved_by: EmployeeId
    approved_at: datetime
    activated_by: str
    activated_at: datetime
    content_provenance: Provenance
    excluded_categories: tuple[str, ...] = ()
    sourcing_regions: tuple[str, ...] = ()
    excluded_countries: tuple[str, ...] = ()
    monthly_budget_credits: int | None = None
    approval_requirements: tuple[str, ...] = ()
    supply_capabilities_note: str | None = None

    @classmethod
    def from_facts(
        cls,
        version: CompanyPlaybookVersion,
        activation: PlaybookActivation,
    ) -> CompanyPlaybook:
        if (
            version.tenant_id != activation.tenant_id
            or version.playbook_version_id != activation.playbook_version_id
            or version.content_hash != activation.content_hash
        ):
            raise ValidationError("Playbook 版本与激活事实不匹配")
        confirmed_provenance = Provenance(
            source_type=version.content_provenance.source_type,
            source_id=version.content_provenance.source_id,
            extracted_by=version.content_provenance.extracted_by,
            extracted_at=version.content_provenance.extracted_at,
            confirmed_by=activation.approved_by,
            confirmed_at=activation.approved_at,
            source_url=version.content_provenance.source_url,
            page_hash=version.content_provenance.page_hash,
        )
        return cls(
            tenant_id=version.tenant_id,
            playbook_version_id=version.playbook_version_id,
            activation_id=activation.activation_id,
            version_number=version.version_number,
            content_hash=version.content_hash,
            base_version_id=version.base_version_id,
            base_content_hash=version.base_content_hash,
            approval_id=activation.approval_id,
            change_set_ref=activation.change_set_ref,
            company_type=version.company_type,
            minimum_deal_value=version.minimum_deal_value,
            proposed_by=version.proposed_by,
            proposed_at=version.proposed_at,
            approved_by=activation.approved_by,
            approved_at=activation.approved_at,
            activated_by=activation.activated_by,
            activated_at=activation.activated_at,
            content_provenance=confirmed_provenance,
            excluded_categories=version.excluded_categories,
            sourcing_regions=version.sourcing_regions,
            excluded_countries=version.excluded_countries,
            monthly_budget_credits=version.monthly_budget_credits,
            approval_requirements=version.approval_requirements,
            supply_capabilities_note=version.supply_capabilities_note,
        )

    def is_category_allowed(self, category: str) -> bool:
        """品类门禁保守匹配；空值和疑似命中均拒绝。"""
        normalized = _normalize_phrase(category)
        if not normalized:
            return False
        padded = f" {normalized} "
        for excluded in self.excluded_categories:
            normalized_excluded = _normalize_phrase(excluded)
            if not normalized_excluded:
                continue
            padded_excluded = f" {normalized_excluded} "
            if padded_excluded in padded or padded in padded_excluded:
                return False
        return True

    def is_country_allowed(self, country: str) -> bool:
        normalized = _normalize_phrase(country)
        if not normalized:
            return False
        return all(
            normalized != normalized_excluded
            for excluded in self.excluded_countries
            if (normalized_excluded := _normalize_phrase(excluded))
        )


def _normalize_phrase(value: str) -> str:
    if not isinstance(value, str):
        return ""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(
        "".join(
            character if character.isalnum() else " " for character in normalized
        ).split()
    )


__all__ = (
    "CompanyPlaybook",
    "CompanyPlaybookVersion",
    "PlaybookActivation",
    "Tenant",
)
