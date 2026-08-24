"""国家政策候选版本与激活事实；两类实体均不可变。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from domains.compliance.schemas import (
    DECISION_FIELDS,
    CountryPolicyActivationView,
    CountryPolicyField,
    CountryPolicyProposalCreate,
    CountryPolicyVersionView,
    normalize_country_key,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ApprovalId,
    CountryPolicyActivationId,
    CountryPolicyVersionId,
    EmployeeId,
    IdempotencyKey,
    TenantId,
)
from shared.schemas.provenance import Provenance

_LOWER_HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def _bounded_text(value: object, *, field_name: str, maximum: int = 200) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError(f"{field_name} 无效")
    return value


def _require_prefixed(value: object, *, field_name: str, prefix: str) -> None:
    text = _bounded_text(value, field_name=field_name)
    if not text.startswith(f"{prefix}_"):
        raise ValidationError(f"{field_name} 必须使用 {prefix}_ 前缀")


def _require_hash(value: str | None, *, field_name: str) -> None:
    if value is not None and _LOWER_HASH_PATTERN.fullmatch(value) is None:
        raise ValidationError(f"{field_name} 必须是 64 位小写 SHA-256")


def _require_utc(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValidationError(f"{field_name} 必须是 UTC 时间")


def _canonical_content(command: CountryPolicyProposalCreate) -> dict[str, object]:
    return {
        "cold_b2b_email_allowed": command.cold_b2b_email_allowed,
        "contact_enrichment_allowed": command.contact_enrichment_allowed,
        "country": command.country,
        "country_key": normalize_country_key(command.country),
        "field_sources": {
            field.value: {
                "page_hash": command.field_sources[field].page_hash,
                "source_id": command.field_sources[field].source_id,
                "source_type": command.field_sources[field].source_type.value,
                "source_url": command.field_sources[field].source_url,
            }
            for field in sorted(DECISION_FIELDS, key=lambda item: item.value)
        },
        "local_representative_required": command.local_representative_required,
        "notes": command.notes,
        "opt_out_deadline_days": command.opt_out_deadline_days,
        "personal_data_basis_required": command.personal_data_basis_required,
        "public_research_allowed": command.public_research_allowed,
        "requirements": list(command.requirements),
        "subject_type_affects_judgment": command.subject_type_affects_judgment,
        "contact_type_affects_judgment": command.contact_type_affects_judgment,
    }


def _change_set_ref(version_id: CountryPolicyVersionId, content_hash: str) -> str:
    return f"country_policy:{version_id}:{content_hash}"


@dataclass(frozen=True)
class CountryPolicyVersion:
    """一次不可变的人工作业候选；审批不会回写该事实。"""

    tenant_id: TenantId
    country_policy_version_id: CountryPolicyVersionId
    country: str
    country_key: str
    version_number: int
    content_hash: str
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
    field_provenance: Mapping[CountryPolicyField, Provenance]
    proposed_by: EmployeeId
    proposed_at: datetime
    idempotency_key: IdempotencyKey
    base_version_id: CountryPolicyVersionId | None = None
    base_content_hash: str | None = None

    def __post_init__(self) -> None:
        _bounded_text(self.tenant_id, field_name="tenant_id")
        _require_prefixed(
            self.country_policy_version_id,
            field_name="country_policy_version_id",
            prefix="cpp",
        )
        if self.country_key != normalize_country_key(self.country):
            raise ValidationError("country_key 与 country 不一致")
        if type(self.version_number) is not int or self.version_number < 1:
            raise ValidationError("国家政策版本号必须是正整数")
        _require_hash(self.content_hash, field_name="content_hash")
        if (self.base_version_id is None) != (self.base_content_hash is None):
            raise ValidationError("base version/hash 必须同时存在或为空")
        if self.base_version_id is not None:
            _require_prefixed(
                self.base_version_id, field_name="base_version_id", prefix="cpp"
            )
        _require_hash(self.base_content_hash, field_name="base_content_hash")
        _bounded_text(self.proposed_by, field_name="proposed_by")
        _bounded_text(self.idempotency_key, field_name="idempotency_key")
        _require_utc(self.proposed_at, field_name="proposed_at")
        if set(self.field_provenance) != DECISION_FIELDS:
            raise ValidationError("国家政策字段 Provenance 不完整")
        for provenance in self.field_provenance.values():
            if (
                provenance.extracted_by != f"human:{self.proposed_by}"
                or provenance.extracted_at != self.proposed_at
                or provenance.confirmed_by != self.proposed_by
                or provenance.confirmed_at != self.proposed_at
                or not provenance.is_human_confirmed
            ):
                raise ValidationError("国家政策字段 Provenance 与提交事实不一致")
        object.__setattr__(
            self, "field_provenance", MappingProxyType(dict(self.field_provenance))
        )

    @classmethod
    def content_hash_for(cls, command: CountryPolicyProposalCreate) -> str:
        """对规范化业务内容与安全来源输入做确定性 SHA-256。"""
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
        version_id: CountryPolicyVersionId,
        version_number: int,
        command: CountryPolicyProposalCreate,
        base_version_id: CountryPolicyVersionId | None,
        base_content_hash: str | None,
        proposed_by: EmployeeId,
        proposed_at: datetime,
        idempotency_key: IdempotencyKey,
    ) -> CountryPolicyVersion:
        if proposed_at.tzinfo is None or proposed_at.utcoffset() is None:
            raise ValidationError("proposed_at 必须是带时区时间")
        normalized_time = proposed_at.astimezone(UTC)
        field_provenance = {
            field: Provenance(
                source_type=source.source_type,
                source_id=source.source_id,
                source_url=source.source_url,
                page_hash=source.page_hash,
                extracted_by=f"human:{proposed_by}",
                extracted_at=normalized_time,
                confirmed_by=proposed_by,
                confirmed_at=normalized_time,
            )
            for field, source in command.field_sources.items()
        }
        return cls(
            tenant_id=tenant_id,
            country_policy_version_id=version_id,
            country=command.country,
            country_key=normalize_country_key(command.country),
            version_number=version_number,
            content_hash=cls.content_hash_for(command),
            public_research_allowed=command.public_research_allowed,
            contact_enrichment_allowed=command.contact_enrichment_allowed,
            cold_b2b_email_allowed=command.cold_b2b_email_allowed,
            personal_data_basis_required=command.personal_data_basis_required,
            subject_type_affects_judgment=command.subject_type_affects_judgment,
            contact_type_affects_judgment=command.contact_type_affects_judgment,
            opt_out_deadline_days=command.opt_out_deadline_days,
            local_representative_required=command.local_representative_required,
            requirements=tuple(command.requirements),
            notes=command.notes,
            field_provenance=field_provenance,
            proposed_by=proposed_by,
            proposed_at=normalized_time,
            idempotency_key=idempotency_key,
            base_version_id=base_version_id,
            base_content_hash=base_content_hash,
        )

    @property
    def change_set_ref(self) -> str:
        return _change_set_ref(self.country_policy_version_id, self.content_hash)

    def to_view(self) -> CountryPolicyVersionView:
        return CountryPolicyVersionView(
            country_policy_version_id=self.country_policy_version_id,
            country=self.country,
            country_key=self.country_key,
            version_number=self.version_number,
            content_hash=self.content_hash,
            base_version_id=self.base_version_id,
            base_content_hash=self.base_content_hash,
            public_research_allowed=self.public_research_allowed,
            contact_enrichment_allowed=self.contact_enrichment_allowed,
            cold_b2b_email_allowed=self.cold_b2b_email_allowed,
            personal_data_basis_required=self.personal_data_basis_required,
            subject_type_affects_judgment=self.subject_type_affects_judgment,
            contact_type_affects_judgment=self.contact_type_affects_judgment,
            opt_out_deadline_days=self.opt_out_deadline_days,
            local_representative_required=self.local_representative_required,
            requirements=self.requirements,
            notes=self.notes,
            field_provenance=dict(self.field_provenance),
            proposed_by=self.proposed_by,
            proposed_at=self.proposed_at,
            change_set_ref=self.change_set_ref,
        )


@dataclass(frozen=True)
class CountryPolicyActivation:
    """审批事实与系统应用事实分离的 append-only 激活记录。"""

    tenant_id: TenantId
    activation_id: CountryPolicyActivationId
    activation_sequence: int
    country_key: str
    country_policy_version_id: CountryPolicyVersionId
    content_hash: str
    approval_id: ApprovalId
    change_set_ref: str
    approved_by: EmployeeId
    approved_at: datetime
    activated_by: str
    activated_at: datetime

    def __post_init__(self) -> None:
        _bounded_text(self.tenant_id, field_name="tenant_id")
        _require_prefixed(self.activation_id, field_name="activation_id", prefix="cpa")
        _require_prefixed(
            self.country_policy_version_id,
            field_name="country_policy_version_id",
            prefix="cpp",
        )
        _require_prefixed(self.approval_id, field_name="approval_id", prefix="apr")
        if type(self.activation_sequence) is not int or self.activation_sequence < 1:
            raise ValidationError("activation_sequence 必须是正整数")
        if self.country_key != normalize_country_key(self.country_key):
            raise ValidationError("country_key 必须已经精确规范化")
        _require_hash(self.content_hash, field_name="content_hash")
        _bounded_text(self.approved_by, field_name="approved_by")
        _bounded_text(self.activated_by, field_name="activated_by")
        _require_utc(self.approved_at, field_name="approved_at")
        _require_utc(self.activated_at, field_name="activated_at")
        if self.approved_at > self.activated_at:
            raise ValidationError("approved_at 不能晚于 activated_at")
        if self.change_set_ref != _change_set_ref(
            self.country_policy_version_id, self.content_hash
        ):
            raise ValidationError("国家政策激活 change_set_ref 不匹配")

    def to_view(self) -> CountryPolicyActivationView:
        return CountryPolicyActivationView(
            activation_id=self.activation_id,
            activation_sequence=self.activation_sequence,
            country_key=self.country_key,
            country_policy_version_id=self.country_policy_version_id,
            content_hash=self.content_hash,
            approval_id=self.approval_id,
            change_set_ref=self.change_set_ref,
            approved_by=self.approved_by,
            approved_at=self.approved_at,
            activated_by=self.activated_by,
            activated_at=self.activated_at,
        )


__all__ = ("CountryPolicyActivation", "CountryPolicyVersion")
