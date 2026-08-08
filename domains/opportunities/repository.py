"""贸易机会域存储接口。

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.opportunities.models import (
    HandoffPacket,
    Opportunity,
    OpportunityState,
    ScoreSnapshot,
)
from shared.schemas.identifiers import (
    EmployeeId,
    HandoffId,
    OpportunityId,
    TenantId,
    ValidatedNeedId,
)


@runtime_checkable
class OpportunityRepository(Protocol):
    async def add(self, opportunity: Opportunity) -> None: ...

    async def get(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> Opportunity | None: ...

    async def update(self, opportunity: Opportunity) -> None: ...

    async def find_by_need(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> Opportunity | None:
        """按需求查机会。用于保证幂等——``NeedValidated`` 事件可能
        重复投递，重复创建机会会导致同一客户被分配给两个员工。"""
        ...

    async def list_by_owner(
        self,
        tenant_id: TenantId,
        owner: EmployeeId,
        states: list[OpportunityState] | None,
        limit: int,
    ) -> list[Opportunity]: ...

    async def list_by_state(
        self, tenant_id: TenantId, state: OpportunityState, limit: int
    ) -> list[Opportunity]: ...

    async def count_loss_reasons(
        self, tenant_id: TenantId, since_days: int
    ) -> list[tuple[str, str, int]]:
        """返回 ``(loss_reason, died_at_state, count)``。

        二维交叉是刻意的：单看原因不足以定位问题。
        """
        ...


@runtime_checkable
class ScoreSnapshotRepository(Protocol):
    async def add(self, snapshot: ScoreSnapshot) -> None:
        """存快照。**只追加，不更新。**

        重新打分产生新快照，旧的保留。分数变化的历史本身是信息：
        一个机会从低分变高分，说明客户回复补充了证据。
        """
        ...

    async def latest_for_opportunity(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> ScoreSnapshot | None: ...

    async def list_for_backtest(
        self, tenant_id: TenantId, since_days: int
    ) -> list[ScoreSnapshot]:
        """导出快照供权重回测。

        Phase 2 用：把快照和成交结果 join 起来，才能知道哪些因子
        真的预测成交。这是 Phase 1 存快照的全部目的。
        """
        ...


@runtime_checkable
class HandoffRepository(Protocol):
    async def add(self, packet: HandoffPacket) -> None: ...

    async def get(
        self, tenant_id: TenantId, handoff_id: HandoffId
    ) -> HandoffPacket | None: ...

    async def update(self, packet: HandoffPacket) -> None: ...

    async def find_pending_for_opportunity(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> HandoffPacket | None:
        """查未完成接管，保证幂等。"""
        ...

    async def list_pending(
        self, tenant_id: TenantId, limit: int
    ) -> list[HandoffPacket]:
        """待接管队列。**按 ``requested_at`` 升序**——最久等待的排最前，
        这是唯一合理的默认排序。按分数排会让高分机会不断插队，
        低分机会永远等不到人处理，最终全部超时流失。
        """
        ...

    async def count_pending_by_employee(
        self, tenant_id: TenantId
    ) -> dict[str, int]: ...
