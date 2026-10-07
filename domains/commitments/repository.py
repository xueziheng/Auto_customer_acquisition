"""承诺域存储接口。（浅域）

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Self, runtime_checkable

from domains.commitments.models import Commitment, CommitmentStatus
from shared.events.bus import EventBus
from shared.schemas.identifiers import CommitmentId, EmployeeId, TenantId


@runtime_checkable
class CommitmentRepository(Protocol):
    async def add(self, commitment: Commitment) -> None: ...

    async def add_if_absent(
        self, commitment: Commitment
    ) -> tuple[Commitment, bool]:
        """按 ``(tenant_id, source_message_id, action)`` 原子插入。

        返回 ``(持久化 winner, 是否本次创建)``。必须由唯一约束实现，
        禁止先查后插。
        """
        ...

    async def get(
        self, tenant_id: TenantId, commitment_id: CommitmentId
    ) -> Commitment | None: ...

    async def get_for_update(
        self, tenant_id: TenantId, commitment_id: CommitmentId
    ) -> Commitment | None: ...

    async def update(self, commitment: Commitment) -> None: ...

    async def find_duplicate(
        self, tenant_id: TenantId, source_message_id: str, action: str
    ) -> Commitment | None:
        """提取幂等：同一消息同一动作只记一条。"""
        ...

    async def list_due(
        self, tenant_id: TenantId, before: datetime, limit: int
    ) -> list[Commitment]:
        """到期未完成的已确认承诺。``due_at_uncertain`` 的排除在外。"""
        ...

    async def list_for_employee(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        statuses: list[CommitmentStatus],
    ) -> list[Commitment]: ...


@runtime_checkable
class CommitmentUnitOfWork(Protocol):
    commitments: CommitmentRepository
    bus: EventBus

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None: ...


@runtime_checkable
class CommitmentUnitOfWorkFactory(Protocol):
    def __call__(self, tenant_id: TenantId) -> CommitmentUnitOfWork: ...
