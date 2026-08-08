"""Postgres 工作流引擎（ADR 0003 的 Phase 1 实现，对齐 runner.py Protocol）。

设计要点（与 0004 schema 一致）：
- ``register`` 内存注册流程定义：重复注册同 (type, version) 且定义相等 → 幂等
  no-op；冲突 → ValueError；step.handler_ref 必须能在注入 handler 映射中解析；
  transition 只允许引用已定义步骤。fail closed，不静默接受畸形定义。
- ``start`` 用 ``INSERT ... ON CONFLICT (tenant_id, idempotency_key) DO NOTHING``
  实现幂等；创建 run 与首步在同一事务原子提交（失败绝不只建一半）。
- ``poll_due`` 逐 step 独立事务 + ``FOR UPDATE SKIP LOCKED`` 领取（扫描周期重叠
  不能取到同一批）；一步永久失败记录可观测失败态后继续本批其他步骤。handler 抛
  ``TransientError`` 按 ``retry_backoff * 2^(attempt-1)`` 指数退避更新
  attempt/retry_count/next_poll_at/error，超过 ``max_retries`` 转 FAILED；
  其他异常一律永久失败且 ``last_error`` 只留异常类型名（不落消息/payload，防凭证）。
- ``deliver_event`` 仅同租户、目标 run 当前步骤为 WAITING_EVENT 且事件类型匹配时
  推进；事件指纹（事件类型 + 规范化 payload）持久化进 ``run.context`` 保留键实现
  durable 幂等——重复投递同一事件是 no-op，不同事件（不同 payload）才可推进。
- ``cancel`` 只取消同租户目标；重复 cancel 幂等；completed/failed 终态不得复活。

所有 SQL 都显式带 ``tenant_id`` 过滤（硬边界 8）。每次操作使用独立 session/事务，
引擎自身不持有跨调用事务状态。
"""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infra.db.tables import WorkflowRunRow, WorkflowStepRow
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import RunId, TenantId, new_id
from workflows.engine.runner import (
    StepDefinition,
    StepHandler,
    StepStatus,
    WorkflowDefinition,
    WorkflowRun,
)

# run.context 保留键：已投递事件指纹（durable 幂等，独立于业务 patch）。
_DELIVERED_EVENTS_KEY = "__wf_delivered_events"
# run.context 保留键：最近一次投递的事件（handler 经 run.context["event"] 读取）。
_EVENT_KEY = "event"

# run 终态：已完成/失败/取消不得再推进、投递或取消复活。
_TERMINAL_STATUSES = ("completed", "failed", "cancelled")
# 可被 poll_due 领取的 step 状态（'running' 仅作崩溃遗留兜底；本实现不写 running）。
_POLLABLE_STEP_STATUSES = ("pending", "running")


def _safe_error(exc: BaseException) -> str:
    """脱敏错误：只留异常类型名，不落异常消息/payload（防止凭证进 last_error）。"""
    return type(exc).__name__


def _event_fingerprint(event_type: str, payload: dict[str, Any]) -> str:
    """事件幂等指纹：规范化序列化 (event_type, payload)，跨进程稳定可比较。"""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return f"{event_type}:{canonical}"


