"""国家政策版本、字段来源与激活事实的 tenant-scoped 存储 Protocol。"""

from __future__ import annotations

from collections.abc import Mapping
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from domains.compliance.models import CountryPolicyActivation, CountryPolicyVersion
from domains.compliance.schemas import CountryPolicyField
from shared.events.bus import EventBus
from shared.schemas.identifiers import (
    ApprovalId,
    CountryPolicyVersionId,
    IdempotencyKey,
    TenantId,
)
from shared.schemas.provenance import Provenance


@runtime_checkable
class CountryPolicyVersionRepository(Protocol):
    async def lock_idempotency_key(
        self, tenant_id: TenantId, idempotency_key: IdempotencyKey
    ) -> None: ...

    async def lock_country(self, tenant_id: TenantId, country_key: str) -> None: ...

    async def add(
        self, tenant_id: TenantId, version: CountryPolicyVersion
    ) -> None: ...

    async def get(
        self, tenant_id: TenantId, version_id: CountryPolicyVersionId
    ) -> CountryPolicyVersion | None: ...

    async def find_by_idempotency_key(
        self, tenant_id: TenantId, idempotency_key: IdempotencyKey
    ) -> CountryPolicyVersion | None: ...

    async def next_version_number(
        self, tenant_id: TenantId, country_key: str
    ) -> int: ...

    async def list(
        self, tenant_id: TenantId, country_key: str, limit: int
    ) -> list[CountryPolicyVersion]: ...


@runtime_checkable
class CountryPolicyFieldProvenanceRepository(Protocol):
    async def add_for_version(
        self,
        tenant_id: TenantId,
        version_id: CountryPolicyVersionId,
        field_provenance: Mapping[CountryPolicyField, Provenance],
    ) -> None: ...

    async def list_for_version(
        self, tenant_id: TenantId, version_id: CountryPolicyVersionId
    ) -> dict[CountryPolicyField, Provenance]: ...


@runtime_checkable
class CountryPolicyActivationRepository(Protocol):
    async def get_current(
        self, tenant_id: TenantId, country_key: str
    ) -> CountryPolicyActivation | None: ...

    async def list_current(
        self, tenant_id: TenantId, limit: int
    ) -> list[CountryPolicyActivation]: ...

    async def get_by_version(
        self, tenant_id: TenantId, version_id: CountryPolicyVersionId
    ) -> CountryPolicyActivation | None: ...

    async def get_by_approval(
        self, tenant_id: TenantId, approval_id: ApprovalId
    ) -> CountryPolicyActivation | None: ...

    async def next_activation_sequence(
        self, tenant_id: TenantId, country_key: str
    ) -> int: ...

    async def add(
        self, tenant_id: TenantId, activation: CountryPolicyActivation
    ) -> None: ...


@runtime_checkable
class ComplianceUnitOfWork(Protocol):
    versions: CountryPolicyVersionRepository
    provenance: CountryPolicyFieldProvenanceRepository
    activations: CountryPolicyActivationRepository
    bus: EventBus

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


@runtime_checkable
class ComplianceUnitOfWorkFactory(Protocol):
    def __call__(self, tenant_id: TenantId) -> ComplianceUnitOfWork: ...


__all__ = (
    "ComplianceUnitOfWork",
    "ComplianceUnitOfWorkFactory",
    "CountryPolicyActivationRepository",
    "CountryPolicyFieldProvenanceRepository",
    "CountryPolicyVersionRepository",
)
