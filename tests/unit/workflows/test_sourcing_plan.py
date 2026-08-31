"""公开寻源计划授权、运行和保守恢复的应用层边界。"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from connectors.search_contracts import SearchCostStatus
from domains.sourcing.permissions import SourcingActor, SourcingScope
from domains.sourcing.schemas import (
    PublicSourcingPlanCommand,
    PublicSourcingQuery,
    SourcingUncertainExecutionReadView,
    SourcingUncertainReconciliationCommand,
)
from domains.sourcing.service import (
    PublicPlanStatus,
    PublicSourcingPlan,
    PublicSourcingRunView,
    SourcingReconciliationStatus,
    SourcingSearchExecution,
    SourcingSearchExecutionStatus,
    SourcingSearchReconciliation,
)
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import (
    ArtifactId,
    RunId,
    SourcingCaseId,
    SourcingPlanId,
    TenantId,
)
from tool_gateway.free_search_contracts import (
    SearchQuotaSnapshot,
    SearchReservation,
)
from workflows.engine.runner import StepStatus, WorkflowRun
from workflows.sourcing_case.application import (
    SourcingCaseApplication,
    SourcingPlanDeliveryError,
    SourcingPublicSearchBlockedError,
)
from workflows.sourcing_case.steps import AwaitPublicPlanStep

NOW = datetime(2026, 8, 31, 8, 0, tzinfo=UTC)
TENANT = TenantId("tenant-a")
CASE_ID = SourcingCaseId("src_case-a")
PLAN_ID = SourcingPlanId("spl_plan-a")
RUN_ID = RunId("run_sourcing-a")
PLAN_HASH = "a" * 64
REQUEST_KEY = "b" * 64
EVIDENCE_ID = ArtifactId("art_evidence-a")
BOSS = SourcingActor("boss-a", TENANT, SourcingScope.TENANT, "boss")
SOURCING = SourcingActor("sourcing-a", TENANT, SourcingScope.TENANT, "sourcing")


def _plan_command() -> PublicSourcingPlanCommand:
    return PublicSourcingPlanCommand(
        plan_id=PLAN_ID,
        case_id=CASE_ID,
        target_countries=("US",),
        product_category="hinges",
        queries=(
            PublicSourcingQuery(query_text="hinge factory US", target_country="US"),
        ),
        max_search_queries=1,
        max_pages_read=2,
        provider="tavily",
        search_depth="basic",
        usage_credits_remaining=10,
        worst_case_credits=2,
        version=1,
        expected_case_version=6,
    )


def _plan(status: PublicPlanStatus = PublicPlanStatus.AUTHORIZED) -> PublicSourcingPlan:
    created = PublicSourcingPlan.create(TENANT, _plan_command(), created_at=NOW)
    authorized = replace(
        created,
        plan_hash=PLAN_HASH,
        status=PublicPlanStatus.AUTHORIZED,
        confirmed_by="boss-a",
        confirmed_at=NOW,
        authorized_plan_hash=PLAN_HASH,
    )
    return (
        authorized
        if status is PublicPlanStatus.AUTHORIZED
        else replace(authorized, status=status)
    )


def _run(step: str = "await_public_plan") -> WorkflowRun:
    return WorkflowRun(
        run_id=RUN_ID,
        tenant_id=TENANT,
        workflow_type="sourcing_case",
        workflow_version=2,
        subject_ref=str(CASE_ID),
        current_step=step,
        status=StepStatus.RUNNING,
        created_at=NOW,
        context={"case_id": str(CASE_ID)},
    )


class _Sourcing:
    def __init__(self) -> None:
        self.plan = _plan()
        self.calls: list[str] = []
        self.execution = SourcingSearchExecution(
            execution_id="sse_execution-a",
            tenant_id=TENANT,
            case_id=CASE_ID,
            plan_id=PLAN_ID,
            run_id=str(RUN_ID),
            plan_hash=PLAN_HASH,
            query_index=0,
            request_key=REQUEST_KEY,
            query_hash="c" * 64,
            locator_results=(),
            provider_status=SourcingSearchExecutionStatus.UNCERTAIN,
            created_at=NOW,
        )

    async def save_public_plan(self, tenant_id, case_id, command, *, actor):
        self.calls.append("save")
        assert tenant_id == TENANT and case_id == CASE_ID and actor == SOURCING
        return PublicSourcingPlan.create(tenant_id, command, created_at=NOW)

    async def confirm_public_plan(
        self, tenant_id, plan_id, expected_plan_hash, *, actor, expected_case_id=None
    ):
        self.calls.append("confirm")
        assert tenant_id == TENANT and plan_id == PLAN_ID and actor == BOSS
        assert expected_plan_hash == PLAN_HASH
        assert expected_case_id == CASE_ID
        return self.plan

    async def get_public_plan_run_view(
        self, tenant_id, case_id, plan_id, expected_plan_hash, *, actor
    ):
        self.calls.append("read_run")
        assert actor == BOSS
        return PublicSourcingRunView(
            tenant_id=tenant_id,
            case_id=case_id,
            need_id="need-a",
            case_version=7,
            active_plan=self.plan,
        )

    async def authorize_public_plan_run(
        self, tenant_id, case_id, plan_id, expected_plan_hash, *, actor
    ):
        self.calls.append("authorize_run")
        self.plan = replace(self.plan, status=PublicPlanStatus.RUNNING)
        return self.plan

    async def get_uncertain_search_execution(
        self, tenant_id, case_id, run_id, request_key, *, actor
    ):
        self.calls.append("read_execution")
        assert actor == BOSS
        return self.execution

    async def get_case_read_view(self, tenant_id, case_id, *, actor):
        self.calls.append("read_case")
        assert tenant_id == TENANT and case_id == CASE_ID
        return object()

    async def list_uncertain_execution_read_views(
        self, tenant_id, case_id, *, actor, limit=50
    ):
        self.calls.append("list_uncertain")
        assert tenant_id == TENANT and case_id == CASE_ID and limit == 50
        return (
            SourcingUncertainExecutionReadView(
                execution_id=self.execution.execution_id,
                run_id=RUN_ID,
                request_key=REQUEST_KEY,
                status="uncertain",
                created_at=NOW,
                reconciliation=None,
                can_current_user_reconcile=False,
            ),
        )

    async def record_confirmed_consumed_reconciliation(
        self, tenant_id, case_id, command, *, actor
    ):
        self.calls.append("record_reconciliation")
        return SourcingSearchReconciliation(
            reconciliation_id=command.reconciliation_id,
            tenant_id=tenant_id,
            execution_id=self.execution.execution_id,
            status=SourcingReconciliationStatus.CONFIRMED_CONSUMED,
            reason=command.reason,
            provider_usage_artifact_ref=command.provider_usage_artifact_ref,
            created_at=NOW,
            reconciled_by="boss-a",
            reconciled_at=NOW,
        )


class _Quota:
    def __init__(self, snapshot: SearchQuotaSnapshot | None = None) -> None:
        self.value = snapshot or SearchQuotaSnapshot(
            TENANT,
            "tavily",
            10,
            0,
            SearchCostStatus.FREE,
            1_000,
            10,
            False,
            NOW,
        )
        self.calls: list[str] = []
        self.reservation = SearchReservation(
            TENANT, RUN_ID, REQUEST_KEY, "uncertain", NOW, NOW
        )

    async def snapshot(self):
        self.calls.append("snapshot")
        return self.value

    async def get(self, run_id, request_key):
        self.calls.append("get")
        return self.reservation

    async def acknowledge_uncertain_as_consumed(self, run_id, request_key):
        self.calls.append("acknowledge")
        self.reservation = replace(self.reservation, status="consumed")


class _Engine:
    def __init__(self, run: WorkflowRun | None = None) -> None:
        self.run = run or _run()
        self.delivered: list[tuple[str, dict[str, Any]]] = []
        self.already_delivered = False
        self.delivery_error: Exception | None = None
        self.event_query_error: Exception | None = None
        self.event_queries: list[tuple[object, ...]] = []
        self.duplicate_active = False

    async def find_active_run(self, tenant_id, workflow_type, subject_ref):
        if self.duplicate_active:
            raise ValidationError("active workflow run is not unique")
        if (
            tenant_id == TENANT
            and workflow_type == "sourcing_case"
            and subject_ref == str(CASE_ID)
            and self.run.status is StepStatus.RUNNING
        ):
            return self.run.run_id
        return None

    async def get_run(self, tenant_id, run_id):
        return self.run if tenant_id == TENANT and run_id == self.run.run_id else None

    async def has_delivered_event(
        self,
        tenant_id,
        workflow_type,
        subject_ref,
        event_type,
        payload,
        *,
        workflow_version=None,
        required_context=None,
    ):
        if self.event_query_error is not None:
            raise self.event_query_error
        self.event_queries.append(
            (
                tenant_id,
                workflow_type,
                subject_ref,
                event_type,
                dict(payload),
                workflow_version,
                dict(required_context or {}),
            )
        )
        if not self.already_delivered:
            return False
        if (
            workflow_version is not None
            and self.run.workflow_version != workflow_version
        ):
            return False
        return required_context is None or all(
            self.run.context.get(key) == value
            for key, value in required_context.items()
        )

    async def deliver_event(self, tenant_id, run_id, event_type, payload):
        if self.delivery_error is not None:
            raise self.delivery_error
        self.delivered.append((event_type, dict(payload)))
        return True


def _application(
    *,
    sourcing: _Sourcing | None = None,
    quota: _Quota | None = None,
    engine: _Engine | None = None,
) -> tuple[SourcingCaseApplication, _Sourcing, _Quota, _Engine]:
    sourcing = sourcing or _Sourcing()
    quota = quota or _Quota()
    engine = engine or _Engine()
    return (
        SourcingCaseApplication(sourcing=sourcing, quota=quota, engine=engine),
        sourcing,
        quota,
        engine,
    )


@pytest.mark.asyncio
async def test_draft_and_confirmation_do_not_read_quota_or_wake_workflow() -> None:
    application, sourcing, quota, engine = _application()

    await application.create_plan(TENANT, CASE_ID, _plan_command(), actor=SOURCING)
    await application.confirm_plan(TENANT, CASE_ID, PLAN_ID, PLAN_HASH, actor=BOSS)

    assert sourcing.calls == ["save", "confirm"]
    assert quota.calls == []
    assert engine.delivered == []


@pytest.mark.asyncio
async def test_current_quota_and_uncertain_recovery_are_safe_application_projections() -> (
    None
):
    application, sourcing, quota, _ = _application()

    current = await application.get_current_quota_read_view(TENANT, CASE_ID, actor=BOSS)
    uncertain = await application.list_uncertain_execution_read_views(
        TENANT, CASE_ID, actor=BOSS
    )

    assert current is not None
    assert current.model_dump() == {
        "remaining": 10,
        "reservations": 0,
        "cost_status": "free",
        "paygo_enabled": False,
        "checked_at": NOW,
    }
    assert uncertain is not None
    assert uncertain[0].can_current_user_reconcile is True
    assert uncertain[0].model_dump(exclude={"request_key"}) == {
        "execution_id": "sse_execution-a",
        "run_id": "run_sourcing-a",
        "status": "uncertain",
        "created_at": NOW,
        "reconciliation": None,
        "can_current_user_reconcile": True,
    }
    assert sourcing.calls == ["read_case", "list_uncertain", "read_execution"]
    assert quota.calls == ["snapshot", "get"]


@pytest.mark.asyncio
async def test_run_checks_free_snapshot_then_authorizes_and_wakes_exact_run() -> None:
    application, sourcing, quota, engine = _application()

    result = await application.run(TENANT, CASE_ID, PLAN_ID, PLAN_HASH, actor=BOSS)

    assert result.status is PublicPlanStatus.RUNNING
    assert sourcing.calls == ["read_run", "authorize_run"]
    assert quota.calls == ["snapshot"]
    assert engine.delivered == [
        ("SourcingPlanConfirmed", {"plan_id": str(PLAN_ID), "plan_hash": PLAN_HASH})
    ]


@pytest.mark.asyncio
async def test_plan_wait_step_advances_only_on_allowlisted_exact_event() -> None:
    handler = AwaitPublicPlanStep()
    waiting = _run()
    waiting.context.update(
        {
            "need_id": "need-a",
            "need_snapshot_hash": "c" * 64,
        }
    )
    assert await handler.execute(waiting) == (
        "wait",
        None,
        {"sourcing_wait_status": "approval_required"},
    )

    confirmed = replace(
        waiting,
        context={
            **waiting.context,
            "event": {
                "event_type": "SourcingPlanConfirmed",
                "payload": {"plan_id": str(PLAN_ID), "plan_hash": PLAN_HASH},
            },
        },
    )
    assert await handler.execute(confirmed) == (
        "advance",
        "public_search",
        {"sourcing_plan_id": str(PLAN_ID), "sourcing_plan_hash": PLAN_HASH},
    )


@pytest.mark.parametrize(
    ("snapshot", "code"),
    [
        (None, "quota_status_unknown"),
        (
            SearchQuotaSnapshot(
                TenantId("tenant-other"),
                "tavily",
                10,
                0,
                SearchCostStatus.FREE,
                1_000,
                10,
                False,
                NOW,
            ),
            "quota_status_unknown",
        ),
        (
            SearchQuotaSnapshot(
                TENANT,
                "tavily",
                10,
                0,
                SearchCostStatus.UNKNOWN,
                None,
                None,
                None,
                NOW,
            ),
            "quota_status_unknown",
        ),
        (
            SearchQuotaSnapshot(
                TENANT,
                "tavily",
                10,
                0,
                SearchCostStatus.FREE,
                1_000,
                10,
                False,
                None,
            ),
            "quota_status_unknown",
        ),
        (
            SearchQuotaSnapshot(
                TENANT,
                "tavily",
                10,
                0,
                SearchCostStatus.PAID,
                1_000,
                10,
                True,
                NOW,
            ),
            "paid_usage_enabled",
        ),
        (
            SearchQuotaSnapshot(
                TENANT,
                "tavily",
                1,
                0,
                SearchCostStatus.FREE,
                1_000,
                10,
                False,
                NOW,
            ),
            "quota_exhausted",
        ),
    ],
)
@pytest.mark.asyncio
async def test_run_returns_distinct_free_quota_stop_codes_without_delivery(
    snapshot: SearchQuotaSnapshot | None, code: str
) -> None:
    quota = _Quota()
    quota.value = snapshot
    application, sourcing, _, engine = _application(quota=quota)

    with pytest.raises(SourcingPublicSearchBlockedError) as failure:
        await application.run(TENANT, CASE_ID, PLAN_ID, PLAN_HASH, actor=BOSS)

    assert failure.value.stop_code == code
    assert sourcing.calls == ["read_run"]
    assert engine.delivered == []


@pytest.mark.asyncio
async def test_run_sanitizes_quota_snapshot_failure_as_unknown_without_exception_chain() -> (
    None
):
    class _FailingQuota(_Quota):
        async def snapshot(self):
            self.calls.append("snapshot")
            raise RuntimeError("tavily_api_key=raw-secret")

    quota = _FailingQuota()
    application, sourcing, _, engine = _application(quota=quota)

    with pytest.raises(SourcingPublicSearchBlockedError) as failure:
        await application.run(TENANT, CASE_ID, PLAN_ID, PLAN_HASH, actor=BOSS)

    assert failure.value.stop_code == "quota_status_unknown"
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    assert "raw-secret" not in str(failure.value)
    assert sourcing.calls == ["read_run"]
    assert engine.delivered == []


@pytest.mark.asyncio
async def test_run_rejects_wrong_workflow_boundary_before_plan_transition() -> None:
    application, sourcing, _, engine = _application(
        engine=_Engine(_run("public_search"))
    )

    with pytest.raises(ValidationError, match="授权边界"):
        await application.run(TENANT, CASE_ID, PLAN_ID, PLAN_HASH, actor=BOSS)

    assert sourcing.calls == ["read_run"]
    assert engine.delivered == []


@pytest.mark.asyncio
async def test_running_replay_accepts_existing_exact_event_without_redelivery() -> None:
    sourcing = _Sourcing()
    sourcing.plan = _plan(PublicPlanStatus.RUNNING)
    engine = _Engine(_run("public_search"))
    engine.already_delivered = True
    application, _, _, _ = _application(sourcing=sourcing, engine=engine)

    result = await application.run(TENANT, CASE_ID, PLAN_ID, PLAN_HASH, actor=BOSS)

    assert result.status is PublicPlanStatus.RUNNING
    assert sourcing.calls == ["read_run"]
    assert engine.delivered == []
    assert engine.event_queries == [
        (
            TENANT,
            "sourcing_case",
            str(CASE_ID),
            "SourcingPlanConfirmed",
            {"plan_id": str(PLAN_ID), "plan_hash": PLAN_HASH},
            2,
            {"case_id": str(CASE_ID)},
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "run",
    [
        _run("verify_candidates"),
        replace(_run("handoff_costing"), status=StepStatus.COMPLETED),
    ],
)
async def test_running_replay_uses_durable_event_after_progress_or_terminal(
    run: WorkflowRun,
) -> None:
    sourcing = _Sourcing()
    sourcing.plan = _plan(PublicPlanStatus.RUNNING)
    engine = _Engine(run)
    engine.already_delivered = True
    application, _, _, _ = _application(sourcing=sourcing, engine=engine)

    result = await application.run(TENANT, CASE_ID, PLAN_ID, PLAN_HASH, actor=BOSS)

    assert result.status is PublicPlanStatus.RUNNING
    assert sourcing.calls == ["read_run"]
    assert engine.delivered == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "active_run",
    [
        replace(_run("public_search"), workflow_version=1),
        replace(_run("public_search"), context={"case_id": "src_case-other"}),
    ],
)
async def test_running_replay_rejects_incompatible_active_run_before_event_history(
    active_run: WorkflowRun,
) -> None:
    sourcing = _Sourcing()
    sourcing.plan = _plan(PublicPlanStatus.RUNNING)
    engine = _Engine(active_run)
    engine.already_delivered = True
    application, _, _, _ = _application(sourcing=sourcing, engine=engine)

    with pytest.raises(ValidationError, match="绑定无效"):
        await application.run(TENANT, CASE_ID, PLAN_ID, PLAN_HASH, actor=BOSS)

    assert engine.event_queries == []
    assert engine.delivered == []


@pytest.mark.asyncio
async def test_running_replay_rejects_duplicate_active_runs_before_event_history() -> (
    None
):
    sourcing = _Sourcing()
    sourcing.plan = _plan(PublicPlanStatus.RUNNING)
    engine = _Engine(_run("public_search"))
    engine.already_delivered = True
    engine.duplicate_active = True
    application, _, _, _ = _application(sourcing=sourcing, engine=engine)

    with pytest.raises(ValidationError, match="not unique"):
        await application.run(TENANT, CASE_ID, PLAN_ID, PLAN_HASH, actor=BOSS)

    assert engine.event_queries == []
    assert engine.delivered == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("history_run", "accepted"),
    [
        (replace(_run("handoff_costing"), status=StepStatus.COMPLETED), True),
        (
            replace(
                _run("handoff_costing"),
                workflow_version=1,
                status=StepStatus.COMPLETED,
            ),
            False,
        ),
        (
            replace(
                _run("handoff_costing"),
                status=StepStatus.COMPLETED,
                context={"case_id": "src_case-other"},
            ),
            False,
        ),
    ],
)
async def test_terminal_replay_requires_compatible_owning_run_history(
    history_run: WorkflowRun, accepted: bool
) -> None:
    sourcing = _Sourcing()
    sourcing.plan = _plan(PublicPlanStatus.RUNNING)
    engine = _Engine(history_run)
    engine.already_delivered = True
    application, _, _, _ = _application(sourcing=sourcing, engine=engine)

    if accepted:
        result = await application.run(TENANT, CASE_ID, PLAN_ID, PLAN_HASH, actor=BOSS)
        assert result.status is PublicPlanStatus.RUNNING
    else:
        with pytest.raises(ValidationError, match="活动 Workflow Run"):
            await application.run(TENANT, CASE_ID, PLAN_ID, PLAN_HASH, actor=BOSS)
    assert engine.delivered == []


@pytest.mark.asyncio
async def test_running_plan_without_event_evidence_fails_closed_on_progressed_run() -> (
    None
):
    sourcing = _Sourcing()
    sourcing.plan = _plan(PublicPlanStatus.RUNNING)
    engine = _Engine(_run("public_search"))
    application, _, _, _ = _application(sourcing=sourcing, engine=engine)

    with pytest.raises(ValidationError, match="授权边界"):
        await application.run(TENANT, CASE_ID, PLAN_ID, PLAN_HASH, actor=BOSS)

    assert engine.event_queries
    assert engine.delivered == []


@pytest.mark.asyncio
async def test_authorized_snapshot_race_rechecks_exact_event_and_running_domain_fact() -> (
    None
):
    class _RacingSourcing(_Sourcing):
        async def get_public_plan_run_view(
            self, tenant_id, case_id, plan_id, expected_plan_hash, *, actor
        ):
            view = await super().get_public_plan_run_view(
                tenant_id,
                case_id,
                plan_id,
                expected_plan_hash,
                actor=actor,
            )
            if self.calls.count("read_run") == 1:
                self.plan = _plan(PublicPlanStatus.RUNNING)
            return view

    sourcing = _RacingSourcing()
    engine = _Engine(_run("public_search"))
    engine.already_delivered = True
    application, _, _, _ = _application(sourcing=sourcing, engine=engine)

    result = await application.run(TENANT, CASE_ID, PLAN_ID, PLAN_HASH, actor=BOSS)

    assert result.status is PublicPlanStatus.RUNNING
    assert sourcing.calls == ["read_run", "read_run"]
    assert engine.event_queries
    assert engine.delivered == []


@pytest.mark.asyncio
async def test_running_plan_without_event_can_redeliver_only_at_exact_waiting_boundary() -> (
    None
):
    sourcing = _Sourcing()
    sourcing.plan = _plan(PublicPlanStatus.RUNNING)
    wrong_context = replace(_run(), context={"case_id": "src_case-other"})
    engine = _Engine(wrong_context)
    application, _, _, _ = _application(sourcing=sourcing, engine=engine)

    with pytest.raises(ValidationError, match="绑定无效"):
        await application.run(TENANT, CASE_ID, PLAN_ID, PLAN_HASH, actor=BOSS)
    assert engine.delivered == []

    engine.run = _run()
    recovered = await application.run(TENANT, CASE_ID, PLAN_ID, PLAN_HASH, actor=BOSS)
    assert recovered.status is PublicPlanStatus.RUNNING
    assert engine.delivered == [
        ("SourcingPlanConfirmed", {"plan_id": str(PLAN_ID), "plan_hash": PLAN_HASH})
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query_error", "expected_type", "retryable"),
    [
        (TransientError("token=raw-secret"), SourcingPlanDeliveryError, True),
        (RuntimeError("token=raw-secret"), ValidationError, False),
    ],
)
async def test_running_replay_sanitizes_event_evidence_query_failures(
    query_error: Exception, expected_type: type[Exception], retryable: bool
) -> None:
    sourcing = _Sourcing()
    sourcing.plan = _plan(PublicPlanStatus.RUNNING)
    engine = _Engine(_run("verify_candidates"))
    engine.event_query_error = query_error
    application, _, _, _ = _application(sourcing=sourcing, engine=engine)

    with pytest.raises(expected_type) as failure:
        await application.run(TENANT, CASE_ID, PLAN_ID, PLAN_HASH, actor=BOSS)

    assert getattr(failure.value, "is_retryable", False) is retryable
    assert failure.value.__cause__ is None
    assert failure.value.__context__ is None
    assert "raw-secret" not in str(failure.value)
    assert engine.delivered == []


@pytest.mark.asyncio
async def test_transient_delivery_failure_is_fixed_and_retryable() -> None:
    from shared.errors import TransientError

    engine = _Engine()
    engine.delivery_error = TransientError("provider secret leaked")
    application, sourcing, _, _ = _application(engine=engine)

    with pytest.raises(SourcingPlanDeliveryError) as failure:
        await application.run(TENANT, CASE_ID, PLAN_ID, PLAN_HASH, actor=BOSS)

    assert failure.value.is_retryable
    assert "secret" not in str(failure.value)
    assert sourcing.plan.status is PublicPlanStatus.RUNNING
    engine.delivery_error = None
    recovered = await application.run(TENANT, CASE_ID, PLAN_ID, PLAN_HASH, actor=BOSS)
    assert recovered.status is PublicPlanStatus.RUNNING
    assert engine.delivered == [
        ("SourcingPlanConfirmed", {"plan_id": str(PLAN_ID), "plan_hash": PLAN_HASH})
    ]


def _reconciliation_command(
    **changes: object,
) -> SourcingUncertainReconciliationCommand:
    values: dict[str, object] = {
        "reconciliation_id": "src_rec-a",
        "run_id": RUN_ID,
        "request_key": REQUEST_KEY,
        "resolution": "count_as_consumed",
        "reason": "已在提供商账户用量页人工核对",
        "provider_usage_artifact_ref": EVIDENCE_ID,
    }
    values.update(changes)
    return SourcingUncertainReconciliationCommand.model_validate(values)


def test_reconciliation_command_forbids_provider_payload_and_credentials() -> None:
    for forbidden in ("tenant_id", "actor", "provider_payload", "api_key"):
        with pytest.raises(PydanticValidationError):
            _reconciliation_command(**{forbidden: "secret"})
    with pytest.raises(PydanticValidationError):
        _reconciliation_command(resolution="count_as_free")


@pytest.mark.asyncio
async def test_reconciliation_records_fact_before_quota_ack_and_safe_retry_event() -> (
    None
):
    application, sourcing, quota, engine = _application(
        engine=_Engine(_run("public_search"))
    )
    order: list[str] = []
    original_record = sourcing.record_confirmed_consumed_reconciliation
    original_ack = quota.acknowledge_uncertain_as_consumed

    async def record(*args, **kwargs):
        order.append("record")
        return await original_record(*args, **kwargs)

    async def acknowledge(*args, **kwargs):
        order.append("ack")
        return await original_ack(*args, **kwargs)

    sourcing.record_confirmed_consumed_reconciliation = record  # type: ignore[method-assign]
    quota.acknowledge_uncertain_as_consumed = acknowledge  # type: ignore[method-assign]

    result = await application.reconcile_uncertain(
        TENANT, CASE_ID, _reconciliation_command(), actor=BOSS
    )

    assert result.status is SourcingReconciliationStatus.CONFIRMED_CONSUMED
    assert order == ["record", "ack"]
    assert quota.reservation.status == "consumed"
    assert engine.delivered == [
        (
            "SourcingSearchRetryRequested",
            {
                "reconciliation_id": "src_rec-a",
                "execution_id": "sse_execution-a",
            },
        )
    ]


@pytest.mark.asyncio
async def test_reconciliation_rejects_reserved_or_missing_quota_fact() -> None:
    for reservation in (
        None,
        replace(_Quota().reservation, status="reserved"),
        replace(_Quota().reservation, run_id=RunId("run-other")),
        replace(_Quota().reservation, request_key="c" * 64),
    ):
        quota = _Quota()
        quota.reservation = reservation  # type: ignore[assignment]
        application, sourcing, _, engine = _application(quota=quota)

        with pytest.raises(ValidationError, match="不确定"):
            await application.reconcile_uncertain(
                TENANT, CASE_ID, _reconciliation_command(), actor=BOSS
            )

        assert sourcing.calls == ["read_execution"]
        assert engine.delivered == []
