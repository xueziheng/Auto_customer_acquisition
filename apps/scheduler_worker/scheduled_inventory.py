"""周期仅提交 canonical InventoryRequest；旧 research 排程等待重新确认目标。"""
from collections.abc import Callable
from datetime import UTC, datetime

from domains.prospecting.service import (
    InventoryService,
    ProspectingServicePlanStore,
    tier_policy,
)
from infra.db.inventory_service_plan import InventoryServicePlanBudget
from shared.errors import InvalidStateTransition, PermissionDenied
from shared.schemas.identifiers import EmployeeId, TenantId
from workflows.enterprise_knowledge.confirmed_context import ConfirmedContextReader

from .inventory_acquisition import InventoryAcquisitionDriver
from .inventory_model import InventoryEmployeeAuthority


class ScheduledInventoryDriver:
    """与库存驱动共享一次singleton扫描，不创建第二个研究循环。"""
    def __init__(self, inventory: InventoryAcquisitionDriver, *, tenant_id: TenantId,
                 store: ProspectingServicePlanStore, requests: InventoryService,
                 receipts: InventoryServicePlanBudget, authority: InventoryEmployeeAuthority,
                 context: ConfirmedContextReader, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.inventory, self.tenant, self.store = inventory, tenant_id, store
        self.requests, self.receipts, self.authority, self.context, self.now = requests, receipts, authority, context, now

    async def submit_due(self) -> int:
        cycle = await self.store.begin_cycle(self.tenant, self.now())
        if cycle is None or cycle.target_kind != "inventory_request":
            return 0
        receipt = await self.receipts.find_request(cycle.key)
        if receipt is not None:
            await self.store.bind_inventory_cycle(self.tenant, key=cycle.key, request_key_hash=receipt, now=self.now())
            return 1
        state = await self.store.get(self.tenant)
        if (state is None or not state.schedule_enabled or not state.period_started_at <= self.now() < state.period_ends_at
            or cycle.query is None or cycle.employee_id is None):
            return 0
        policy = tier_policy(state.tier)
        reserved = await self.receipts.request_reservation(cycle.key)
        # 请求事务可回滚而独立预留已提交；同周期重放不能再次收费或改变数量。
        if reserved is None and (
            state.candidates_reserved + policy.per_run_signal_limit > policy.candidate_limit
            or state.searches_reserved + policy.per_run_search_limit > policy.search_limit
        ):
            return 0
        count = reserved.candidates if reserved is not None else policy.per_run_signal_limit
        await self.authority.authorize_schedule(self.tenant, EmployeeId(cycle.employee_id), cycle.acting_user_id)
        await self.context.read(self.tenant, EmployeeId(cycle.employee_id), cycle.query.confirmed_refs)
        if not await self.store.claim_cycle_start(self.tenant, key=cycle.key, expected_version=state.version, now=self.now()):
            return 0
        await self.requests.request(self.tenant, EmployeeId(cycle.employee_id), cycle.query, count, cycle.key)
        receipt = await self.receipts.find_request(cycle.key)
        if receipt is None:
            raise InvalidStateTransition("库存请求回执尚未确认")
        await self.store.bind_inventory_cycle(self.tenant, key=cycle.key, request_key_hash=receipt, now=self.now())
        return 1

    async def scan(self) -> int:
        try:
            submitted = await self.submit_due()
        except (PermissionDenied, InvalidStateTransition):
            submitted = 0
        return submitted + await self.inventory.scan()


class InventoryWithScreeningWake:
    """旧等待只投递恢复事件，沿用已有singleton生命周期和成本闸门。"""
    def __init__(self, inventory, wake) -> None:
        self.inventory, self.wake = inventory, wake

    async def scan(self) -> int:
        await self.wake.scan_once()
        return await self.inventory.scan() if self.inventory is not None else 0
