"""报价审批专用政策选择租约；不扩大一般成本读取权限。"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from datetime import datetime
from typing import Protocol

from domains.costing.errors import CostFreezeError
from domains.costing.quote_repository import PricingPolicyRepository
from domains.costing.repository import CostingUnitOfWorkFactory
from domains.costing.schemas import PricingPolicyView
from shared.schemas.identifiers import TenantId
from shared.schemas.quote_facts import fact_utc


class CostingPolicySelection(Protocol):
    """持共享集合锁的当前政策读取，不以历史ID代替选择。"""

    async def current(self) -> PricingPolicyView:
        """每次调用新取时钟，未来生效政策不能被旧快照遮蔽。"""
        ...


class CostingApprovalPolicyReader(Protocol):
    """仅供已授权报价审批编排，无HTTP入口或系统actor。"""

    def open(
        self, tenant_id: TenantId, category: str | None
    ) -> AbstractAsyncContextManager[CostingPolicySelection]:
        """本域UoW持政策集合共享锁，退出才释放。"""
        ...


class _Selection:
    """单用途租约，离开后不可读取未保护的当前政策。"""

    def __init__(
        self,
        repo: PricingPolicyRepository,
        tenant_id: TenantId,
        category: str | None,
        now: Callable[[], datetime],
    ) -> None:
        """仓储来自已经持锁的成本事务。"""
        self._repo, self._tenant, self._category, self._now = (
            repo,
            tenant_id,
            category,
            now,
        )
        self.closed = False

    async def current(self) -> PricingPolicyView:
        """复用成本域既有选择算法，不默认回退未确认政策。"""
        if self.closed:
            raise CostFreezeError("invalid_input")
        record = await self._repo.get_effective(
            self._tenant, self._category, fact_utc(self._now())
        )
        if record is None:
            raise CostFreezeError("policy_missing")
        return record.value


class CostingApprovalPolicyReaderImpl:
    """只复用成本UoW及选择锁，不制造CostingActor。"""

    def __init__(
        self, factory: CostingUnitOfWorkFactory, *, now: Callable[[], datetime]
    ) -> None:
        """传入已显式配置超时的UoW工厂与业务时钟。"""
        self._factory, self._now = factory, now

    @asynccontextmanager
    async def open(
        self, tenant_id: TenantId, category: str | None
    ) -> AsyncIterator[CostingPolicySelection]:
        """调用方负责在退出此租约前完成报价提交。"""
        async with self._factory(tenant_id) as uow:
            await uow.policies.lock_selection(tenant_id, exclusive=False)
            selection = _Selection(uow.policies, tenant_id, category, self._now)
            try:
                yield selection
            finally:
                selection.closed = True
