"""单位专用仓储与事务端口；不扩大旧Demand UoW。"""

from __future__ import annotations

from typing import Protocol, Self

from domains.demand.schemas import (
    NeedQuoteFacts,
    NeedUnitConfirmationView,
    NeedUnitStoredConfirmation,
)
from shared.schemas.identifiers import TenantId, ValidatedNeedId
from shared.schemas.provenance import FactualField


class NeedUnitRepository(Protocol):
    """严格租户隔离；存储仅编解码，不替域判断业务。"""

    async def read_facts(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> NeedQuoteFacts | None:
        """完整读取事实，保留原始类型及全部Provenance。"""
        ...

    async def lock_facts(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> NeedQuoteFacts | None:
        """FOR UPDATE锁Need；原文IO不在此事务内。"""
        ...

    async def find_operation(
        self, tenant_id: TenantId, need_id: ValidatedNeedId, idempotency_key: str
    ) -> NeedUnitStoredConfirmation | None:
        """租户+Need+键读取持久receipt，不修改当前绑定。"""
        ...

    async def get_confirmation(
        self, tenant_id: TenantId, need_id: ValidatedNeedId, confirmation_id: str
    ) -> NeedUnitConfirmationView | None:
        """读取指定Need的不可变确认。"""
        ...

    async def add_confirmation(
        self, tenant_id: TenantId, record: NeedUnitStoredConfirmation
    ) -> None:
        """插入一次完整receipt，与绑定/历史同事务。"""
        ...

    async def apply_current_unit(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        confirmation: NeedUnitConfirmationView,
    ) -> None:
        """只写单位、quantity hash与确认ID三列。"""
        ...

    async def append_unit_history(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        previous_unit: FactualField[str] | None,
        confirmation: NeedUnitConfirmationView,
    ) -> None:
        """旧历史表记前后显示值及真实消息/确认人/时间，完整来源留在receipt。"""
        ...


class NeedUnitUnitOfWork(Protocol):
    """成功退出提交；异常全部回滚；未知提交只用原键恢复。"""

    units: NeedUnitRepository

    async def __aenter__(self) -> Self:
        """进入短事务。"""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        """提交必须在授权guard退出前完成。"""
        ...
