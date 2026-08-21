"""受控 Agent 任务执行进程。

生产 composition 只可注入已经实现租户过滤和 ``FOR UPDATE SKIP LOCKED`` 的
持久化 job repository。进程只编排安全生命周期，不读取模型凭证，也不记录任务
payload、模型输入或 ChangeSet 内容。
"""

from __future__ import annotations

import asyncio
import logging
import math
import signal
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

from agent_runtime.base import AgentTask, ChangeSet
from shared.errors import TradeOSError, ValidationError
from shared.schemas.identifiers import ChangeSetId

logger = logging.getLogger(__name__)

_EXIT_RUNTIME_NOT_CONFIGURED = 2


class WorkerReadinessError(RuntimeError):
    """worker 在领取任务前发现 schema 或能力注册表不完整。"""


@dataclass(frozen=True)
class AgentJob:
    """已通过审批闸门、可交给某项 Phase 1 能力执行的任务。"""

    job_id: str
    capability: str
    task: AgentTask

    def __post_init__(self) -> None:
        if (
            not self.job_id
            or self.job_id != self.job_id.strip()
            or len(self.job_id) > 128
            or not self.capability
            or self.capability != self.capability.strip()
            or len(self.capability) > 64
        ):
            raise ValidationError("agent job 元数据无效")


class AgentJobRepository(Protocol):
    """durable job 队列端口。

    ``claim_approved`` 的实现必须同时绑定 tenant、approved 状态和租约条件，按
    ``FOR UPDATE SKIP LOCKED`` 有界领取；不得返回尚未批准的 Trade Run。
    """

    async def assert_schema_current(self) -> None: ...

    async def claim_approved(self, *, limit: int) -> tuple[AgentJob, ...]: ...

    async def complete(
        self, *, job_id: str, change_set_id: ChangeSetId
    ) -> None: ...

    async def fail(
        self, *, job_id: str, category: str, retryable: bool
    ) -> None: ...


class AgentRunner(Protocol):
    """已在 composition 中完成模型计量、Tool Gateway 和护栏注入的能力。"""

    name: str

    async def run(self, task: AgentTask, context: object) -> ChangeSet: ...


class ContextBuilder(Protocol):
    """按任务身份裁剪上下文；不得把凭证放入结果。"""

    async def build(self, task: AgentTask) -> object: ...


class ChangeSetGate(Protocol):
    """ChangeSet 落库前的统一护栏与审批分流端口。"""

    async def accept(self, change_set: ChangeSet) -> None: ...


@dataclass(frozen=True)
class AgentWorkerConfig:
    """有界领取与空闲等待配置。"""

    batch_limit: int
    idle_seconds: float
    required_capabilities: tuple[str, ...] = ("demand_intelligence",)

    def __post_init__(self) -> None:
        if (
            isinstance(self.batch_limit, bool)
            or not isinstance(self.batch_limit, int)
            or self.batch_limit < 1
        ):
            raise ValidationError("agent worker batch_limit 必须为正整数")
        if (
            isinstance(self.idle_seconds, bool)
            or not isinstance(self.idle_seconds, (int, float))
            or not math.isfinite(self.idle_seconds)
            or self.idle_seconds <= 0
        ):
            raise ValidationError("agent worker idle_seconds 必须为正数")
        if not self.required_capabilities or any(
            not name or name != name.strip() for name in self.required_capabilities
        ):
            raise ValidationError("agent worker required_capabilities 无效")


@dataclass(frozen=True)
class AgentWorkerRuntime:
    """已装配 runtime；不允许进程在运行中动态补空 registry。"""

    jobs: AgentJobRepository
    agents: Mapping[str, AgentRunner]
    contexts: ContextBuilder
    gate: ChangeSetGate
    config: AgentWorkerConfig


@dataclass(frozen=True)
class AgentWorkerResult:
    """一次进程生命周期的安全计数。"""

    jobs_completed: int
    jobs_failed: int


