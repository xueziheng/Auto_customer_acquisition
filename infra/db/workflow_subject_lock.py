"""工作流 subject 创建边界的 PostgreSQL 事务互斥锁。"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.schemas.identifiers import TenantId

_LOCK_NAMESPACE = "tradeos:workflow-subject:v1"


async def acquire_workflow_subject_lock(
    session: AsyncSession,
    tenant_id: TenantId,
    workflow_type: str,
    subject_ref: str,
) -> None:
    """锁住同租户、同工作流类型与 subject 的创建边界，直至当前事务结束。

    正常 Workflow Run 创建与需要排除既有 Run 的维护写操作必须共用此锁；锁仅
    提供串行化，业务资格仍由对应领域服务判断。
    """

    identity = f"{_LOCK_NAMESPACE}:{tenant_id}:{workflow_type}:{subject_ref}"
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
        {"identity": identity},
    )


__all__ = ("acquire_workflow_subject_lock",)
