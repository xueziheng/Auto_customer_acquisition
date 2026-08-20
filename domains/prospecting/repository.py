"""潜客域存储接口。（浅域）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from typing import Protocol, Self, runtime_checkable

from domains.prospecting.models import (
    ContactPoint,
    ContactPointKind,
    ProspectAccount,
    ProspectContact,
)
from shared.events.bus import EventBus
from shared.schemas.identifiers import (
    ContactPointId,
    ProspectAccountId,
    ProspectContactId,
    TenantId,
)


@runtime_checkable
class AccountRepository(Protocol):
    async def add(self, account: ProspectAccount) -> bool:
        """新插入返回 True；同租户 canonical domain 冲突返回 False。"""
        ...

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

    async def merge_source_signal_refs(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        source_signal_refs: tuple[str, ...],
    ) -> ProspectAccount | None:
        """行锁下做稳定去重并集；不存在返回 None。"""
        ...


@runtime_checkable
class ContactRepository(Protocol):
    async def add_contact(self, contact: ProspectContact) -> None: ...

    async def get_contact(
        self, tenant_id: TenantId, contact_id: ProspectContactId
    ) -> ProspectContact | None: ...

    async def add_contact_point(self, cp: ContactPoint) -> bool:
        """联系方式与法律依据原子新增；指纹冲突返回 False。"""
        ...

    async def get_contact_point(
        self, tenant_id: TenantId, contact_point_id: ContactPointId
    ) -> ContactPoint | None: ...

    async def update_contact_point(self, cp: ContactPoint) -> None: ...

    async def find_by_value_hash(
        self, tenant_id: TenantId, kind: ContactPointKind, value_hash: str
    ) -> ContactPoint | None: ...

    async def list_verified_for_account(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> list[ContactPoint]: ...

    async def erase_personal_data(
        self, tenant_id: TenantId, contact_point_value_hash: str
    ) -> int:
        """删除请求的执行：清除个人数据字段，保留哈希化抑制记录。"""
        ...

    async def is_erasure_suppressed(
        self, tenant_id: TenantId, contact_point_value_hash: str
    ) -> bool: ...


@runtime_checkable
class ProspectingUnitOfWork(Protocol):
    """企业、联系人、法律依据与 outbox 的同一事务边界。"""

    accounts: AccountRepository
    contacts: ContactRepository
    bus: EventBus

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None: ...
