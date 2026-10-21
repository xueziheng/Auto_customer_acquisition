"""自动恢复旧版人工补证等待；费用与执行权限仍由联系人步骤重读校验。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from domains.prospecting.service import (
    ProspectingServicePlanStore,
    tier_policy,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import ProspectAccountId, RunId, TenantId
from workflows.demand_discovery.service_tiers import current_plan
from workflows.engine.runner import WorkflowEngine

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WaitingContactScreening:
    """只含投递身份；等待版本由持久等待代数和步骤更新时间组成。"""

    run_id: RunId
    account_id: ProspectAccountId
    step_name: str
    wait_version: str


@dataclass(frozen=True)
class ContactScreeningWaitPage:
    """游标来自原始页，保证被过滤的损坏记录不会饿死后续等待项。"""

    waits: tuple[WaitingContactScreening, ...]
    next_after_run_id: RunId | None


class ContactScreeningWaitReader(Protocol):
    """按当前租户有界读取当前联系人等待步骤，不读取其他业务上下文。"""

    async def list_waiting(
        self, tenant_id: TenantId, *, after_run_id: RunId | None, limit: int
    ) -> ContactScreeningWaitPage:
        """按 Run ID 稳定升序扫描；原始页不足 limit 时游标归零。"""
        ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ContactScreeningWakeDriver:
    """单次最多一页；有效服务期内唤醒历史等待，不预留额度或调用 Provider。"""

    def __init__(
        self,
        *,
        tenant_id: TenantId,
        waits: ContactScreeningWaitReader,
        service_plans: ProspectingServicePlanStore,
        engine: WorkflowEngine,
        batch_limit: int = 50,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if type(batch_limit) is not int or not 1 <= batch_limit <= 1000:
            raise ValidationError("联系人筛选扫描数量必须在 1 到 1000 之间")
        self._tenant_id = tenant_id
        self._waits = waits
        self._service_plans = service_plans
        self._engine = engine
        self._batch_limit = batch_limit
        self._clock = clock
        self._after_run_id: RunId | None = None

    async def scan_once(self) -> int:
        """返回引擎接受的事件数；单项失败安全记录，取消与页读取失败交给主循环。"""
        page = await self._waits.list_waiting(
            self._tenant_id, after_run_id=self._after_run_id, limit=self._batch_limit
        )
        if len(page.waits) > self._batch_limit:
            raise ValidationError("联系人筛选读取超过扫描上限")
        self._after_run_id = page.next_after_run_id
        accepted = 0
        for waiting in page.waits:
            try:
                now = self._clock()
                state = await current_plan(self._service_plans, self._tenant_id, now)
                if state is None:
                    continue
                delivered = await self._engine.deliver_event(
                    self._tenant_id, waiting.run_id, "ProspectScreeningReady",
                    {"account_id": waiting.account_id,
                     "step_name": waiting.step_name,
                     "wait_version": waiting.wait_version,
                     "tier": state.tier,
                     "policy_version": tier_policy(state.tier).policy_version},
                )
                accepted += int(delivered)
            except Exception:  # noqa: BLE001 — 单 Run 隔离且不记录异常内容
                # 禁止携带原始异常、来源或凭证；CancelledError 不属于 Exception。
                _logger.warning("联系人筛选等待项暂未唤醒")
        return accepted
