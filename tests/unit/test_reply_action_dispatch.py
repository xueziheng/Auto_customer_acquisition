"""回复分类的非基础动作必须经窄端口确定性分派。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from domains.outreach.service import OutreachService
from shared.schemas.identifiers import RunId, TenantId, new_id
from workflows.engine.runner import StepStatus, WorkflowRun

MESSAGE = "msg_01K3H0T8NBWM3KGT9XQ06YRC5V"
OUTBOUND = "out_01K3H0T8NBWM3KGT9XQ06YRC5V"
ENROLLMENT = "enr_01K3H0T8NBWM3KGT9XQ06YRC5V"
ACCOUNT = "acc_01K3H0T8NBWM3KGT9XQ06YRC5V"
CONTACT = "cp_01K3H0T8NBWM3KGT9XQ06YRC5V"


class _Outreach:
    async def stop_enrollment(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    async def add_suppression(self, *args: object, **kwargs: object) -> None:
        del args, kwargs


class _RecordingOutreach(_Outreach):
    def __init__(self) -> None:
        self.suppressions: list[object] = []
        self.stops: list[object] = []

    async def stop_enrollment(self, *args: object, **kwargs: object) -> None:
        del kwargs
        self.stops.append(args[1])

    async def add_suppression(self, *args: object, **kwargs: object) -> None:
        del kwargs
        self.suppressions.append(args[1])


class _Classifier:
    model = "reply-action-test-v1"

    async def classify(self, **kwargs: object) -> object:
        del kwargs
        return object()


class _Reader:
    async def load(self, *args: object) -> None:
        del args


class _Guard:
    def check(self, **kwargs: object) -> None:
        del kwargs


class _Conversations:
    async def record_classification(self, *args: object, **kwargs: object) -> tuple[str, ...]:
        del args, kwargs
        return ()


class _Actions:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object, str]] = []

    async def _record(self, name: str, context: object, key: str) -> None:
        self.calls.append((name, context, key))

    async def route_bounce(self, tenant_id, context, idempotency_key) -> None:
        del tenant_id
        await self._record("route_bounce", context, idempotency_key)

    async def record_complaint(self, tenant_id, context, idempotency_key) -> None:
        del tenant_id
        await self._record("record_complaint", context, idempotency_key)

    async def request_handoff(self, tenant_id, context, idempotency_key) -> None:
        del tenant_id
        await self._record("handoff", context, idempotency_key)

    async def start_qualification(self, tenant_id, context, idempotency_key) -> None:
        del tenant_id
        await self._record("start_qualification", context, idempotency_key)

    async def extract_need_fields(self, tenant_id, context, idempotency_key) -> None:
        del tenant_id
        await self._record("extract_need_fields", context, idempotency_key)

    async def mark_future_restart(self, tenant_id, context, idempotency_key) -> None:
        del tenant_id
        await self._record("mark_future_restart", context, idempotency_key)

    async def create_follow_up(self, tenant_id, context, idempotency_key) -> None:
        del tenant_id
        await self._record("create_follow_up", context, idempotency_key)

    async def intake_new_contact(self, tenant_id, context, idempotency_key) -> None:
        del tenant_id
        await self._record("intake_new_contact", context, idempotency_key)


def test_reply_production_composition_rejects_missing_action_ports() -> None:
    """启用回复流程却没有完整动作实现必须在装配期失败，不能等客户回复后才失败。"""
    from apps.scheduler_worker.runtime import ReplyQualificationComposition
    from shared.errors import ValidationError

    with pytest.raises(ValidationError, match="动作依赖未完整配置"):
        ReplyQualificationComposition(
            classifier=_Classifier(),  # type: ignore[arg-type]
            content_reader=_Reader(),  # type: ignore[arg-type]
            input_guard=_Guard(),  # type: ignore[arg-type]
            conversations=_Conversations(),  # type: ignore[arg-type]
            outreach=cast(OutreachService, _Outreach()),
            action_ports=None,  # type: ignore[arg-type]
        )


def _run(
    tenant_id: TenantId,
    action: str,
    *,
    category: str = "clear_interest",
    suppress_scope: str | None = None,
) -> WorkflowRun:
    context = {
        "message_id": MESSAGE,
        "outbound_message_id": OUTBOUND,
        "enrollment_id": ENROLLMENT,
        "account_id": ACCOUNT,
        "contact_point_id": CONTACT,
        "category": category,
        "actions": [action],
        "classification_occurred_at": datetime(2026, 8, 21, tzinfo=UTC).isoformat(),
    }
    if suppress_scope is not None:
        context["suppress_scope"] = suppress_scope
    return WorkflowRun(
        run_id=RunId(new_id("run")),
        tenant_id=tenant_id,
        workflow_type="reply_qualification",
        workflow_version=1,
        subject_ref=MESSAGE,
        current_step="apply_actions",
        status=StepStatus.RUNNING,
        created_at=datetime(2026, 8, 21, tzinfo=UTC),
        context=context,
    )


@pytest.mark.parametrize(
    "action,method",
    (
        ("route_bounce", "route_bounce"),
        ("record_complaint", "record_complaint"),
        ("handoff", "handoff"),
        ("start_qualification", "start_qualification"),
        ("extract_need_fields", "extract_need_fields"),
        ("mark_future_restart", "mark_future_restart"),
        ("create_follow_up", "create_follow_up"),
        ("intake_new_contact", "intake_new_contact"),
    ),
)
async def test_reply_action_dispatch_uses_metadata_only_context(
    action: str, method: str
) -> None:
    from workflows.reply_qualification.steps import ApplyActionsStep

    tenant = TenantId(new_id("tn"))
    ports = _Actions()
    step = ApplyActionsStep(
        cast(OutreachService, _Outreach()),
        tenant,
        lambda: datetime(2026, 8, 21, tzinfo=UTC),
        ports,
    )

    result = await step.execute(_run(tenant, action))

    assert result == ("complete", None, {})
    assert len(ports.calls) == 1
    called_method, context, idempotency_key = ports.calls[0]
    assert called_method == method
    assert vars(context) == {
        "message_id": MESSAGE,
        "outbound_message_id": OUTBOUND,
        "enrollment_id": ENROLLMENT,
        "account_id": ACCOUNT,
        "contact_point_id": CONTACT,
    }
    assert idempotency_key == f"reply:{action}:{MESSAGE}"


async def test_second_specification_reply_replays_stop_update_then_handoff() -> None:
    """第二条消息在字段更新后崩溃，整步重放仍完成 stop/update/handoff。"""
    from workflows.reply_qualification.steps import ApplyActionsStep

    class CrashOnceActions(_Actions):
        def __init__(self) -> None:
            super().__init__()
            self.extract_attempts = 0

        async def extract_need_fields(
            self, tenant_id, context, idempotency_key
        ) -> None:
            del tenant_id
            self.extract_attempts += 1
            await self._record("extract_need_fields", context, idempotency_key)
            if self.extract_attempts == 1:
                raise RuntimeError("simulated second-message crash")

    tenant = TenantId(new_id("tn"))
    outreach = _RecordingOutreach()
    actions = CrashOnceActions()
    step = ApplyActionsStep(
        cast(OutreachService, outreach),
        tenant,
        lambda: datetime(2026, 8, 21, tzinfo=UTC),
        actions,
    )
    run = _run(tenant, "stop_sequence", category="provides_specification")
    run.context["actions"] = [
        "stop_sequence",
        "extract_need_fields",
        "handoff",
    ]

    with pytest.raises(RuntimeError, match="second-message crash"):
        await step.execute(run)

    result = await step.execute(run)

    assert result == ("complete", None, {})
    assert len(outreach.stops) == 2
    assert [call[0] for call in actions.calls] == [
        "extract_need_fields",
        "extract_need_fields",
        "handoff",
    ]
    assert actions.calls[-1][2] == f"reply:handoff:{MESSAGE}"


async def test_suppression_retry_uses_stable_run_occurrence() -> None:
    """重试同一 workflow run 必须生成相同 suppression payload。"""
    from workflows.reply_qualification.steps import ApplyActionsStep

    tenant = TenantId(new_id("tn"))
    outreach = _RecordingOutreach()
    wall_clock = datetime(2026, 8, 22, tzinfo=UTC)
    step = ApplyActionsStep(
        cast(OutreachService, outreach),
        tenant,
        lambda: wall_clock,
        _Actions(),
    )
    run = _run(
        tenant,
        "suppress",
        category="unsubscribe",
        suppress_scope="contact",
    )

    await step.execute(run)
    wall_clock += timedelta(days=1)
    await step.execute(run)

    first, second = outreach.suppressions
    assert first == second
    assert first.occurred_at == datetime.fromisoformat(
        run.context["classification_occurred_at"]
    )


async def test_account_unsubscribe_uses_account_target() -> None:
    from workflows.reply_qualification.steps import ApplyActionsStep

    tenant = TenantId(new_id("tn"))
    outreach = _RecordingOutreach()
    step = ApplyActionsStep(
        cast(OutreachService, outreach),
        tenant,
        lambda: datetime(2026, 8, 22, tzinfo=UTC),
        _Actions(),
    )

    await step.execute(
        _run(
            tenant,
            "suppress",
            category="unsubscribe",
            suppress_scope="account",
        )
    )

    request = outreach.suppressions[0]
    assert request.target.account_id == ACCOUNT
    assert request.target.contact_point_id is None


async def test_contact_unsubscribe_uses_contact_target() -> None:
    from workflows.reply_qualification.steps import ApplyActionsStep

    tenant = TenantId(new_id("tn"))
    outreach = _RecordingOutreach()
    step = ApplyActionsStep(
        cast(OutreachService, outreach),
        tenant,
        lambda: datetime(2026, 8, 22, tzinfo=UTC),
        _Actions(),
    )

    await step.execute(
        _run(
            tenant,
            "suppress",
            category="unsubscribe",
            suppress_scope="contact",
        )
    )

    request = outreach.suppressions[0]
    assert request.target.contact_point_id == CONTACT
    assert request.target.account_id is None
