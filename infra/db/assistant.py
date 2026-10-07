"""assistant 仓储：锁私有会话串行接纳，不持锁等待 Provider。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.assistant.errors import AssistantConflict, AssistantNotFound
from domains.assistant.models import can_transition
from domains.assistant.schemas import (
    MAX_CONTEXT_REFS,
    AssistantActor,
    AssistantDecision,
    ObjectRef,
    SessionView,
    TurnExecution,
    TurnInput,
    TurnState,
    TurnView,
)
from infra.db.base import TenantScopedRepository
from infra.db.tables import AgentSessionRow, AgentTurnRow, ModelInvocationRow
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    AgentSessionId,
    AgentTurnId,
    RunId,
    TenantId,
    new_id,
)
from shared.schemas.model_invocation import ModelFailureCode


class SqlAssistantRepository:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def _session(
        self,
        db: AsyncSession,
        actor: AssistantActor,
        session_id: AgentSessionId,
        *,
        lock: bool = False,
    ) -> AgentSessionRow:
        query = (
            TenantScopedRepository(actor.tenant_id)
            .scoped_query(AgentSessionRow)
            .where(
                AgentSessionRow.session_id == session_id,
                AgentSessionRow.employee_id == actor.employee_id,
                AgentSessionRow.user_id == actor.user_id,
            )
        )
        row = (
            await db.scalars(query.with_for_update() if lock else query)
        ).one_or_none()
        if row is None:
            raise AssistantNotFound()
        return row

    def _view(self, row: AgentTurnRow) -> TurnView:
        return TurnView.model_validate(
            {
                key: getattr(row, key)
                for key in TurnView.model_fields
                if hasattr(row, key)
            }
        )

    async def _turn(
        self,
        db: AsyncSession,
        tenant: TenantId,
        session_id: AgentSessionId,
        turn_id: AgentTurnId,
    ) -> AgentTurnRow:
        row = (
            await db.scalars(
                TenantScopedRepository(tenant)
                .scoped_query(AgentTurnRow)
                .where(
                    AgentTurnRow.session_id == session_id,
                    AgentTurnRow.turn_id == turn_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise AssistantNotFound()
        return row

    async def create(self, actor: AssistantActor) -> SessionView:
        view = SessionView(
            session_id=AgentSessionId(new_id("ase")),
            created_at=datetime.now(UTC),
            version=1,
        )
        async with self._factory() as db, db.begin():
            db.add(AgentSessionRow(**actor.model_dump(), **view.model_dump()))
        return view

    async def sessions(self, actor: AssistantActor) -> list[SessionView]:
        async with self._factory() as db:
            rows = (
                await db.scalars(
                    TenantScopedRepository(actor.tenant_id)
                    .scoped_query(AgentSessionRow)
                    .where(
                        AgentSessionRow.employee_id == actor.employee_id,
                        AgentSessionRow.session_kind == "conversation",
                        AgentSessionRow.user_id == actor.user_id,
                    )
                    .order_by(AgentSessionRow.created_at.desc())
                    .limit(100)
                )
            ).all()
            return [
                SessionView(
                    session_id=AgentSessionId(r.session_id),
                    created_at=r.created_at,
                    version=r.version,
                )
                for r in rows
            ]

    async def accept(
        self,
        actor: AssistantActor,
        session_id: AgentSessionId,
        input: TurnInput,
        request_hmac: str,
        run_id: RunId,
        *,
        attempt_of: AgentTurnId | None = None,
    ) -> TurnView:
        async with self._factory() as db, db.begin():
            await self._session(db, actor, session_id, lock=True)
            scoped = (
                TenantScopedRepository(actor.tenant_id)
                .scoped_query(AgentTurnRow)
                .where(AgentTurnRow.session_id == session_id)
            )
            old = (
                await db.scalars(
                    scoped.where(AgentTurnRow.idempotency_key == input.idempotency_key)
                )
            ).one_or_none()
            if old:
                if old.request_hmac != request_hmac or old.attempt_of != attempt_of:
                    raise AssistantConflict()
                return self._view(old)
            if len((await db.scalars(scoped.limit(100))).all()) >= 100:
                raise ValidationError("会话已达到轮次上限，请新建会话")
            active = (
                await db.scalars(
                    scoped.where(AgentTurnRow.state.in_(("queued", "running")))
                )
            ).first()
            if active:
                raise AssistantConflict()
            if attempt_of is not None:
                original = await self._turn(db, actor.tenant_id, session_id, attempt_of)
                if original.state not in {"failed", "unknown"}:
                    raise AssistantConflict()
            row = AgentTurnRow(
                tenant_id=actor.tenant_id,
                turn_id=new_id("atr"),
                session_id=session_id,
                run_id=run_id,
                idempotency_key=input.idempotency_key,
                request_hmac=request_hmac,
                input_text=input.text,
                object_refs=[r.model_dump() for r in input.object_refs],
                result=None,
                state="queued",
                dispatch_state="pending",
                turn_kind="conversation",
                created_at=datetime.now(UTC),
                attempt_of=attempt_of,
                proposal_id=None,
                error_code=None,
            )
            db.add(row)
            await db.flush()
            return self._view(row)

    async def turns(
        self, actor: AssistantActor, session_id: AgentSessionId
    ) -> list[TurnView]:
        async with self._factory() as db:
            await self._session(db, actor, session_id)
            rows = (
                await db.scalars(
                    TenantScopedRepository(actor.tenant_id)
                    .scoped_query(AgentTurnRow)
                    .where(AgentTurnRow.session_id == session_id)
                    .order_by(
                        AgentTurnRow.created_at.desc(), AgentTurnRow.turn_id.desc()
                    )
                    .limit(101)
                )
            ).all()
            if len(rows) > 100:
                raise ValidationError("上下文超限，请新建会话")
            return [self._view(r) for r in reversed(rows)]

    async def get(
        self, actor: AssistantActor, session_id: AgentSessionId, turn_id: AgentTurnId
    ) -> TurnView:
        async with self._factory() as db:
            await self._session(db, actor, session_id)
            return self._view(
                await self._turn(db, actor.tenant_id, session_id, turn_id)
            )

    async def transition(
        self,
        actor: AssistantActor,
        session_id: AgentSessionId,
        turn_id: AgentTurnId,
        target: TurnState,
        *,
        result: AssistantDecision | None = None,
        proposal_id: str | None = None,
        error_code: ModelFailureCode | None = None,
    ) -> TurnView:
        async with self._factory() as db, db.begin():
            await self._session(db, actor, session_id, lock=True)
            row = await self._turn(db, actor.tenant_id, session_id, turn_id)
            view = self._view(row)
            if not can_transition(view.state, target):
                raise AssistantConflict()
            if view.state == target:
                if result is not None and view.result != result:
                    raise AssistantConflict()
                return view
            row.state = target
            row.result = result.model_dump(mode="json") if result else None
            row.proposal_id = proposal_id
            row.error_code = error_code
            await db.flush()
            return self._view(row)

    async def pending(self, tenant_id: TenantId, limit: int) -> list[TurnExecution]:
        if not 1 <= limit <= 100:
            raise ValueError("轮次扫描上限无效")
        async with self._factory() as db:
            rows = (
                await db.scalars(
                    TenantScopedRepository(tenant_id)
                    .scoped_query(AgentTurnRow)
                    .where(
                        AgentTurnRow.state.in_(("queued", "running")),
                    )
                    .order_by(AgentTurnRow.created_at, AgentTurnRow.turn_id)
                    .limit(limit)
                )
            ).all()
            return [await self._execution(db, tenant_id, row) for row in rows]

    async def _execution(
        self, db: AsyncSession, tenant_id: TenantId, row: AgentTurnRow
    ) -> TurnExecution:
        session = (
            await db.scalars(
                TenantScopedRepository(tenant_id)
                .scoped_query(AgentSessionRow)
                .where(AgentSessionRow.session_id == row.session_id)
            )
        ).one()
        # 以网关先于外部请求落库的不可变调用版本作为恢复绑定，不能用重启后的配置猜测。
        versions = tuple(
            (
                await db.scalars(
                    TenantScopedRepository(tenant_id)
                    .scoped_query(ModelInvocationRow)
                    .where(ModelInvocationRow.turn_id == row.turn_id)
                )
            ).all()
        )
        return TurnExecution.model_validate(
            {
                "actor": {
                    "tenant_id": tenant_id,
                    "user_id": session.user_id,
                    "employee_id": session.employee_id,
                },
                "turn": self._view(row),
                "dispatch_state": row.dispatch_state,
                "checkpoint_sequence": row.checkpoint_sequence,
                "configuration_versions": tuple(
                    sorted({v.configuration_version for v in versions})
                ),
            }
        )

    async def execution(
        self, tenant_id: TenantId, turn_id: AgentTurnId
    ) -> TurnExecution:
        async with self._factory() as db:
            row = (
                await db.scalars(
                    TenantScopedRepository(tenant_id)
                    .scoped_query(AgentTurnRow)
                    .where(AgentTurnRow.turn_id == turn_id)
                )
            ).one_or_none()
            if row is None:
                raise AssistantNotFound()
            return await self._execution(db, tenant_id, row)

    async def bind(
        self, tenant_id: TenantId, turn_id: AgentTurnId, run_id: RunId
    ) -> None:
        async with self._factory() as db, db.begin():
            row = (
                await db.scalars(
                    TenantScopedRepository(tenant_id)
                    .scoped_query(AgentTurnRow)
                    .where(AgentTurnRow.turn_id == turn_id)
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                raise AssistantNotFound()
            if row.run_id != run_id:
                raise AssistantConflict()
            row.dispatch_state = "bound"

    async def checkpoint(
        self,
        actor: AssistantActor,
        session_id: AgentSessionId,
        turn_id: AgentTurnId,
        sequence: int,
        result: AssistantDecision,
        refs: tuple[ObjectRef, ...],
    ) -> None:
        if sequence not in {0, 1} or len(refs) > MAX_CONTEXT_REFS:
            raise AssistantConflict()
        async with self._factory() as db, db.begin():
            await self._session(db, actor, session_id, lock=True)
            row = await self._turn(db, actor.tenant_id, session_id, turn_id)
            if row.state != "running":
                raise AssistantConflict()
            data = result.model_dump(mode="json")
            context_refs = [r.model_dump(mode="json") for r in refs]
            if row.checkpoint_sequence == sequence:
                if row.result != data or row.context_refs != context_refs:
                    raise AssistantConflict()
                return
            if (row.checkpoint_sequence is None and sequence != 0) or (
                row.checkpoint_sequence is not None
                and (sequence != 1 or row.checkpoint_sequence != 0)
            ):
                raise AssistantConflict()
            row.result = data
            row.context_refs = context_refs
            row.checkpoint_sequence = sequence

    async def fail_turn(
        self,
        tenant_id: TenantId,
        turn_id: AgentTurnId,
        state: TurnState,
        code: ModelFailureCode,
    ) -> None:
        if state not in {"blocked", "failed", "unknown"}:
            raise AssistantConflict()
        execution = await self.execution(tenant_id, turn_id)
        async with self._factory() as db, db.begin():
            await self._session(
                db, execution.actor, execution.turn.session_id, lock=True
            )
            row = await self._turn(db, tenant_id, execution.turn.session_id, turn_id)
            if row.state not in {"queued", "running"}:
                return
            row.state, row.error_code, row.result, row.proposal_id = (
                state,
                code,
                None,
                None,
            )
