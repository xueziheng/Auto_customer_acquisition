"""仅按规范 Run 读取已确认提案标识；不从 Run context 接受员工身份。"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.tables import WorkflowRunRow
from shared.schemas.identifiers import RunId, TenantId
from shared.schemas.model_invocation import ModelGenerationError


class ResearchRunBindingReader:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def proposal(self, tenant_id: TenantId, run_id: RunId) -> str:
        async with self._factory() as session:
            row = await session.scalar(select(WorkflowRunRow).where(
                WorkflowRunRow.tenant_id == tenant_id, WorkflowRunRow.run_id == run_id,
                WorkflowRunRow.workflow_type == 'demand_discovery',
                WorkflowRunRow.current_step == 'execute_search',
                WorkflowRunRow.status.in_(('pending','running')),
            ))
            if row is None or row.idempotency_key != f'demand-discovery:{row.subject_ref}':
                raise ModelGenerationError('permission')
            return row.subject_ref