class PostgresWorkflowEngine:
    """``WorkflowEngine`` 的 Postgres 实现（runner.py Protocol）。

    handler 通过构造注入（``Mapping[str, StepHandler]``，键为 ``handler_ref``）；
    时钟 ``now`` 可注入以便调度测试确定化。构造不触数据库。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        handlers: Mapping[str, StepHandler],
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._factory = session_factory
        self._handlers = dict(handlers)
        self._now = now if now is not None else lambda: datetime.now(UTC)
        self._definitions: dict[tuple[str, int], WorkflowDefinition] = {}

    # ---- register ---------------------------------------------------------

    def register(self, definition: WorkflowDefinition) -> None:
        """注册流程定义；重复/冲突版本行为确定：

        - 同 (workflow_type, version) 且定义相等 → 幂等 no-op。
        - 同 (workflow_type, version) 且定义不同 → ``ValueError``。
        - step.handler_ref 未注入 / 空 steps / 重复 step_name / transition 引用
          未知步骤 → ``ValueError``（fail closed）。
        """
        key = (definition.workflow_type, definition.version)
        if key in self._definitions:
            if self._definitions[key] != definition:
                raise ValueError(
                    f"workflow 定义冲突：{definition.workflow_type} v{definition.version} 已注册"
                )
            return
        names = [s.step_name for s in definition.steps]
        if not names:
            raise ValueError("workflow 定义至少需要一个 step")
        if len(set(names)) != len(names):
            raise ValueError("workflow 定义 step_name 不得重复")
        missing = [
            s.handler_ref for s in definition.steps if s.handler_ref not in self._handlers
        ]
        if missing:
            raise ValueError(f"未注册的 handler_ref：{sorted(set(missing))}")
        known = set(names)
        for source, dests in definition.transitions.items():
            if source not in known:
                raise ValueError(f"transition 源步骤未定义：{source}")
            unknown = sorted(d for d in dests if d not in known)
            if unknown:
                raise ValueError(f"transition 目标步骤未定义：{unknown}")
        self._definitions[key] = definition

    def _definition_for(self, workflow_type: str) -> WorkflowDefinition:
        """按 workflow_type 取最新已注册版本；未注册 fail closed（``ValidationError``）。"""
        candidates = [
            d for d in self._definitions.values() if d.workflow_type == workflow_type
        ]
        if not candidates:
            raise ValidationError(f"workflow {workflow_type} 未注册")
        return max(candidates, key=lambda d: d.version)

    # ---- start ------------------------------------------------------------

    async def start(
        self,
        tenant_id: TenantId,
        workflow_type: str,
        subject_ref: str,
        initial_context: dict[str, Any],
        idempotency_key: str,
    ) -> RunId:
        """启动流程：创建 run 与首步原子提交；同 (tenant, key) 幂等返回既有 run。"""
        if not idempotency_key or not idempotency_key.strip():
            raise ValidationError("idempotency_key 必填且拒绝空白")
        definition = self._definition_for(workflow_type)
        first_step = definition.steps[0].step_name
        run_id = new_id("run")
        session = self._factory()
        try:
            result = await session.execute(
                insert(WorkflowRunRow)
                .values(
                    run_id=run_id,
                    tenant_id=tenant_id,
                    workflow_type=workflow_type,
                    workflow_version=definition.version,
                    subject_ref=subject_ref,
                    current_step=first_step,
                    status=StepStatus.RUNNING.value,
                    context=initial_context,
                    idempotency_key=idempotency_key,
                )
                .on_conflict_do_nothing(index_elements=["tenant_id", "idempotency_key"])
            )
            if cast(CursorResult, result).rowcount > 0:
                session.add(
                    self._new_step_row(
                        tenant_id=tenant_id,
                        run_id=run_id,
                        step_name=first_step,
                        due_at=self._now(),
                    )
                )
                await session.commit()
                return RunId(run_id)
            # 冲突：返回既有 run（Postgres 会等待并发插入的事务落地后才判冲突）。
            existing = (
                await session.execute(
                    select(WorkflowRunRow.run_id).where(
                        WorkflowRunRow.tenant_id == tenant_id,
                        WorkflowRunRow.idempotency_key == idempotency_key,
                    )
                )
            ).scalar_one()
            await session.rollback()
            return RunId(existing)
        finally:
            await session.close()

    # ---- poll_due ---------------------------------------------------------

    async def poll_due(self, tenant_id: TenantId, limit: int) -> int:
        """扫描到期步骤并推进，返回领取并处理的步数（scheduler 主循环）。

        逐 step 独立事务：``FOR UPDATE SKIP LOCKED`` 领取；handler 的
        TransientError 退避/永久失败都在同事务提交，不阻塞本批其他步骤。
        """
        if limit <= 0:
            return 0
        now = self._now()
        processed = 0
        for _ in range(limit):
            session = self._factory()
            try:
                step_row = (
                    await session.execute(
                        select(WorkflowStepRow)
                        .where(
                            WorkflowStepRow.tenant_id == tenant_id,
                            WorkflowStepRow.status.in_(_POLLABLE_STEP_STATUSES),
                            WorkflowStepRow.due_at <= now,
                        )
                        .order_by(
                            WorkflowStepRow.due_at.asc(),
                            WorkflowStepRow.step_id.asc(),
                        )
                        .limit(1)
                        .with_for_update(skip_locked=True)
                    )
                ).scalars().first()
                if step_row is None:
                    await session.rollback()
                    break
                run_row = (
                    await session.execute(
                        select(WorkflowRunRow)
                        .where(
                            WorkflowRunRow.tenant_id == tenant_id,
                            WorkflowRunRow.run_id == step_row.run_id,
                        )
                        .with_for_update()
                    )
                ).scalars().first()
                if run_row is None:
                    # 与复合 FK 不一致的孤儿步骤（正常不应出现）：标记失败避免紧循环。
                    step_row.status = "failed"
                    step_row.error = "run row missing"
                    step_row.updated_at = now
                    await session.commit()
                    processed += 1
                    continue
                await self._process_step(session, step_row, run_row, now)
                await session.commit()
                processed += 1
            finally:
                await session.close()
        return processed

    async def _process_step(
        self,
        session: AsyncSession,
        step_row: WorkflowStepRow,
        run_row: WorkflowRunRow,
        now: datetime,
    ) -> None:
        """领取后处理单个步骤：解析定义/handler → 执行 → 应用转换或退避/失败。"""
        definition = self._definitions.get((run_row.workflow_type, run_row.workflow_version))
        if definition is None:
            self._fail_run(session, step_row, run_row, now, "workflow definition not registered")
            return
        step_def: StepDefinition | None = None
        for step in definition.steps:
            if step.step_name == step_row.step_name:
                step_def = step
                break
        if step_def is None:
            self._fail_run(
                session, step_row, run_row, now,
                f"step {step_row.step_name} not in definition",
            )
            return
        handler = self._handlers.get(step_def.handler_ref)
        if handler is None:
            self._fail_run(
                session, step_row, run_row, now,
                f"handler {step_def.handler_ref} not registered",
            )
            return
        run = self._row_to_run(run_row)
        try:
            action, next_step, patch = await handler.execute(run)
        except TransientError as exc:
            self._schedule_retry(session, step_def, step_row, run_row, now, exc)
            return
        # handler 抛任意非 TransientError 异常都必须进入可观测永久失败态（不得
        # 伪装成可重试）；这正是 brief「永久错误必须可观测」的语义要求。
        except Exception as exc:  # noqa: BLE001
            self._fail_run(session, step_row, run_row, now, _safe_error(exc))
            return
        try:
            self._apply_transition(
                session, definition, step_def, step_row, run_row,
                action, next_step, patch, now,
            )
        except ValueError as exc:
            self._fail_run(session, step_row, run_row, now, _safe_error(exc))

    def _apply_transition(
        self,
        session: AsyncSession,
        definition: WorkflowDefinition,
        step_def: StepDefinition,
        step_row: WorkflowStepRow,
        run_row: WorkflowRunRow,
        action: str,
        next_step: str | None,
        patch: dict[str, Any],
        now: datetime,
    ) -> None:
        """先校验后变更：非法 transition/action 在触碰任何行前抛 ``ValueError``。

        patch 确定性浅合并进 run.context（后写覆盖先写）。advance 创建的下一
        step 带 durable 幂等键 ``{tenant}:wfstep:{run_id}:{step_name}``。
        """
        if action not in ("advance", "wait", "complete", "fail"):
            raise ValueError(f"未知 handler action：{action!r}")
        if action == "advance":
            allowed = definition.transitions.get(run_row.current_step, ())
            if next_step not in allowed:
                raise ValueError(
                    f"transition {run_row.current_step}→{next_step!r} 不在注册定义中"
                )
        merged = self._merged_context(run_row.context, patch)
        step_row.updated_at = now
        if action == "advance":
            next_name = cast(str, next_step)
            next_def = self._step_definition(definition, next_name)
            step_row.status = "completed"
            run_row.current_step = next_name
            run_row.status = StepStatus.RUNNING.value
            run_row.next_poll_at = None
            run_row.context = merged
            session.add(
                self._new_step_row(
                    tenant_id=run_row.tenant_id,
                    run_id=run_row.run_id,
                    step_name=next_name,
                    due_at=now,
                    # 等待事件的下一步直接进入 waiting_event：poll_due 不领取，
                    # 只由 deliver_event 触发（避免 handler 在无事件上下文中误跑）。
                    status=(
                        "waiting_event" if next_def.wait_event_type else "pending"
                    ),
                )
            )
        elif action == "wait":
            step_row.status = (
                "waiting_event" if step_def.wait_event_type else "waiting_human"
            )
            run_row.next_poll_at = None
            run_row.context = merged
        elif action == "complete":
            step_row.status = "completed"
            run_row.status = StepStatus.COMPLETED.value
            run_row.next_poll_at = None
            run_row.context = merged
        else:  # action == "fail"
            step_row.status = "failed"
            run_row.status = StepStatus.FAILED.value
            run_row.next_poll_at = None
            run_row.last_error = next_step or "handler declared failure"
            run_row.context = merged

    def _schedule_retry(
        self,
        session: AsyncSession,
        step_def: StepDefinition,
        step_row: WorkflowStepRow,
        run_row: WorkflowRunRow,
        now: datetime,
        exc: TransientError,
    ) -> None:
        """TransientError：指数退避重排 next_poll_at；超过 max_retries 转 FAILED。"""
        step_row.attempt += 1
        run_row.retry_count += 1
        step_row.error = _safe_error(exc)
        run_row.last_error = _safe_error(exc)
        step_row.updated_at = now
        if step_row.attempt > step_def.max_retries:
            step_row.status = "failed"
            run_row.status = StepStatus.FAILED.value
            run_row.next_poll_at = None
            run_row.last_error = (
                f"step {step_row.step_name} failed after exhausting "
                f"{step_def.max_retries} retries"
            )
        else:
            backoff = step_def.retry_backoff * (2 ** (step_row.attempt - 1))
            next_at = now + backoff
            step_row.status = "pending"
            step_row.due_at = next_at
            run_row.next_poll_at = next_at

    def _fail_run(
        self,
        session: AsyncSession,
        step_row: WorkflowStepRow,
        run_row: WorkflowRunRow,
        now: datetime,
        reason: str,
    ) -> None:
        """永久失败：step 与 run 转 FAILED；last_error 用脱敏 reason，不越权改 current_step。"""
        step_row.status = "failed"
        step_row.error = reason
        step_row.updated_at = now
        run_row.status = StepStatus.FAILED.value
        run_row.next_poll_at = None
        run_row.last_error = f"step {step_row.step_name} failed: {reason}"

    # ---- deliver_event ----------------------------------------------------

    async def deliver_event(
        self,
        tenant_id: TenantId,
        run_id: RunId,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """向 WAITING_EVENT 的流程投递事件；重复投递同一事件幂等 no-op。

        仅同租户、目标 run 当前步骤 WAITING_EVENT 且事件类型匹配时推进；事件
        指纹持久化进 run.context 实现 durable 幂等。错误租户/未知 run 一律
        no-op（不越权读取或修改）。handler 抛 TransientError → 回滚并传播，
        由调用方（outbox）稍后重投同一事件；永久错误 → 标记 FAILED 后正常返回。
        """
        now = self._now()
        session = self._factory()
        try:
            run_row = (
                await session.execute(
                    select(WorkflowRunRow)
                    .where(
                        WorkflowRunRow.tenant_id == tenant_id,
                        WorkflowRunRow.run_id == run_id,
                    )
                    .with_for_update()
                )
            ).scalars().first()
            if run_row is None or run_row.status in _TERMINAL_STATUSES:
                return
            step_row = (
                await session.execute(
                    select(WorkflowStepRow)
                    .where(
                        WorkflowStepRow.tenant_id == tenant_id,
                        WorkflowStepRow.run_id == run_id,
                        WorkflowStepRow.step_name == run_row.current_step,
                    )
                )
            ).scalars().first()
            if step_row is None or step_row.status != "waiting_event":
                return
            definition = self._definitions.get((run_row.workflow_type, run_row.workflow_version))
            if definition is None:
                self._fail_run(session, step_row, run_row, now, "workflow definition not registered")
                await session.commit()
                return
            step_def = self._step_definition(definition, step_row.step_name)
            if step_def.wait_event_type != event_type:
                return
            ctx: dict[str, Any] = dict(run_row.context or {})
            delivered: list[Any] = ctx.setdefault(_DELIVERED_EVENTS_KEY, [])
            fingerprint = _event_fingerprint(event_type, payload)
            if fingerprint in delivered:
                return
            ctx[_EVENT_KEY] = {"event_type": event_type, "payload": payload}
            run = self._row_to_run(run_row, context=ctx)
            handler = self._handlers.get(step_def.handler_ref)
            if handler is None:
                self._fail_run(
                    session, step_row, run_row, now,
                    f"handler {step_def.handler_ref} not registered",
                )
                await session.commit()
                return
            try:
                action, next_step, patch = await handler.execute(run)
                delivered.append(fingerprint)
                run_row.context = ctx
                self._apply_transition(
                    session, definition, step_def, step_row, run_row,
                    action, next_step, patch, now,
                )
            except TransientError:
                # 可重试投递：回滚、不落指纹，调用方稍后重投同一事件。
                await session.rollback()
                raise
            # 同 poll_due：非 TransientError 异常转可观测永久失败。
            except Exception as exc:  # noqa: BLE001
                self._fail_run(session, step_row, run_row, now, _safe_error(exc))
            await session.commit()
        finally:
            await session.close()

    # ---- cancel -----------------------------------------------------------

    async def cancel(
        self, tenant_id: TenantId, run_id: RunId, reason: str
    ) -> None:
        """取消同租户目标 run：置 cancelled 并取消未终态步骤；重复 cancel 幂等；
        completed/failed 终态不被复活；未知 run no-op。"""
        session = self._factory()
        try:
            run_row = (
                await session.execute(
                    select(WorkflowRunRow)
                    .where(
                        WorkflowRunRow.tenant_id == tenant_id,
                        WorkflowRunRow.run_id == run_id,
                    )
                    .with_for_update()
                )
            ).scalars().first()
            if run_row is None or run_row.status in _TERMINAL_STATUSES:
                return
            run_row.status = StepStatus.CANCELLED.value
            run_row.next_poll_at = None
            run_row.last_error = reason
            await session.execute(
                update(WorkflowStepRow)
                .where(
                    WorkflowStepRow.tenant_id == tenant_id,
                    WorkflowStepRow.run_id == run_id,
                    WorkflowStepRow.status.not_in(("completed", "failed", "cancelled")),
                )
                .values(status="cancelled", updated_at=self._now())
            )
            await session.commit()
        finally:
            await session.close()

    # ---- helpers ----------------------------------------------------------

    @staticmethod
    def _step_definition(definition: WorkflowDefinition, step_name: str) -> StepDefinition:
        for step in definition.steps:
            if step.step_name == step_name:
                return step
        raise ValueError(f"定义 {definition.workflow_type} 中无步骤 {step_name}")

    @staticmethod
    def _merged_context(
        current: dict[str, Any], patch: dict[str, Any]
    ) -> dict[str, Any]:
        merged = dict(current)
        if patch:
            merged.update(patch)
        return merged

    def _new_step_row(
        self,
        *,
        tenant_id: str,
        run_id: str,
        step_name: str,
        due_at: datetime,
        status: str = "pending",
    ) -> WorkflowStepRow:
        """新建 step 行：durable 幂等键 = {tenant}:wfstep:{run_id}:{step_name}。"""
        return WorkflowStepRow(
            step_id=new_id("wfs"),
            run_id=run_id,
            tenant_id=tenant_id,
            step_name=step_name,
            status=status,
            data={},
            attempt=0,
            error=None,
            due_at=due_at,
            idempotency_key=f"{tenant_id}:wfstep:{run_id}:{step_name}",
        )

    def _row_to_run(
        self,
        row: WorkflowRunRow,
        *,
        context: dict[str, Any] | None = None,
    ) -> WorkflowRun:
        return WorkflowRun(
            run_id=RunId(row.run_id),
            tenant_id=TenantId(row.tenant_id),
            workflow_type=row.workflow_type,
            workflow_version=row.workflow_version,
            subject_ref=row.subject_ref,
            current_step=row.current_step,
            status=StepStatus(row.status),
            created_at=row.created_at,
            next_poll_at=row.next_poll_at,
            retry_count=row.retry_count,
            context=context if context is not None else dict(row.context or {}),
            last_error=row.last_error,
        )
