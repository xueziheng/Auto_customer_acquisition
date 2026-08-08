"""发件身份域存储接口。

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from domains.sending_identity.models import (
    DomainRole,
    IdentityState,
    ReputationWindow,
    SendingIdentity,
)
from shared.schemas.identifiers import SendingIdentityId, TenantId


@runtime_checkable
class SendingIdentityRepository(Protocol):
    async def add(self, identity: SendingIdentity) -> None: ...

    async def get(
        self, tenant_id: TenantId, identity_id: SendingIdentityId
    ) -> SendingIdentity | None: ...

    async def update(self, identity: SendingIdentity) -> None: ...

    async def find_by_address(
        self, tenant_id: TenantId, address: str
    ) -> SendingIdentity | None: ...

    async def list_by_domain(
        self, tenant_id: TenantId, domain: str
    ) -> list[SendingIdentity]:
        """同域名下的全部身份。

        两个用途：登记时检查角色冲突，以及计算域名级聚合信誉。
        """
        ...

    async def find_domain_role(
        self, tenant_id: TenantId, domain: str
    ) -> DomainRole | None:
        """查域名已登记的角色。

        登记新身份时先查这个：同一域名下角色必须一致。返回 None
        表示该域名首次登记。
        """
        ...

    async def list_by_state(
        self, tenant_id: TenantId, states: list[IdentityState]
    ) -> list[SendingIdentity]: ...

    async def list_usable_for_cold_outreach(
        self, tenant_id: TenantId
    ) -> list[SendingIdentity]:
        """可用于冷开发的身份。

        实现要求：角色为 ``COLD_OUTREACH``、认证通过、状态为
        ``ACTIVE`` 或 ``WARMING``。三个条件都要在 SQL 里过滤，
        不要查出来再在 Python 里筛——这个列表直接喂给 Campaign
        创建界面，漏一个条件就等于给用户提供了错误选项。
        """
        ...


@runtime_checkable
class SendCounterRepository(Protocol):
    """当日发送计数。

    单独一个 repository 是因为写入频率远高于身份实体，且需要原子递增。
    """

    async def increment(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        on_day: date,
        count: int = 1,
    ) -> int:
        """原子递增并返回递增后的值。

        **必须原子。** 并发发送时读-改-写会丢计数，然后当日实际发送量
        突破预热上限——那正是预热要防的事。
        """
        ...

    async def get_count(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        on_day: date,
    ) -> int: ...


@runtime_checkable
class ReputationRepository(Protocol):
    async def record_event(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        event_type: str,
        occurred_at: str,
        dedup_key: str,
    ) -> bool:
        """记录投递事件，返回是否为新事件。

        ``dedup_key`` 必填：邮件服务商 webhook 经常重发，重复计数
        会导致误熔断。返回 False 表示重复，调用方应跳过后续处理。
        """
        ...

    async def compute_window(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        window_days: int,
    ) -> ReputationWindow:
        """按滚动窗口聚合。

        **不要缓存成生命周期累计值。** 生命周期数字会掩盖最近的
        恶化，而最近的恶化才是要熔断的对象。
        """
        ...

    async def compute_domain_window(
        self, tenant_id: TenantId, domain: str, window_days: int
    ) -> ReputationWindow:
        """域名级聚合。"""
        ...
