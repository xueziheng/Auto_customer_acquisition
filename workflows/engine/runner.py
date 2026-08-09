"""状态机运行器接口 —— Phase 1 用 Postgres 实现，接口 Temporal 可替换。

可替换是 ADR 0003 成立的前提：状态、转换守卫、超时、重试、等待人工
这些概念都在接口层，换引擎不动流程定义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from shared.schemas.identifiers import RunId, TenantId


class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    WAITING_HUMAN = "waiting_human"
    """等待人工（审批/确认）。**一等公民状态**，不是用重试模拟的
    ——这是选状态机而不是任务队列的原因之一。"""

    WAITING_EVENT = "waiting_event"
    """等待外部事件（客户回复、审批结果）。"""

    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class StepDefinition:
    """流程步骤定义。

    字段：
        step_name
        handler_ref:      步骤 handler 的注册名。LLM/IO 只在 handler
                          内——状态转换本身是纯逻辑（将来 Temporal 的
                          Workflow/Activity 分界线）
        timeout:          超时时长，到期转 TIMED_OUT 并走 on_timeout
        max_retries:      TransientError 的重试上限
        retry_backoff:    重试间隔基数（指数退避）
        on_timeout:       超时后转到哪一步（None = 整个流程失败）
        wait_event_type:  WAITING_EVENT 步骤等的事件类型
        reminder_interval: WAITING_EVENT 周期提醒间隔；成功后按绝对计划重排自身
        reminder_handler_ref: 周期提醒 handler 的注册名
        inherit_planned_anchor: 是否从 predecessor 继承不可变计划锚点；默认关闭
    """

    step_name: str
    handler_ref: str
    timeout: timedelta | None = None
    max_retries: int = 3
    retry_backoff: timedelta = timedelta(seconds=30)
    on_timeout: str | None = None
    wait_event_type: str | None = None
    reminder_interval: timedelta | None = None
    reminder_handler_ref: str | None = None
    inherit_planned_anchor: bool = False


@dataclass(frozen=True)
class WorkflowDefinition:
    """流程定义：步骤序列 + 转换规则。

    ``transitions`` 是显式表（step → 后继候选），与域状态机同一
    风格：转换散在代码里迟早两处不一致。
    """

    workflow_type: str
    version: int
    steps: tuple[StepDefinition, ...]
    transitions: dict[str, tuple[str, ...]] = field(default_factory=dict)


@dataclass(frozen=True)
class ReminderInvocation:
    """引擎触发的单次 durable reminder；index 从 1 开始且重试不变。"""

    index: int
    scheduled_at: datetime


@dataclass
class WorkflowRun:
    """一次流程执行。持久化在 workflow_runs / workflow_steps 表。"""

    run_id: RunId
    tenant_id: TenantId
    workflow_type: str
    workflow_version: int
    subject_ref: str
    """流程作用的业务实体（enrollment / message / case 的 ID）。"""

    current_step: str
    status: StepStatus
    created_at: datetime
    next_poll_at: datetime | None = None
    retry_count: int = 0
    context: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None
    reminder: ReminderInvocation | None = None


@runtime_checkable
class StepHandler(Protocol):
    """步骤 handler。

    返回值语义：
        ("advance", next_step, patch)   推进到下一步
        ("wait", None, patch)           保持等待（事件/人工）
        ("complete", None, patch)       流程正常结束
        ("fail", reason, patch)         流程失败

    ``patch`` 合并进 run.context。handler 必须幂等——同一步可能被
    重复调用（scheduler 重扫、崩溃恢复）。
    """

    async def execute(
        self, run: WorkflowRun
    ) -> tuple[str, str | None, dict[str, Any]]: ...


@runtime_checkable
class WorkflowEngine(Protocol):
    """引擎接口。Phase 1 实现：Postgres 表 + 扫描。"""

    def register(self, definition: WorkflowDefinition) -> None: ...

    async def start(
        self,
        tenant_id: TenantId,
        workflow_type: str,
        subject_ref: str,
        initial_context: dict[str, Any],
        idempotency_key: str,
        *,
        scheduled_at: datetime | None = None,
    ) -> RunId:
        """启动流程。

        ``idempotency_key`` 必填：同一业务实体的同一类流程不重复启动
        （事件重复投递会重复触发 start）。
        """
        ...

    async def find_active_run(
        self,
        tenant_id: TenantId,
        workflow_type: str,
        subject_ref: str,
    ) -> RunId | None:
        """按租户、流程类型与业务主体查唯一运行中实例。

        终态实例不可见；若数据异常地产生多个运行中实例，必须失败关闭，
        不得任取一条继续投递事件。
        """
        ...

    async def poll_due(self, tenant_id: TenantId, limit: int) -> int:
        """扫描到期步骤并推进，返回推进数。``scheduler_worker`` 的主循环。

        实现要求：
        - ``FOR UPDATE SKIP LOCKED`` 取批——扫描周期重叠不能取到同一批
        - 每步推进在独立事务里：一步失败不影响本批其他步
        - TransientError 按 backoff 重排 ``next_poll_at``；超过
          max_retries 转 FAILED 并通知
        - 推进用步骤幂等键防重复执行
        """
        ...

    async def deliver_event(
        self,
        tenant_id: TenantId,
        run_id: RunId,
        event_type: str,
        payload: dict[str, Any],
    ) -> bool:
        """向 WAITING_EVENT 的流程投递事件（回复到了、审批有结果了）。
        返回是否已持久接受；幂等：重复投递同一事件不重复推进。"""
        ...

    async def has_delivered_event(
        self,
        tenant_id: TenantId,
        workflow_type: str,
        subject_ref: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> bool:
        """查询同租户/type/subject 是否已有同一事件的 durable 指纹证据。"""
        ...

    async def cancel(
        self, tenant_id: TenantId, run_id: RunId, reason: str
    ) -> None: ...
