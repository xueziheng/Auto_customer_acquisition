"""工作流 subject 创建边界的 PostgreSQL 事务互斥锁。"""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.schemas.identifiers import TenantId

_LOCK_NAMESPACE = "tradeos.workflow-subject.v2"


def workflow_subject_lock_identity(
    tenant_id: TenantId,
    workflow_type: str,
    subject_ref: str,
) -> str:
    """返回无歧义、跨语言可重建的锁 identity。

    JSON 数组保留字段边界；固定 namespace 允许未来不兼容地升级锁协议。实际
    advisory key 仍由 PostgreSQL ``hashtextextended`` 确定性生成。
    """

    return json.dumps(
        [_LOCK_NAMESPACE, str(tenant_id), workflow_type, subject_ref],
        ensure_ascii=True,
        separators=(",", ":"),
    )


async def acquire_workflow_subject_lock(
    session: AsyncSession,
    tenant_id: TenantId,
    workflow_type: str,
    subject_ref: str,
) -> None:
    """锁住同租户、同工作流类型与 subject 的创建边界，直至当前事务结束。

    正常 Workflow Run 创建与需要排除既有 Run 的维护写操作必须共用此锁；锁负责
    串行化，业务资格仍由领域服务判断，持久写入约束由数据库 guard 最终执行。
    """

    identity = workflow_subject_lock_identity(
        tenant_id,
        workflow_type,
        subject_ref,
    )
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:identity, 0))"),
        {"identity": identity},
    )


__all__ = ("acquire_workflow_subject_lock", "workflow_subject_lock_identity")
