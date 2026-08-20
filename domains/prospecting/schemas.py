"""潜客域对外 DTO；调用方不得导入本域内部实体。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from domains.prospecting.models import (
    ContactPointKind,
    ContactType,
    LegalBasisType,
    SubjectType,
    VerificationStatus,
)
from shared.schemas.identifiers import (
    ContactPointId,
    ProspectAccountId,
    ProspectContactId,
    TenantId,
)

__all__ = (
    "AccountResolveRequest",
    "ContactCreateRequest",
    "ContactPointCreateRequest",
    "ContactPointKind",
    "ContactPointView",
    "ContactType",
    "LegalBasisInput",
    "LegalBasisType",
    "ProspectAccountView",
    "ProspectContactView",
    "SubjectType",
    "VerificationStatus",
)


@dataclass(frozen=True)
class AccountResolveRequest:
    entity_name: str
    country: str
    website_domain: str | None = None
    entity_type: str | None = None
    industry: str | None = None
    size_hint: str | None = None
    source_signal_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ContactCreateRequest:
    account_id: ProspectAccountId
    full_name: str | None = None
    role_title: str | None = None
    language: str | None = None


@dataclass(frozen=True)
class LegalBasisInput:
    basis: LegalBasisType
    subject_type: SubjectType
    contact_type: ContactType
    source: str
    collected_at: datetime
    source_url: str | None = None
    assessment_ref: str | None = None


@dataclass(frozen=True)
class ContactPointCreateRequest:
    contact_id: ProspectContactId
    kind: ContactPointKind
    value: str
    legal_basis: LegalBasisInput
    enrichment_cost_note: str | None = None


@dataclass(frozen=True)
class ProspectAccountView:
    account_id: ProspectAccountId
    tenant_id: TenantId
    name: str
    country: str
    created_at: datetime
    website_domain: str | None = None
    entity_type: str | None = None
    industry: str | None = None
    size_hint: str | None = None
    source_signal_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProspectContactView:
    contact_id: ProspectContactId
    tenant_id: TenantId
    account_id: ProspectAccountId
    created_at: datetime
    full_name: str | None = None
    role_title: str | None = None
    language: str | None = None


@dataclass(frozen=True)
class ContactPointView:
    contact_point_id: ContactPointId
    tenant_id: TenantId
    contact_id: ProspectContactId
    account_id: ProspectAccountId
    kind: ContactPointKind
    value: str
    verification: VerificationStatus
    created_at: datetime
    verified_at: datetime | None = None
    verification_provider: str | None = None
    enrichment_cost_note: str | None = None
