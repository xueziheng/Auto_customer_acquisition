"""组织域存储 Protocol；所有操作显式租户过滤。"""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from domains.organization.models import (
    CompanyPlaybookVersion,
    PlaybookActivation,
    Tenant,
)
from shared.schemas.identifiers import (
    ApprovalId,
    IdempotencyKey,
    PlaybookVersionId,
    TenantId,
)


@runtime_checkable
class TenantRepository(Protocol):
    async def get(self, tenant_id: TenantId) -> Tenant | None: ...

    async def add(self, tenant: Tenant) -> None: ...


@runtime_checkable
class PlaybookVersionRepository(Protocol):
    async def add(self, version: CompanyPlaybookVersion) -> None: ...

    async def get(
        self, tenant_id: TenantId, version_id: PlaybookVersionId
    ) -> CompanyPlaybookVersion | None: ...

    async def find_by_idempotency_key(
        self, tenant_id: TenantId, idempotency_key: IdempotencyKey
    ) -> CompanyPlaybookVersion | None: ...

    async def next_version_number(self, tenant_id: TenantId) -> int:
        """在租户级事务锁内分配严格递增版本号。"""
        ...

    async def list(
        self, tenant_id: TenantId, limit: int
    ) -> list[CompanyPlaybookVersion]: ...


@runtime_checkable
class PlaybookActivationRepository(Protocol):
    async def lock_tenant(self, tenant_id: TenantId) -> None:
        """取得与版本编号分配相同的租户级事务锁。"""
        ...

    async def get_current(self, tenant_id: TenantId) -> PlaybookActivation | None: ...

    async def get_by_version(
        self, tenant_id: TenantId, version_id: PlaybookVersionId
    ) -> PlaybookActivation | None: ...

    async def get_by_approval(
        self, tenant_id: TenantId, approval_id: ApprovalId
    ) -> PlaybookActivation | None: ...

    async def add(self, activation: PlaybookActivation) -> None: ...


@runtime_checkable
class OrganizationUnitOfWork(Protocol):
    versions: PlaybookVersionRepository
    activations: PlaybookActivationRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

@runtime_checkable
class OrganizationUnitOfWorkFactory(Protocol):
    def __call__(self, tenant_id: TenantId) -> OrganizationUnitOfWork: ...


__all__ = (
    "OrganizationUnitOfWork",
    "OrganizationUnitOfWorkFactory",
    "PlaybookActivationRepository",
    "PlaybookVersionRepository",
    "TenantRepository",
)
