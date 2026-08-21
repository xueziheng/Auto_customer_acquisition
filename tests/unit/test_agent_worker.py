from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from agent_runtime.base import AgentTask, ChangeSet
from apps.agent_worker.main import (
    AgentJob,
    AgentWorkerConfig,
    AgentWorkerRuntime,
    WorkerReadinessError,
    run_agent_worker,
)
from shared.schemas.identifiers import (
    ChangeSetId,
    RunId,
    TenantId,
    UserId,
)


@dataclass
class Jobs:
    schema_ready: bool = True
    queued: list[AgentJob] = field(default_factory=list)
    claim_limits: list[int] = field(default_factory=list)
    completed: list[tuple[str, str]] = field(default_factory=list)
    failed: list[tuple[str, str, bool]] = field(default_factory=list)

    async def assert_schema_current(self) -> None:
        if not self.schema_ready:
            raise WorkerReadinessError("schema_not_current")

    async def claim_approved(self, *, limit: int) -> tuple[AgentJob, ...]:
        self.claim_limits.append(limit)
        claimed = tuple(self.queued[:limit])
        del self.queued[:limit]
        return claimed

    async def complete(self, *, job_id: str, change_set_id: ChangeSetId) -> None:
        self.completed.append((job_id, str(change_set_id)))

    async def fail(
        self, *, job_id: str, category: str, retryable: bool
    ) -> None:
        self.failed.append((job_id, category, retryable))


class Contexts:
    async def build(self, task: AgentTask) -> object:
        return {"run_id": str(task.run_id)}


class Gate:
    accepted: list[ChangeSet]

    def __init__(self) -> None:
        self.accepted = []

    async def accept(self, change_set: ChangeSet) -> None:
        self.accepted.append(change_set)


class Agent:
    name = "demand_intelligence"

    def __init__(
        self,
        stop: asyncio.Event | None = None,
        changes: list[dict[str, Any]] | None = None,
    ) -> None:
        self.stop = stop
        self.changes = changes or []
        self.calls: list[AgentTask] = []

    async def run(self, task: AgentTask, context: Any) -> ChangeSet:
        assert context == {"run_id": str(task.run_id)}
        self.calls.append(task)
        if self.stop is not None:
            self.stop.set()
        return ChangeSet(
            ChangeSetId("chg_01K39P9M5D6K4A91YEQ80EJZ0X"),
            task.tenant_id,
            task.run_id,
            changes=self.changes,
            summary="安全摘要",
        )


@pytest.mark.asyncio
async def test_worker_replaces_unsafe_changes_with_structured_rejection() -> None:
    stop = asyncio.Event()
    gate = Gate()
    unsafe_agent = Agent(
        stop,
        changes=[
            {
                "domain": "outreach",
                "operation": "create_draft",
                "payload": {
                    "subject": "Quotation update",
                    "body": "The price is USD 2.50.",
                    "target_language": "en",
                    "content_language": "en",
                },
            }
        ],
    )
    runtime = AgentWorkerRuntime(
        jobs=Jobs(queued=[job("guarded")]),
        agents={"demand_intelligence": unsafe_agent},
        contexts=Contexts(),
        gate=gate,
        config=AgentWorkerConfig(batch_limit=1, idle_seconds=1),
    )

    result = await run_agent_worker(
        runtime,
        stop_event=stop,
        install_signal_handlers=False,
    )

    assert result.jobs_completed == 1
    assert len(gate.accepted) == 1
    guarded = gate.accepted[0]
    assert guarded.change_set_id == ChangeSetId(
        "chg_01K39P9M5D6K4A91YEQ80EJZ0X"
    )
    assert guarded.changes == []
    assert guarded.summary == "模型输出被 Phase 1 护栏拦截"
    assert {item["rail"] for item in guarded.guardrail_violations} == {
        "no_forbidden_commitment"
    }


def job(suffix: str) -> AgentJob:
    return AgentJob(
        job_id=f"agent-job-{suffix}",
        capability="demand_intelligence",
        task=AgentTask(
            TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0X"),
            RunId("run_01K39P9M5D6K4A91YEQ80EJZ0X"),
            UserId("usr_01K39P9M5D6K4A91YEQ80EJZ0X"),
            "发现已批准市场中的公开需求信号",
        ),
    )


@pytest.mark.asyncio
async def test_worker_fails_closed_before_readiness_when_schema_is_stale() -> None:
    ready = asyncio.Event()
    runtime = AgentWorkerRuntime(
        jobs=Jobs(schema_ready=False),
        agents={"demand_intelligence": Agent()},
        contexts=Contexts(),
        gate=Gate(),
        config=AgentWorkerConfig(batch_limit=2, idle_seconds=1),
    )

    with pytest.raises(WorkerReadinessError, match="schema_not_current"):
        await run_agent_worker(runtime, ready_event=ready, install_signal_handlers=False)

    assert not ready.is_set()


@pytest.mark.asyncio
async def test_worker_validates_the_phase1_registry_before_claiming() -> None:
    jobs = Jobs(queued=[job("one")])
    runtime = AgentWorkerRuntime(
        jobs=jobs,
        agents={},
        contexts=Contexts(),
        gate=Gate(),
        config=AgentWorkerConfig(batch_limit=2, idle_seconds=1),
    )

    with pytest.raises(WorkerReadinessError, match="registry_incomplete"):
        await run_agent_worker(runtime, install_signal_handlers=False)

    assert jobs.claim_limits == []


@pytest.mark.asyncio
async def test_sigterm_boundary_finishes_current_job_and_stops_before_next() -> None:
    stop = asyncio.Event()
    jobs = Jobs(queued=[job("one"), job("two")])
    agent = Agent(stop)
    gate = Gate()
    runtime = AgentWorkerRuntime(
        jobs=jobs,
        agents={"demand_intelligence": agent},
        contexts=Contexts(),
        gate=gate,
        config=AgentWorkerConfig(batch_limit=2, idle_seconds=1),
    )

    result = await run_agent_worker(
        runtime,
        stop_event=stop,
        install_signal_handlers=False,
    )

    assert result.jobs_completed == 1
    assert jobs.claim_limits == [2]
    assert jobs.completed == [
        ("agent-job-one", "chg_01K39P9M5D6K4A91YEQ80EJZ0X")
    ]
    assert [item.job_id for item in jobs.queued] == []
    assert len(agent.calls) == 1
    assert gate.accepted[0].summary == "安全摘要"
