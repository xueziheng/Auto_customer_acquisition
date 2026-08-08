"""组织域存储接口。（浅域）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.organization.models import CompanyPlaybook, Tenant
from shared.schemas.identifiers import TenantId


@runtime_checkable
class TenantRepository(Protocol):
    async def get(self, tenant_id: TenantId) -> Tenant | None: ...

    async def add(self, tenant: Tenant) -> None: ...


@runtime_checkable
class PlaybookRepository(Protocol):
    async def get(self, tenant_id: TenantId) -> CompanyPlaybook | None: ...

    async def save_version(self, playbook: CompanyPlaybook) -> None:
        """保存新版本，旧版本保留——「上个月的排除清单里有没有这个
        品类」是合规争议时要回答的问题。"""
        ...
