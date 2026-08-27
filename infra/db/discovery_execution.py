"""按研究提案幂等键读取启动回执；不读取工作流业务正文。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from shared.schemas.identifiers import RunId, TenantId

from .tables import WorkflowRunRow


class PostgresDiscoveryExecutionReader:
    """所有状态（包括终态）均可恢复，查询必须同时绑定租户与原提案。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def find_run(self, tenant_id: TenantId, proposal_id: str) -> RunId | None:
        """只返回标识，精确对齐既有 start 的 tenant+idempotency_key 唯一约束。"""
        async with self._factory() as session:
            result = await session.scalar(
                select(WorkflowRunRow.run_id).where(
                    WorkflowRunRow.tenant_id == tenant_id,
                    WorkflowRunRow.workflow_type == "demand_discovery",
                    WorkflowRunRow.subject_ref == proposal_id,
                    WorkflowRunRow.idempotency_key == f"demand-discovery:{proposal_id}",
                )
            )
        return RunId(result) if result is not None else None
