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
  **commit/flush/DB 级异常也在迭代内隔离**：回滚后用新事务按 (tenant, step_id,
  run_id) 重定位锁定，若仍可推进则标记 FAILED（固定脱敏错误），同一批其余步骤
  不中断、坏 step 不被同批重复领取。
- ``deliver_event`` 仅同租户、目标 run 当前步骤为 WAITING_EVENT 且事件类型匹配时
  推进；事件指纹为 **SHA-256 digest（固定 64 位 hex，不含 payload 原文）**，
  durable 持久化在 ``run.context`` 保留键实现幂等——重复投递同一事件是 no-op，
  不同事件（不同 payload）才可推进；raw event 只在 handler execute 期间可见，
  transition 后从持久化 context 移除。``initial_context``/handler patch 含保留键
  一律 fail closed（ValidationError/永久失败），DB 中损坏的保留键值也 fail closed。
- ``cancel`` 只取消同租户目标；重复 cancel 幂等；completed/failed 终态不得复活；
  锁序 step→run（与 poll_due 一致，避免死锁倒置）；``last_error`` 只写固定安全
  状态文本，不落调用方原始 reason（防凭证）。

所有 SQL 都显式带 ``tenant_id`` 过滤（硬边界 8）。每次操作使用独立 session/事务，
引擎自身不持有跨调用事务状态。
"""
from __future__ import annotations

import hashlib
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
# 只允许在 handler execute 期间临时可见；transition 后从持久化 context 移除。
_EVENT_KEY = "event"
# 引擎保留键集合：initial_context / handler patch 一律不得携带（fail closed）。
_RESERVED_CONTEXT_KEYS = (_DELIVERED_EVENTS_KEY, _EVENT_KEY)

# run 终态：已完成/失败/取消不得再推进、投递或取消复活。
_TERMINAL_STATUSES = ("completed", "failed", "cancelled")
# 可被 poll_due 领取的 step 状态（'running' 仅作崩溃遗留兜底；本实现不写 running）。
_POLLABLE_STEP_STATUSES = ("pending", "running")

# 固定脱敏错误文本：不依赖具体异常包装类型、不含异常消息/payload（防凭证落库）。
_COMMIT_FAILURE_ERROR = "step commit failure"
_CORRUPTED_CONTEXT_ERROR = "corrupted workflow context"
_HANDLER_FAILED_REASON = "handler declared failure"
_CANCEL_REASON = "cancelled by operator"


def _safe_error(exc: BaseException) -> str:
    """脱敏错误：只留异常类型名，不落异常消息/payload（防止凭证进 last_error）。"""
    return type(exc).__name__


def _reserved_key_hits(context: dict[str, Any]) -> list[str]:
    """返回 context 中命中的引擎保留键（空列表 = 干净）。"""
    return sorted(k for k in _RESERVED_CONTEXT_KEYS if k in context)


def _event_fingerprint(event_type: str, payload: dict[str, Any]) -> str:
    """事件幂等指纹：对 (event_type, 规范化 payload) 做 SHA-256，返回固定 64 位 hex。

    绝不保存 canonical payload 文本（防凭证/payload 原文持久化）；跨进程稳定，
    同事件同 payload 哈希相同，不同 payload 哈希不同。
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{event_type}:{canonical}".encode()).hexdigest()


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
        # 非正退避/负重试上限会在零退避下立即重领直到耗尽，违反 no tight loop；fail closed。
        for step in definition.steps:
            if step.max_retries < 0:
                raise ValueError(
                    f"step {step.step_name} max_retries 必须 >= 0（当前 {step.max_retries}）"
                )
            if step.retry_backoff.total_seconds() <= 0:
                raise ValueError(
                    f"step {step.step_name} retry_backoff 必须 > 0（当前 {step.retry_backoff}）"
                )
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
        hits = _reserved_key_hits(initial_context)
        if hits:
            raise ValidationError(f"initial_context 不得含引擎保留键：{hits}")
        definition = self._definition_for(workflow_type)
        first_step = definition.steps[0]
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
                    current_step=first_step.step_name,
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
                        step_name=first_step.step_name,
                        due_at=self._now(),
                        # 首步本身等待事件时直接进入 waiting_event：poll_due 不领取，
                        # 只由 deliver_event 触发（与 advance 到等待步骤语义一致）。
                        status=(
                            "waiting_event" if first_step.wait_event_type else "pending"
                        ),
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
        **commit/flush/DB 级异常也在迭代内隔离**：回滚后用新事务把该 step/run
        标记 FAILED（固定脱敏错误），本批其余步骤不中断；坏 step 记入
        ``tried_step_ids`` 避免同批立即重复领取（无 tight loop）。
        """
        if limit <= 0:
            return 0
        now = self._now()
        processed = 0
        tried_step_ids: set[str] = set()
        for _ in range(limit):
            session = self._factory()
            step_row: WorkflowStepRow | None = None
            try:
                step_row = (
                    await session.execute(
                        select(WorkflowStepRow)
                        .where(
                            WorkflowStepRow.tenant_id == tenant_id,
                            WorkflowStepRow.status.in_(_POLLABLE_STEP_STATUSES),
                            WorkflowStepRow.due_at <= now,
                            WorkflowStepRow.step_id.not_in(tried_step_ids),
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
                # commit/flush 前先取出原始主键：rollback 会使 ORM 实例过期，except
                # 路径再访问 step_row.run_id 会触发延迟刷新（MissingGreenlet），掩盖
                # 真正的 DB 异常。
                step_run_id = step_row.run_id
                step_id = step_row.step_id
                tried_step_ids.add(step_id)
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
            except Exception:  # noqa: BLE001  commit/flush/DB 级异常：迭代内隔离
                await session.rollback()
                if step_row is not None:
                    await self._mark_step_commit_failed(
                        tenant_id, step_run_id, step_id, now
                    )
                    processed += 1
            finally:
                await session.close()
        return processed

    async def _mark_step_commit_failed(
        self, tenant_id: TenantId, run_id: str, step_id: str, now: datetime
    ) -> None:
        """commit/flush 失败后，在新事务按 (tenant, step_id, run_id) 重新定位并锁定。

        若 step 仍为可推进状态且 run 非终态，则标记 step/run FAILED（固定脱敏错误，
        不含异常类型/消息）；终态/cancelled 不得被复活。即使失败标记本身出错也不
        抛出——本批其余步骤必须继续（该 step 已由调用方记入 tried 集合避免重领）。
        锁序 step→run，与 poll_due/cancel 一致，避免死锁倒置。
        """
        session = self._factory()
        try:
            step_row = (
                await session.execute(
                    select(WorkflowStepRow)
                    .where(
                        WorkflowStepRow.tenant_id == tenant_id,
                        WorkflowStepRow.run_id == run_id,
                        WorkflowStepRow.step_id == step_id,
                    )
                    .with_for_update()
                )
            ).scalars().first()
            if step_row is None or step_row.status not in _POLLABLE_STEP_STATUSES:
                await session.rollback()
                return
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
                await session.rollback()
                return
            self._fail_run(session, step_row, run_row, now, _COMMIT_FAILURE_ERROR)
            await session.commit()
        except Exception:  # noqa: BLE001  失败标记本身失败：不得中断本批
            await session.rollback()
        finally:
            await session.close()

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
        elif action in ("wait", "complete"):
            # wait/complete 的第二个字段语义上必须为 None；携带 next_step 属非法转换。
            if next_step is not None:
                raise ValueError(f"{action} action 不得携带 next_step：{next_step!r}")
        hits = _reserved_key_hits(patch)
        if hits:
            raise ValueError(f"handler patch 不得含引擎保留键：{hits}")
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
            # handler 声明的 fail reason 不得原样写入 error 字段（可能含凭证）：
            # 只写固定安全状态文本，不落原始 reason / 异常消息 / payload。
            step_row.status = "failed"
            run_row.status = StepStatus.FAILED.value
            run_row.next_poll_at = None
            step_row.error = _HANDLER_FAILED_REASON
            run_row.last_error = f"step {step_row.step_name} failed: {_HANDLER_FAILED_REASON}"
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
            # ``event`` 是瞬态键（handler 执行期可见，transition 后不持久化）；
            # 清除历史实现可能遗留的脏数据，避免其进入本投递的持久化 context。
            ctx.pop(_EVENT_KEY, None)
            delivered: object = ctx.get(_DELIVERED_EVENTS_KEY)
            if delivered is None:
                delivered = []
                ctx[_DELIVERED_EVENTS_KEY] = delivered
            if not isinstance(delivered, list) or not all(
                isinstance(d, str) for d in delivered
            ):
                # DB 中遗留/损坏的保留键值：fail closed（固定脱敏错误），不得
                # AttributeError/追加到错误类型后卡住投递。
                self._fail_run(session, step_row, run_row, now, _CORRUPTED_CONTEXT_ERROR)
                await session.commit()
                return
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
                # raw event 只在 handler 执行期可见；transition 后从持久化 context 移除。
                ctx.pop(_EVENT_KEY, None)
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
        completed/failed 终态不被复活；未知 run no-op。

        锁序 step→run（与 poll_due 的 step→run 一致，避免死锁倒置）：先锁定并
        取消未终态步骤，再锁定 run 检查终态——若 run 已终态/未知则整体回滚，不
        复活、不部分修改。``last_error`` 只写固定安全状态文本，不落调用方原始
        ``reason``（可能含凭证）；``reason`` 参数保留仅为满足 Protocol 签名。
        """
        now = self._now()
        session = self._factory()
        try:
            await session.execute(
                update(WorkflowStepRow)
                .where(
                    WorkflowStepRow.tenant_id == tenant_id,
                    WorkflowStepRow.run_id == run_id,
                    WorkflowStepRow.status.not_in(("completed", "failed", "cancelled")),
                )
                .values(status="cancelled", updated_at=now)
            )
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
                # 未知 run 或终态 run：整体回滚（不复活、不部分修改步骤）。
                await session.rollback()
                return
            run_row.status = StepStatus.CANCELLED.value
            run_row.next_poll_at = None
            run_row.last_error = _CANCEL_REASON
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
