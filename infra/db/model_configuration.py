"""非秘密配置与进程版本仓储；探测与 turn 同事务落库。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.assistant.errors import AssistantConflict
from domains.assistant.schemas import (
    AssistantActor,
    AssistantRegenerateInput,
    ModelConfigurationSnapshot,
    ModelRuntimeStatus,
    ModelSettingsUpdate,
    TurnView,
)
from domains.assistant.service import AssistantFingerprints
from infra.db.tables import (
    AgentSessionRow,
    AgentTurnRow,
    EmployeeRow,
    ModelConfigurationHeadRow,
    ModelConfigurationVersionRow,
    ModelProbeRow,
    ModelRuntimeProcessRow,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import AgentTurnId, TenantId, new_id
from shared.schemas.model_invocation import ModelLimits


def _turn(row: AgentTurnRow) -> TurnView:
    return TurnView.model_validate(
        {key: getattr(row, key) for key in TurnView.model_fields if hasattr(row, key)}
    )


class SqlModelConfigurationRepository:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        fingerprints: AssistantFingerprints,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._factory, self._fingerprints, self._now = factory, fingerprints, now

    async def initialize(
        self,
        tenant_id: TenantId,
        version: str,
        model: str,
        limits: ModelLimits,
        export_enabled: bool,
    ) -> None:
        async with self._factory() as db, db.begin():
            await db.execute(
                insert(ModelConfigurationVersionRow)
                .values(
                    tenant_id=tenant_id,
                    version=version,
                    model=model,
                    limits=limits.model_dump(),
                    export_enabled=export_enabled,
                    created_by="system:deployment",
                    created_at=self._now(),
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "version"])
            )
            existing = (
                await db.scalars(
                    select(ModelConfigurationVersionRow).where(
                        ModelConfigurationVersionRow.tenant_id == tenant_id,
                        ModelConfigurationVersionRow.version == version,
                    )
                )
            ).one()
            if (
                existing.model != model
                or existing.limits != limits.model_dump()
                or existing.export_enabled != export_enabled
            ):
                raise ValidationError("同一模型配置版本不可修改")
            await db.execute(
                insert(ModelConfigurationHeadRow)
                .values(tenant_id=tenant_id, version=version)
                .on_conflict_do_nothing(index_elements=["tenant_id"])
            )

    async def register_process(
        self,
        tenant_id: TenantId,
        process: Literal["api", "scheduler"],
        version: str,
        instance_id: str,
    ) -> None:
        async with self._factory() as db, db.begin():
            await db.execute(
                insert(ModelRuntimeProcessRow)
                .values(
                    tenant_id=tenant_id,
                    process=process,
                    version=version,
                    instance_id=instance_id,
                    heartbeat_at=self._now(),
                )
                .on_conflict_do_update(
                    index_elements=["tenant_id", "process"],
                    set_={
                        "version": version,
                        "instance_id": instance_id,
                        "heartbeat_at": self._now(),
                    },
                )
            )

    async def heartbeat(
        self,
        tenant_id: TenantId,
        process: Literal["api", "scheduler"],
        instance_id: str,
    ) -> None:
        async with self._factory() as db, db.begin():
            row = (
                await db.scalars(
                    select(ModelRuntimeProcessRow)
                    .where(
                        ModelRuntimeProcessRow.tenant_id == tenant_id,
                        ModelRuntimeProcessRow.process == process,
                        ModelRuntimeProcessRow.instance_id == instance_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                raise ValidationError("模型进程实例已失效")
            row.heartbeat_at = self._now()

    async def _head(
        self, db: AsyncSession, tenant_id: TenantId, *, lock: bool = False
    ) -> ModelConfigurationHeadRow:
        query = select(ModelConfigurationHeadRow).where(
            ModelConfigurationHeadRow.tenant_id == tenant_id
        )
        row = (
            await db.scalars(query.with_for_update() if lock else query)
        ).one_or_none()
        if row is None:
            raise ValidationError("模型配置不存在")
        return row

    async def get(self, tenant_id: TenantId) -> ModelConfigurationSnapshot | None:
        async with self._factory() as db:
            head = (
                await db.scalars(
                    select(ModelConfigurationHeadRow).where(
                        ModelConfigurationHeadRow.tenant_id == tenant_id
                    )
                )
            ).one_or_none()
            if head is None:
                return None
            config = (
                await db.scalars(
                    select(ModelConfigurationVersionRow).where(
                        ModelConfigurationVersionRow.tenant_id == tenant_id,
                        ModelConfigurationVersionRow.version == head.version,
                    )
                )
            ).one()
            processes = {
                p.process: p
                for p in (
                    await db.scalars(
                        select(ModelRuntimeProcessRow).where(
                            ModelRuntimeProcessRow.tenant_id == tenant_id
                        )
                    )
                ).all()
            }
            probe = (
                await db.scalars(
                    select(ModelProbeRow)
                    .where(
                        ModelProbeRow.tenant_id == tenant_id,
                        ModelProbeRow.configuration_version == head.version,
                    )
                    .order_by(
                        ModelProbeRow.created_at.desc(), ModelProbeRow.turn_id.desc()
                    )
                    .limit(1)
                )
            ).one_or_none()
            turn = None
            if probe is not None:
                turn = (
                    await db.scalars(
                        select(AgentTurnRow).where(
                            AgentTurnRow.tenant_id == tenant_id,
                            AgentTurnRow.turn_id == probe.turn_id,
                        )
                    )
                ).one()
            api, worker = processes.get("api"), processes.get("scheduler")
            return ModelConfigurationSnapshot(
                configuration_version=head.version,
                model=config.model,
                limits=ModelLimits.model_validate(config.limits),
                model_data_export_enabled=config.export_enabled,
                runtime=ModelRuntimeStatus.model_validate(
                    {
                        "api_version": api.version if api else None,
                        "scheduler_version": worker.version if worker else None,
                        "api_heartbeat_at": api.heartbeat_at if api else None,
                        "scheduler_heartbeat_at": worker.heartbeat_at
                        if worker
                        else None,
                        "verified_at": probe.verified_at
                        if probe and turn and turn.state == "completed"
                        else None,
                        "failure_code": turn.error_code if turn else None,
                        "probe_turn_id": probe.turn_id if probe else None,
                        "probe_state": turn.state if turn else None,
                    }
                ),
            )

    async def save(self, actor: AssistantActor, input: ModelSettingsUpdate) -> None:
        async with self._factory() as db, db.begin():
            head = await self._head(db, actor.tenant_id, lock=True)
            if head.version != input.expected_version:
                raise AssistantConflict()
            prior = (
                await db.scalars(
                    select(ModelConfigurationVersionRow).where(
                        ModelConfigurationVersionRow.tenant_id == actor.tenant_id,
                        ModelConfigurationVersionRow.version == head.version,
                    )
                )
            ).one()
            version = new_id("mcv")
            db.add(
                ModelConfigurationVersionRow(
                    tenant_id=actor.tenant_id,
                    version=version,
                    model=input.model,
                    limits=input.limits.model_dump(),
                    export_enabled=prior.export_enabled
                    if input.model_data_export_enabled is None
                    else input.model_data_export_enabled,
                    created_by=actor.user_id,
                    created_at=self._now(),
                )
            )
            await db.flush()
            head.version = version

    async def probe(
        self, actor: AssistantActor, configuration_version: str, idempotency_key: str
    ) -> TurnView:
        AssistantRegenerateInput(idempotency_key=idempotency_key)
        async with self._factory() as db, db.begin():
            head = await self._head(db, actor.tenant_id, lock=True)
            if head.version != configuration_version:
                raise AssistantConflict()
            existing = (
                await db.scalars(
                    select(ModelProbeRow).where(
                        ModelProbeRow.tenant_id == actor.tenant_id,
                        ModelProbeRow.employee_id == actor.employee_id,
                        ModelProbeRow.configuration_version == configuration_version,
                        ModelProbeRow.idempotency_key == idempotency_key,
                    )
                )
            ).one_or_none()
            if existing:
                row = (
                    await db.scalars(
                        select(AgentTurnRow).where(
                            AgentTurnRow.tenant_id == actor.tenant_id,
                            AgentTurnRow.turn_id == existing.turn_id,
                        )
                    )
                ).one()
                return _turn(row)
            session_id, turn_id, run_id = new_id("ase"), new_id("atr"), new_id("run")
            now = self._now()
            digest, _ = self._fingerprints.fingerprint(
                (
                    actor.model_dump_json().encode(),
                    configuration_version.encode(),
                    idempotency_key.encode(),
                )
            )
            db.add(
                AgentSessionRow(
                    tenant_id=actor.tenant_id,
                    session_id=session_id,
                    user_id=actor.user_id,
                    employee_id=actor.employee_id,
                    created_at=now,
                    version=1,
                    session_kind="model_probe",
                )
            )
            await db.flush()
            row = AgentTurnRow(
                tenant_id=actor.tenant_id,
                turn_id=turn_id,
                session_id=session_id,
                run_id=run_id,
                idempotency_key=idempotency_key,
                request_hmac=digest,
                input_text="管理员显式模型连接测试",
                object_refs=[],
                context_refs=[],
                result=None,
                state="queued",
                dispatch_state="pending",
                turn_kind="model_probe",
                created_at=now,
            )
            db.add(row)
            await db.flush()
            db.add(
                ModelProbeRow(
                    tenant_id=actor.tenant_id,
                    turn_id=turn_id,
                    employee_id=actor.employee_id,
                    configuration_version=configuration_version,
                    idempotency_key=idempotency_key,
                    created_at=now,
                )
            )
            await db.flush()
            return _turn(row)

    async def probe_version(self, tenant_id: TenantId, turn_id: AgentTurnId) -> str:
        async with self._factory() as db:
            row = (
                await db.scalars(
                    select(ModelProbeRow).where(
                        ModelProbeRow.tenant_id == tenant_id,
                        ModelProbeRow.turn_id == turn_id,
                    )
                )
            ).one_or_none()
            if row is None:
                raise ValidationError("模型探测不存在")
            return row.configuration_version

    async def complete_probe(
        self,
        actor: AssistantActor,
        turn_id: AgentTurnId,
        configuration_version: str,
        authorize: Callable[[], Awaitable[None]],
    ) -> None:
        async with self._factory() as db, db.begin():
            head = await self._head(db, actor.tenant_id, lock=True)
            if head.version != configuration_version:
                raise AssistantConflict()
            probe = (
                await db.scalars(
                    select(ModelProbeRow)
                    .where(
                        ModelProbeRow.tenant_id == actor.tenant_id,
                        ModelProbeRow.turn_id == turn_id,
                        ModelProbeRow.employee_id == actor.employee_id,
                        ModelProbeRow.configuration_version == configuration_version,
                    )
                    .with_for_update()
                )
            ).one()
            turn = (
                await db.scalars(
                    select(AgentTurnRow).where(
                        AgentTurnRow.tenant_id == actor.tenant_id,
                        AgentTurnRow.turn_id == turn_id,
                    )
                )
            ).one()
            _session = (
                await db.scalars(
                    select(AgentSessionRow)
                    .where(
                        AgentSessionRow.tenant_id == actor.tenant_id,
                        AgentSessionRow.session_id == turn.session_id,
                        AgentSessionRow.employee_id == actor.employee_id,
                        AgentSessionRow.user_id == actor.user_id,
                    )
                    .with_for_update()
                )
            ).one()
            employee = (
                await db.scalars(
                    select(EmployeeRow)
                    .where(
                        EmployeeRow.tenant_id == actor.tenant_id,
                        EmployeeRow.employee_id == actor.employee_id,
                    )
                    .with_for_update()
                )
            ).one_or_none()
            if employee is None:
                raise ValidationError("探测员工不存在")
            await authorize()
            await db.refresh(turn)
            if turn.state != "running":
                raise AssistantConflict()
            turn.state = "completed"
            turn.result = None
            probe.verified_at = self._now()

    async def unregister_process(
        self,
        tenant_id: TenantId,
        process: Literal["api", "scheduler"],
        instance_id: str,
    ) -> None:
        from sqlalchemy import delete

        async with self._factory() as db, db.begin():
            await db.execute(
                delete(ModelRuntimeProcessRow).where(
                    ModelRuntimeProcessRow.tenant_id == tenant_id,
                    ModelRuntimeProcessRow.process == process,
                    ModelRuntimeProcessRow.instance_id == instance_id,
                )
            )
