"""模型技术额度独立事务；只锁额度身份，不锁工作流 Run。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.base import TenantScopedRepository
from infra.db.tables import ModelInvocationRow, ModelQuotaBucketRow, ModelSlotReleaseRow
from shared.schemas.identifiers import ModelInvocationId, TenantId, UserId, new_id
from shared.schemas.model_invocation import InvocationIdentity, ModelLimits, ModelUsage
from tool_gateway.model_usage import (
    InvocationState,
    InvocationView,
    Reservation,
    window_start,
)


class SqlModelUsageRepository:
    """每次调用独立提交；调用方不能把工作流连接借给该仓储。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def _bucket(
        self, session: AsyncSession, tenant: TenantId, key: str, start: datetime
    ) -> ModelQuotaBucketRow:
        await session.execute(
            insert(ModelQuotaBucketRow)
            .values(
                tenant_id=tenant,
                scope_key=key,
                window_started_at=start,
                calls=0,
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "scope_key"])
        )
        return (
            await session.scalars(
                TenantScopedRepository(tenant)
                .scoped_query(ModelQuotaBucketRow)
                .where(ModelQuotaBucketRow.scope_key == key)
                .with_for_update()
            )
        ).one()

    async def reserve(
        self,
        identity: InvocationIdentity,
        request_hmac: str,
        limits: ModelLimits,
        now: datetime,
        *,
        model: str,
        owner_id: str | None = None,
        lease_expires_at: datetime | None = None,
    ) -> Reservation:
        """租户锁→员工锁→幂等→两级额度，全部在一笔独立事务内完成。"""
        if (
            not re.fullmatch(r"[0-9a-f]{64}", request_hmac)
            or not model
            or len(model) > 128
        ):
            raise ValueError("模型预留参数无效")
        start = window_start(now, limits.window_seconds)
        tenant = identity.tenant_id
        scoped = TenantScopedRepository(tenant)
        async with self._factory() as session, session.begin():
            tenant_bucket = await self._bucket(session, tenant, "tenant", start)
            employee_bucket = await self._bucket(
                session, tenant, "employee:" + identity.employee_id, start
            )
            existing = (
                await session.scalars(
                    scoped.scoped_query(ModelInvocationRow).where(
                        ModelInvocationRow.run_id == identity.run_id,
                        ModelInvocationRow.capability == identity.capability,
                        ModelInvocationRow.configuration_version
                        == identity.configuration_version,
                        ModelInvocationRow.sequence == identity.sequence,
                    )
                )
            ).one_or_none()
            if existing is not None:
                same = (
                    existing.request_hmac == request_hmac
                    and existing.model == model
                    and existing.user_id == identity.user_id
                    and existing.employee_id == identity.employee_id
                    and existing.turn_id == identity.turn_id
                )
                return Reservation(
                    invocation_id=ModelInvocationId(existing.invocation_id),
                    outcome="duplicate" if same else "conflict",
                    state=cast(InvocationState, existing.state),
                )
            counted = scoped.scoped_query(ModelInvocationRow).where(
                ModelInvocationRow.state != "rejected"
            )
            recent = counted.where(ModelInvocationRow.created_at >= start).subquery()
            active = counted.where(
                ModelInvocationRow.slot_released.is_(False)
            ).subquery()
            tenant_count = int(
                (await session.scalar(select(func.count()).select_from(recent))) or 0
            )
            employee_count = int(
                (
                    await session.scalar(
                        select(func.count())
                        .select_from(recent)
                        .where(recent.c.employee_id == identity.employee_id)
                    )
                )
                or 0
            )
            active_tenant = int(
                (await session.scalar(select(func.count()).select_from(active))) or 0
            )
            active_employee = int(
                (
                    await session.scalar(
                        select(func.count())
                        .select_from(active)
                        .where(active.c.employee_id == identity.employee_id)
                    )
                )
                or 0
            )
            if (
                tenant_count >= limits.tenant_calls
                or employee_count >= limits.employee_calls
                or active_tenant >= limits.tenant_concurrency
                or active_employee >= limits.employee_concurrency
            ):
                return Reservation(invocation_id=None, outcome="limited", state=None)
            invocation_id = ModelInvocationId(new_id("minv"))
            session.add(
                ModelInvocationRow(
                    **identity.model_dump(),
                    invocation_id=invocation_id,
                    request_hmac=request_hmac,
                    provider="deepseek",
                    model=model,
                    owner_id=owner_id,
                    lease_expires_at=lease_expires_at,
                    state="reserved",
                    input_tokens=None,
                    cached_input_tokens=None,
                    output_tokens=None,
                    slot_released=False,
                    created_at=now,
                    dispatched_at=None,
                    finished_at=None,
                )
            )
            tenant_bucket.window_started_at = employee_bucket.window_started_at = start
            tenant_bucket.calls = tenant_count + 1
            employee_bucket.calls = employee_count + 1
            return Reservation(
                invocation_id=invocation_id, outcome="reserved", state="reserved"
            )

    async def recover_abandoned(
        self, tenant_id: TenantId, current_owner: str, now: datetime
    ) -> int:
        """只供持有规范 scheduler 单例锁的当前 owner 调用；未知费用不退款。"""
        async with self._factory() as session, session.begin():
            rows = (await session.scalars(
                TenantScopedRepository(tenant_id).scoped_query(ModelInvocationRow)
                .where(ModelInvocationRow.state.in_(("reserved", "dispatched")),
                       ModelInvocationRow.owner_id.is_not(None),
                       ModelInvocationRow.owner_id != current_owner,
                       ModelInvocationRow.lease_expires_at <= now)
                .with_for_update()
            )).all()
            for row in rows:
                # dispatched 在 Provider IO 前独立提交；reserved 因此可证明尚未发送。
                row.slot_released = row.state == "reserved"
                row.state = "rejected" if row.slot_released else "unknown"
                row.finished_at = now
            return len(rows)

    async def _row(
        self,
        session: AsyncSession,
        tenant: TenantId,
        invocation: ModelInvocationId,
        *,
        lock: bool = True,
    ) -> ModelInvocationRow:
        stmt = (
            TenantScopedRepository(tenant)
            .scoped_query(ModelInvocationRow)
            .where(ModelInvocationRow.invocation_id == invocation)
        )
        if lock:
            stmt = stmt.with_for_update()
        row = (await session.scalars(stmt)).one_or_none()
        if row is None:
            raise ValueError("模型调用不可见")
        return row

    async def mark_dispatched(
        self, tenant_id: TenantId, invocation_id: ModelInvocationId
    ) -> None:
        """只有第一位执行者能从 reserved 进入外部调用，重复执行拒绝。"""
        async with self._factory() as session, session.begin():
            row = await self._row(session, tenant_id, invocation_id)
            if row.state != "reserved":
                raise ValueError("模型请求已派发或结束")
            row.state = "dispatched"
            row.dispatched_at = datetime.now(UTC)

    async def finish(
        self,
        tenant_id: TenantId,
        invocation_id: ModelInvocationId,
        usage: ModelUsage,
        state: InvocationState,
    ) -> None:
        """结果未知保留并发槽与次数；确定完成/拒绝才释放并发槽。"""
        async with self._factory() as session, session.begin():
            row = await self._row(session, tenant_id, invocation_id)
            if row.state == state:
                if (row.input_tokens, row.cached_input_tokens, row.output_tokens) != (
                    usage.input_tokens,
                    usage.cached_input_tokens,
                    usage.output_tokens,
                ):
                    raise ValueError("模型用量结算冲突")
                return
            allowed = (row.state == "reserved" and state == "rejected") or (
                row.state == "dispatched"
                and state in {"succeeded", "invalid", "unknown"}
            )
            if not allowed:
                raise ValueError("模型调用状态冲突")
            row.state = state
            row.input_tokens = usage.input_tokens
            row.cached_input_tokens = usage.cached_input_tokens
            row.output_tokens = usage.output_tokens
            row.slot_released = state != "unknown"
            row.finished_at = datetime.now(UTC)

    async def get(
        self, tenant_id: TenantId, invocation_id: ModelInvocationId
    ) -> InvocationView:
        async with self._factory() as session:
            row = await self._row(session, tenant_id, invocation_id, lock=False)
            return InvocationView(
                invocation_id=ModelInvocationId(row.invocation_id),
                identity=InvocationIdentity.model_validate(
                    {key: getattr(row, key) for key in InvocationIdentity.model_fields}
                ),
                model=row.model,
                state=cast(InvocationState, row.state),
                usage=ModelUsage(
                    input_tokens=row.input_tokens,
                    cached_input_tokens=row.cached_input_tokens,
                    output_tokens=row.output_tokens,
                ),
                created_at=row.created_at,
                finished_at=row.finished_at,
                slot_released=row.slot_released,
            )

    async def release_unknown_slot(
        self,
        tenant_id: TenantId,
        invocation_id: ModelInvocationId,
        operator: UserId,
        reason: str,
    ) -> None:
        """仅供受信运维调用；记录操作者与固定原因，不改变计费事实。"""
        if not operator or reason != "operator_confirmed_stopped":
            raise ValueError("模型对账记录无效")
        async with self._factory() as session, session.begin():
            row = await self._row(session, tenant_id, invocation_id)
            if row.state != "unknown":
                raise ValueError("只有未知请求可以人工解除槽位")
            if row.slot_released:
                return
            session.add(
                ModelSlotReleaseRow(
                    tenant_id=tenant_id,
                    invocation_id=invocation_id,
                    operator_id=operator,
                    reason=reason,
                    created_at=datetime.now(UTC),
                )
            )
            row.slot_released = True
