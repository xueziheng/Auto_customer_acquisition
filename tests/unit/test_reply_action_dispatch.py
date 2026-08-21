"""回复分类的非基础动作必须经窄端口确定性分派。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

import pytest

from domains.outreach.service import OutreachService
from shared.schemas.identifiers import RunId, TenantId, new_id
from workflows.engine.runner import StepStatus, WorkflowRun


class _Outreach:
    async def stop_enrollment(self, *args: object, **kwargs: object) -> None:
        del args, kwargs

    async def add_suppression(self, *args: object, **kwargs: object) -> None:
        del args, kwargs


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


def _run(tenant_id: TenantId, action: str) -> WorkflowRun:
    return WorkflowRun(
        run_id=RunId(new_id("run")),
        tenant_id=tenant_id,
        workflow_type="reply_qualification",
        workflow_version=1,
        subject_ref="msg_reply_action_1",
        current_step="apply_actions",
        status=StepStatus.RUNNING,
        created_at=datetime(2026, 8, 21, tzinfo=UTC),
        context={
            "message_id": "msg_reply_action_1",
            "outbound_message_id": "out_reply_action_1",
            "enrollment_id": "enr_reply_action_1",
            "account_id": "acc_reply_action_1",
            "contact_point_id": "cp_reply_action_1",
            "category": "clear_interest",
            "actions": [action],
        },
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
        "message_id": "msg_reply_action_1",
        "outbound_message_id": "out_reply_action_1",
        "enrollment_id": "enr_reply_action_1",
        "account_id": "acc_reply_action_1",
        "contact_point_id": "cp_reply_action_1",
    }
    assert idempotency_key == f"reply:{action}:msg_reply_action_1"
