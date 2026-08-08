"""潜客域存储接口。（浅域）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.prospecting.models import (
    ContactPoint,
    ProspectAccount,
    ProspectContact,
)
from shared.schemas.identifiers import (
    ContactPointId,
    ProspectAccountId,
    TenantId,
)


@runtime_checkable
class AccountRepository(Protocol):
    async def add(self, account: ProspectAccount) -> None: ...

    async def get(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> ProspectAccount | None: ...

    async def find_by_domain(
        self, tenant_id: TenantId, website_domain: str
    ) -> ProspectAccount | None:
        """消歧主查询：域名相同即同一企业。"""
        ...

    async def search_by_name(
        self, tenant_id: TenantId, name: str, country: str
    ) -> list[ProspectAccount]:
        """名称模糊匹配，消歧辅助。返回候选让调用方决定，
        不自动合并——误合并两家不同公司比漏合并更难修。"""
        ...


@runtime_checkable
class ContactRepository(Protocol):
    async def add_contact(self, contact: ProspectContact) -> None: ...

    async def add_contact_point(self, cp: ContactPoint) -> None: ...

    async def get_contact_point(
        self, tenant_id: TenantId, contact_point_id: ContactPointId
    ) -> ContactPoint | None: ...

    async def update_contact_point(self, cp: ContactPoint) -> None: ...

    async def list_verified_for_account(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> list[ContactPoint]: ...

    async def erase_personal_data(
        self, tenant_id: TenantId, contact_point_value_hash: str
    ) -> int:
        """删除请求的执行：清除个人数据字段，保留哈希化抑制记录。"""
        ...
