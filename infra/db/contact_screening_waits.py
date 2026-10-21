"""租户限定的联系人筛选等待扫描；只读持久状态，不判断研究证据。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.tables import WorkflowRunRow, WorkflowStepRow
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import ProspectAccountId, RunId, TenantId
from workflows.account_discovery.screening_wake import (
    ContactScreeningWaitPage,
    WaitingContactScreening,
)


def _valid_identity(value: object, maximum: int) -> bool:
    return (isinstance(value, str) and bool(value) and value == value.strip()
            and len(value) <= maximum
            and not any(ord(char) < 32 or ord(char) == 127 for char in value))


class PostgresContactScreeningWaitReader:
    """查询显式限定两表租户、Run 与当前步骤；游标使缺证或坏上下文均不会阻塞后页。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def list_waiting(
        self, tenant_id: TenantId, *, after_run_id: RunId | None, limit: int
    ) -> ContactScreeningWaitPage:
        """最多读取 limit 行，短页回绕；上下文坏行只过滤输出，不丢失原始页进度。"""
        if not _valid_identity(tenant_id, 40):
            raise ValidationError("联系人筛选扫描租户无效")
        if after_run_id is not None and not _valid_identity(after_run_id, 40):
            raise ValidationError("联系人筛选扫描游标无效")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValidationError("联系人筛选扫描数量必须在 1 到 1000 之间")
        query = (
            select(WorkflowRunRow.run_id, WorkflowRunRow.context,
                   WorkflowStepRow.step_name, WorkflowStepRow.updated_at)
            .join(WorkflowStepRow, (
                (WorkflowRunRow.run_id == WorkflowStepRow.run_id)
                & (WorkflowRunRow.tenant_id == WorkflowStepRow.tenant_id)
                & (WorkflowRunRow.current_step == WorkflowStepRow.step_name)
            ))
            .where(
                WorkflowRunRow.tenant_id == tenant_id,
                WorkflowStepRow.tenant_id == tenant_id,
                WorkflowRunRow.workflow_type == "account_discovery",
                WorkflowRunRow.status == "running",
                WorkflowStepRow.status == "waiting_event",
                WorkflowRunRow.current_step.in_(("find_contacts", "verify_contacts")),
            )
            .order_by(WorkflowRunRow.run_id, WorkflowStepRow.step_id)
            .limit(limit)
        )
        if after_run_id is not None:
            query = query.where(WorkflowRunRow.run_id > after_run_id)
        try:
            async with self._factory() as session:
                rows = (await session.execute(query)).all()
        except SQLAlchemyError:
            raise TransientError("联系人筛选等待存储暂不可用") from None
        waits = []
        for row in rows:
            context = row.context
            if not isinstance(context, dict):
                continue
            account_id = context.get("account_id")
            generation = context.get("contact_screening_wait_generation", 0)
            if (not isinstance(account_id, str) or not _valid_identity(account_id, 40)
                    or type(generation) is not int or generation < 0
                    or not isinstance(row.updated_at, datetime)):
                continue
            updated_at = row.updated_at
            if updated_at.tzinfo is None:
                # SQLite 测试库不保留时区；引擎写入一律 UTC。
                updated_at = updated_at.replace(tzinfo=UTC)
            waits.append(WaitingContactScreening(
                run_id=RunId(row.run_id), account_id=ProspectAccountId(account_id),
                step_name=row.step_name,
                wait_version=f"{generation}:{updated_at.astimezone(UTC).isoformat()}",
            ))
        return ContactScreeningWaitPage(
            waits=tuple(waits),
            next_after_run_id=RunId(rows[-1].run_id) if len(rows) == limit else None,
        )
