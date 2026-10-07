"""贸易机会域存储接口。

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from domains.opportunities.models import (
    HandoffPacket,
    LossReason,
    LossRecord,
    Opportunity,
    OpportunityState,
    ScoreSnapshot,
)
from domains.opportunities.permissions import OpportunityScope
from shared.events.bus import EventBus
from shared.schemas.identifiers import (
    EmployeeId,
    HandoffId,
    OpportunityId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.provenance import Provenance


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

    async def list_scoped(
        self,
        tenant_id: TenantId,
        scope: OpportunityScope,
        states: list[OpportunityState] | None,
        limit: int,
    ) -> list[Opportunity]:
        """在 SQL WHERE 中同时应用完整 ABAC scope、状态与租户过滤。"""
        ...

    async def advance_state(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        expected: OpportunityState,
        target: OpportunityState,
    ) -> bool:
        """原子推进状态：``UPDATE ... WHERE state = :expected``；返回是否成功
        （并发下另一事务已改则 False）。终态（WON/LOST）不走此方法。"""
        ...

    async def close_lost_if_state(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        expected: OpportunityState,
        reason: LossReason,
        detail: str | None,
        actor: EmployeeId,
        confirmed_at: datetime,
    ) -> bool:
        """原子终结为 lost：``WHERE state = :expected``，同时写 loss_reason /
        died_at_state / closed_by / closed_at；返回是否成功（并发已变则 False）。"""
        ...

    async def close_won_if_state(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        actor: EmployeeId,
        confirmed_at: datetime,
    ) -> bool:
        """原子终结为 won：仅 ``WHERE state = 'negotiating'``，写 closed_by/closed_at；
        返回是否成功。"""
        ...

    async def assign_owner(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        owner: EmployeeId,
        assigned_by: EmployeeId,
        assigned_at: datetime,
    ) -> bool:
        """分配负责人并记录 assigner 与时间（审计）。"""
        ...


@runtime_checkable
class ScoreSnapshotRepository(Protocol):
    async def add(self, tenant_id: TenantId, snapshot: ScoreSnapshot) -> None:
        """存快照。**只追加，不更新**；写入强制租户。

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


@dataclass(frozen=True)
class HandoffNotificationFacts:
    """事务锁保护的接管、当前机会负责人和员工在职事实。"""

    state: str
    assigned_to: EmployeeId | None
    owner: EmployeeId | None
    account_owner: EmployeeId
    recipient_active: bool


@runtime_checkable
class HandoffRepository(Protocol):
    async def lock_notification_facts(
        self,
        tenant_id: TenantId,
        handoff_id: HandoffId,
        opportunity_id: OpportunityId,
        recipient: EmployeeId,
    ) -> HandoffNotificationFacts | None:
        """先锁员工、机会、账户归属、接管行，返回事实；锁保持到 UoW 退出。"""
        ...

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

    async def list_pending_scoped(
        self,
        tenant_id: TenantId,
        scope: OpportunityScope,
        limit: int,
    ) -> list[HandoffPacket]:
        """按完整 ABAC scope 在 SQL 层读取 pending 接管，最久等待优先。"""
        ...

    async def count_pending_by_employee(
        self, tenant_id: TenantId
    ) -> dict[str, int]: ...

    async def accept_if_requested(
        self,
        tenant_id: TenantId,
        handoff_id: HandoffId,
        accepted_by: EmployeeId,
        accepted_at: datetime,
    ) -> bool:
        """原子接受接管：``UPDATE ... WHERE state = 'requested'``；返回是否成功
        （并发下已被他人接受则 False → 抛 ``HandoffAlreadyAcceptedError``）。"""
        ...

    async def record_escalation(
        self,
        tenant_id: TenantId,
        handoff_id: HandoffId,
        level: int,
        escalated_at: datetime,
    ) -> None:
        """只增记录接管升级；同一接管同一级由数据库唯一约束保证幂等。"""
        ...


@runtime_checkable
class LossRecordRepository(Protocol):
    async def add(self, tenant_id: TenantId, record: LossRecord) -> None:
        """存归因记录（``loss_records``，只增）。"""
        ...

    async def count_by_reason_and_state(
        self, tenant_id: TenantId, since_days: int
    ) -> list[tuple[str, str, int]]:
        """二维交叉计数：``(loss_reason, died_at_state, count)``。"""
        ...


@runtime_checkable
class FieldProvenanceRepository(Protocol):
    async def save(
        self,
        tenant_id: TenantId,
        entity_type: str,
        entity_id: str,
        field_name: str,
        provenance: Provenance,
    ) -> None:
        """保存字段来源（``provenance_records``，只增）。"""
        ...

    async def list_for_entity(
        self, tenant_id: TenantId, entity_type: str, entity_id: str
    ) -> list[tuple[str, Provenance]]:
        """列出某实体全部字段来源：``(field_name, provenance)``，按 extracted_at 新到旧；
        同一字段可有多条只增历史。"""
        ...


@runtime_checkable
class OpportunityUnitOfWork(Protocol):
    """机会事务单元：共享同一 AsyncSession，commit/rollback 语义。

    服务在 ``async with uow:`` 内执行所有仓库操作与事件发布——事件写入
    outbox 与业务写入同一事务。``__aexit__`` 无异常 commit、有异常 rollback。
    """

    opportunities: OpportunityRepository
    snapshots: ScoreSnapshotRepository
    handoffs: HandoffRepository
    loss_records: LossRecordRepository
    provenance: FieldProvenanceRepository
    bus: EventBus

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