WaitForWork = Callable[[float, asyncio.Event], Awaitable[None]]


class RuntimeFactory(Protocol):
    """生产 composition 的资源生命周期入口。"""

    def __call__(self) -> AbstractAsyncContextManager[AgentWorkerRuntime]: ...


async def _default_wait(seconds: float, stop_event: asyncio.Event) -> None:
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except TimeoutError:
        return


def _install_stop_signals(stop_event: asyncio.Event) -> Callable[[], None]:
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except (NotImplementedError, RuntimeError):
            logger.warning("agent worker 信号处理器不可用", extra={"signal": signum.name})
        else:
            installed.append(signum)

    def cleanup() -> None:
        for signum in installed:
            loop.remove_signal_handler(signum)

    return cleanup


def _error_disposition(error: BaseException) -> tuple[str, bool]:
    if isinstance(error, TradeOSError):
        category = "transient" if error.is_retryable else "permanent"
        return category, error.is_retryable
    return "unexpected", False


async def run_agent_worker(
    runtime: AgentWorkerRuntime,
    *,
    stop_event: asyncio.Event | None = None,
    ready_event: asyncio.Event | None = None,
    wait: WaitForWork | None = None,
    install_signal_handlers: bool = True,
) -> AgentWorkerResult:
    """校验 readiness 后循环执行已批准任务，SIGTERM 不取消当前任务。"""

    await runtime.jobs.assert_schema_current()
    missing = sorted(set(runtime.config.required_capabilities) - set(runtime.agents))
    if missing:
        raise WorkerReadinessError("registry_incomplete")

    stop = stop_event if stop_event is not None else asyncio.Event()
    wait_for_work = wait if wait is not None else _default_wait
    cleanup = _install_stop_signals(stop) if install_signal_handlers else lambda: None
    completed = 0
    failed = 0
    if ready_event is not None:
        ready_event.set()

    try:
        while not stop.is_set():
            jobs = await runtime.jobs.claim_approved(limit=runtime.config.batch_limit)
            if not jobs:
                await wait_for_work(runtime.config.idle_seconds, stop)
                continue
            for job in jobs:
                if stop.is_set():
                    break
                runner = runtime.agents.get(job.capability)
                if runner is None:
                    await runtime.jobs.fail(
                        job_id=job.job_id,
                        category="capability_not_registered",
                        retryable=False,
                    )
                    failed += 1
                    continue
                try:
                    context = await runtime.contexts.build(job.task)
                    change_set = await runner.run(job.task, context)
                    if (
                        change_set.tenant_id != job.task.tenant_id
                        or change_set.run_id != job.task.run_id
                    ):
                        raise ValidationError("ChangeSet 身份与 Agent job 不一致")
                    await runtime.gate.accept(change_set)
                    await runtime.jobs.complete(
                        job_id=job.job_id,
                        change_set_id=change_set.change_set_id,
                    )
                    completed += 1
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001 - 统一脱敏并标记 job
                    category, retryable = _error_disposition(error)
                    await runtime.jobs.fail(
                        job_id=job.job_id,
                        category=category,
                        retryable=retryable,
                    )
                    failed += 1
                    logger.error(
                        "agent job 执行失败",
                        extra={
                            "job_id": job.job_id,
                            "capability": job.capability,
                            "error_category": category,
                        },
                    )
    finally:
        cleanup()
        if ready_event is not None:
            ready_event.clear()
    return AgentWorkerResult(completed, failed)


async def _run_configured(factory: RuntimeFactory) -> None:
    async with factory() as runtime:
        await run_agent_worker(runtime)


def main(runtime_factory: RuntimeFactory | None = None) -> None:
    """生产入口；没有完整 composition 时以固定退出码失败关闭。"""

    if runtime_factory is None:
        logger.critical("agent worker runtime 未配置")
        raise SystemExit(_EXIT_RUNTIME_NOT_CONFIGURED)
    asyncio.run(_run_configured(runtime_factory))


if __name__ == "__main__":
    main()
