"""S3-15 CRM handoff HTTP adapter behavior tests."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest

from apps.api.dependencies import ConfiguredApiDependencies
from apps.api.main import ApiSettings, create_app
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.opportunities.errors import HandoffAlreadyAcceptedError
from domains.opportunities.permissions import OpportunityAction
from domains.opportunities.schemas import HandoffPacketView, HandoffQueueItemView
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId, HandoffId
from tests.provider_readiness_fakes import provider_readiness_dependencies
from tests.unit.test_crm_router import (
    _HEADERS,
    _TENANT,
    _Authorizer,
    _Client,
    _Employees,
    _EmployeeScope,
    _ManualRuntime,
    _Opportunities,
)

_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


def _queue_item(
    handoff_id: str,
    *,
    requested_at: datetime,
    wait_seconds: int,
) -> HandoffQueueItemView:
    return HandoffQueueItemView(
        handoff_id=handoff_id,
        opportunity_id=f"opp-{handoff_id}",
        trigger="quote_requested",
        account_name=f"Account {handoff_id}",
        country="US",
        why_valuable=f"Value {handoff_id}",
        customer_verbatim=f"Customer {handoff_id}",
        requested_at=requested_at,
        wait_seconds=wait_seconds,
        state="requested",
        assigned_to="emp-sales",
        suggested_next_step="Call customer",
        missing_information=["quantity"],
        evidence_links=[f"evidence://{handoff_id}"],
    )


def _packet(handoff_id: str = "hand-packet") -> HandoffPacketView:
    return HandoffPacketView(
        handoff_id=handoff_id,
        opportunity_id="opp-packet",
        trigger="specification_file_received",
        account_name="Acme Components",
        country="DE",
        how_we_found_them="Inbound reply",
        why_valuable="Confirmed annual demand",
        customer_verbatim="We need 20,000 pieces this quarter.",
        validated_need_summary="Quarterly hinge purchase",
        missing_information=["finish", "packaging"],
        conversation_summary="Customer sent a specification file.",
        already_sent=["catalogue"],
        commitments_made=["No commercial commitment"],
        suggested_next_step="Confirm coating",
        evidence_links=["evidence://message-1", "evidence://upload-1"],
        requested_at=_NOW - timedelta(seconds=321),
        wait_seconds=321,
        state="requested",
        assigned_to_name="Sales One",
    )


class _HandoffOpportunities(_Opportunities):
    def __init__(self, trace: list[str]) -> None:
        super().__init__(trace)
        self.queue_result = [
            _queue_item(
                "hand-service-first",
                requested_at=_NOW - timedelta(seconds=30),
                wait_seconds=30,
            ),
            _queue_item(
                "hand-service-second",
                requested_at=_NOW - timedelta(seconds=300),
                wait_seconds=300,
            ),
        ]
        self.packet_result = _packet()
        self.loss_result = {
            "price_too_high": {"quoted": 2},
            "need_not_real": {"contacted": 1},
        }

    async def list_pending_handoffs(self, tenant_id, actor, *, limit=50):
        self._record("handoff_list", tenant_id, actor, limit=limit)
        return list(self.queue_result)

    async def get_handoff_packet(self, tenant_id, handoff_id, *, actor):
        self._record("handoff_packet", tenant_id, handoff_id, actor=actor)
        return self.packet_result

    async def accept_handoff(
        self, tenant_id, handoff_id, accepted_by, *, actor
    ) -> None:
        self._record(
            "handoff_accept",
            tenant_id,
            handoff_id,
            accepted_by,
            actor=actor,
        )

    async def loss_reason_breakdown(self, tenant_id, *, actor, since_days=30):
        self._record(
            "loss_reason_breakdown",
            tenant_id,
            actor=actor,
            since_days=since_days,
        )
        return self.loss_result


def _app(
    *,
    role: str = "sales",
    deny_first_gate: bool = False,
) -> tuple[object, _HandoffOpportunities, _Authorizer]:
    trace: list[str] = []
    employees = _Employees(trace, role=role)
    opportunities = _HandoffOpportunities(trace)
    opportunity_authorizer = _Authorizer(deny=deny_first_gate)
    manual_runtime = _ManualRuntime()
    dependencies = ConfiguredApiDependencies(
        opportunities=opportunities,
        outreach=manual_runtime,
        sending_identities=manual_runtime,
        tool_gateway=manual_runtime,
        delivery_materials=manual_runtime,
        unsubscribe_links=manual_runtime,
        unsubscribe_service=manual_runtime,
        employees=_EmployeeScope(employees, trace),
        opportunity_authorizer=opportunity_authorizer,
        employee_authorizer=_Authorizer(),
        workflow_engine=object(),
        outbox_deliverer=object(),
        notification_router=object(),
        notification_dedup_store=object(),
        employee_lookup_actor=EmployeeActor(
            actor_id="system:api-identity",
            scope=EmployeeScope.SYSTEM,
            role="system",
        ),
        outreach_authorizer=object(),
        sending_identity_authorizer=object(),
        campaign_scope_resolver=object(),
        in_app_notifications=object(),
        **provider_readiness_dependencies(_TENANT),
    )
    app = create_app(
        settings=ApiSettings(
            tenant_id=str(_TENANT),
            dev_mode=True,
            retry_after_seconds=5,
        ),
        dependencies=dependencies,
    )
    return app, opportunities, opportunity_authorizer


def test_handoff_queue_preserves_service_order_and_exact_identity_scope() -> None:
    """A router-side sort or wrong actor/action/limit breaks queue fairness or ABAC."""
    app, opportunities, authorizer = _app()

    response = _Client(app).request(
        "GET", "/crm/handoffs?limit=7", headers=_HEADERS
    )

    assert response.status_code == 200
    assert [item["handoff_id"] for item in response.json()] == [
        "hand-service-first",
        "hand-service-second",
    ]
    assert len(opportunities.calls) == 1
    name, args, kwargs = opportunities.calls[0]
    assert name == "handoff_list"
    assert args[0] == _TENANT
    actor = args[1]
    assert actor.actor_id == "emp-sales"
    assert kwargs == {"limit": 7}
    assert authorizer.calls == [
        (actor, OpportunityAction.HANDOFF_QUEUE_READ, actor.scope, _TENANT)
    ]


def test_handoff_packet_returns_complete_public_view_and_typed_id() -> None:
    """Reconstructing the packet in HTTP would drop context fields needed for handoff."""
    app, opportunities, authorizer = _app()

    response = _Client(app).request(
        "GET", "/crm/handoffs/hand-packet", headers=_HEADERS
    )

    assert response.status_code == 200
    assert response.json() == {
        "handoff_id": "hand-packet",
        "opportunity_id": "opp-packet",
        "trigger": "specification_file_received",
        "account_name": "Acme Components",
        "country": "DE",
        "why_valuable": "Confirmed annual demand",
        "customer_verbatim": "We need 20,000 pieces this quarter.",
        "requested_at": "2026-08-09T11:54:39Z",
        "state": "requested",
        "how_we_found_them": "Inbound reply",
        "validated_need_summary": "Quarterly hinge purchase",
        "missing_information": ["finish", "packaging"],
        "conversation_summary": "Customer sent a specification file.",
        "already_sent": ["catalogue"],
        "commitments_made": ["No commercial commitment"],
        "suggested_next_step": "Confirm coating",
        "evidence_links": ["evidence://message-1", "evidence://upload-1"],
        "wait_seconds": 321,
        "assigned_to_name": "Sales One",
    }
    name, args, kwargs = opportunities.calls[0]
    assert name == "handoff_packet"
    assert args == (_TENANT, HandoffId("hand-packet"))
    actor = kwargs["actor"]
    assert authorizer.calls == [
        (actor, OpportunityAction.HANDOFF_READ, actor.scope, _TENANT)
    ]


def test_accept_is_bodyless_204_and_accepted_by_only_comes_from_identity() -> None:
    """Trusting an accepted-by header would let one employee forge another's audit trail."""
    app, opportunities, authorizer = _app()
    headers = {
        **_HEADERS,
        "X-Accepted-By": "emp-forged",
        "X-Role": "boss",
        "X-Scope": "tenant",
    }

    response = _Client(app).request(
        "POST", "/crm/handoffs/hand-accept/accept", headers=headers
    )

    assert response.status_code == 204
    assert response.content == b""
    name, args, kwargs = opportunities.calls[0]
    assert name == "handoff_accept"
    assert args == (
        _TENANT,
        HandoffId("hand-accept"),
        EmployeeId("emp-sales"),
    )
    actor = kwargs["actor"]
    assert actor.actor_id == "emp-sales"
    assert authorizer.calls == [
        (actor, OpportunityAction.HANDOFF_ACCEPT, actor.scope, _TENANT)
    ]


def test_already_accepted_is_fixed_safe_409_without_identifier_or_error_leak(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Leaking a conflict exception would expose customer text and rejected IDs."""
    caplog.set_level(logging.DEBUG)
    app, opportunities, _ = _app()
    secret_id = "hand-private-409"
    secret_text = "customer verbatim must stay private"
    opportunities.raise_on["handoff_accept"] = HandoffAlreadyAcceptedError(
        f"{secret_id}: {secret_text}"
    )

    response = _Client(app).request(
        "POST",
        f"/crm/handoffs/{secret_id}/accept",
        headers={**_HEADERS, "X-Actor": "actor-private"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "code": "handoff_already_accepted",
        "message": "接管已被接受",
    }
    assert len(opportunities.calls) == 1
    assert secret_id not in response.text
    assert secret_text not in response.text
    assert "actor-private" not in response.text
    app_logs = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("apps.api")
    )
    assert secret_id not in app_logs
    assert secret_text not in app_logs
    assert "actor-private" not in app_logs


def test_loss_reason_analytics_is_boss_only_and_returns_domain_mapping_unchanged() -> None:
    """Router aggregation or a broad role gate can corrupt or expose tenant analytics."""
    app, opportunities, authorizer = _app(role="boss")

    response = _Client(app).request(
        "GET", "/crm/analytics/loss-reasons?since_days=9", headers=_HEADERS
    )

    assert response.status_code == 200
    assert response.json() == opportunities.loss_result
    name, args, kwargs = opportunities.calls[0]
    assert name == "loss_reason_breakdown"
    assert args == (_TENANT,)
    assert kwargs["since_days"] == 9
    actor = kwargs["actor"]
    assert actor.role == "boss"
    assert authorizer.calls == [
        (actor, OpportunityAction.LOSS_REASON_READ, actor.scope, _TENANT)
    ]


def test_queue_and_analytics_pass_declared_default_windows() -> None:
    """Changing HTTP defaults must not silently change queue depth or analytics horizon."""
    app, opportunities, _ = _app(role="boss")
    client = _Client(app)

    queue = client.request("GET", "/crm/handoffs", headers=_HEADERS)
    analytics = client.request(
        "GET", "/crm/analytics/loss-reasons", headers=_HEADERS
    )

    assert queue.status_code == 200
    assert analytics.status_code == 200
    assert opportunities.calls[0][0] == "handoff_list"
    assert opportunities.calls[0][2] == {"limit": 50}
    assert opportunities.calls[1][0] == "loss_reason_breakdown"
    assert opportunities.calls[1][2]["since_days"] == 30


@pytest.mark.parametrize("role", ["sales", "manager"])
def test_non_boss_roles_fail_before_loss_aggregate_service_call(role: str) -> None:
    """Calling the aggregate before the boss role gate leaks tenant-wide counts."""
    app, opportunities, authorizer = _app(role=role)

    response = _Client(app).request(
        "GET", "/crm/analytics/loss-reasons", headers=_HEADERS
    )

    assert response.status_code == 403
    assert opportunities.calls == []
    assert authorizer.calls == []


@pytest.mark.parametrize("role", ["sales", "manager", "boss"])
def test_all_crm_roles_can_reach_handoff_queue_gate(role: str) -> None:
    """Removing an approved CRM role from the gate would block legitimate handoff work."""
    app, opportunities, authorizer = _app(role=role)

    response = _Client(app).request("GET", "/crm/handoffs", headers=_HEADERS)

    assert response.status_code == 200
    assert [call[0] for call in opportunities.calls] == ["handoff_list"]
    assert authorizer.calls[0][1] is OpportunityAction.HANDOFF_QUEUE_READ


@pytest.mark.parametrize("role", ["sales", "manager", "boss"])
def test_all_crm_roles_can_reach_packet_and_accept_gates(role: str) -> None:
    """Packet and accept must retain the same explicit CRM role matrix as the queue."""
    app, opportunities, authorizer = _app(role=role)
    client = _Client(app)

    packet = client.request("GET", "/crm/handoffs/hand-role", headers=_HEADERS)
    accepted = client.request(
        "POST", "/crm/handoffs/hand-role/accept", headers=_HEADERS
    )

    assert packet.status_code == 200
    assert accepted.status_code == 204
    assert [call[0] for call in opportunities.calls] == [
        "handoff_packet",
        "handoff_accept",
    ]
    assert [call[1] for call in authorizer.calls] == [
        OpportunityAction.HANDOFF_READ,
        OpportunityAction.HANDOFF_ACCEPT,
    ]


def test_non_crm_role_fails_before_handoff_first_gate_or_service() -> None:
    """A role sourced outside the CRM allowlist must fail closed before domain access."""
    app, opportunities, authorizer = _app(role="sourcing")

    response = _Client(app).request("GET", "/crm/handoffs", headers=_HEADERS)

    assert response.status_code == 403
    assert opportunities.calls == []
    assert authorizer.calls == []


@pytest.mark.parametrize(
    ("method", "path", "role"),
    [
        ("GET", "/crm/handoffs", "sales"),
        ("GET", "/crm/handoffs/hand-denied", "sales"),
        ("POST", "/crm/handoffs/hand-denied/accept", "sales"),
        ("GET", "/crm/analytics/loss-reasons", "boss"),
    ],
)
def test_first_gate_denial_stops_before_every_handoff_service_call(
    method: str, path: str, role: str
) -> None:
    """A denied first gate must not be followed by a domain read or write."""
    app, opportunities, authorizer = _app(role=role, deny_first_gate=True)

    response = _Client(app).request(method, path, headers=_HEADERS)

    assert response.status_code == 403
    assert response.json() == {"code": "forbidden", "message": "没有权限"}
    assert opportunities.calls == []
    assert len(authorizer.calls) == 1


@pytest.mark.parametrize(
    "path",
    [
        "/crm/handoffs?limit=0",
        "/crm/handoffs?limit=-1",
        "/crm/handoffs?limit=not-an-int",
        "/crm/analytics/loss-reasons?since_days=0",
        "/crm/analytics/loss-reasons?since_days=-1",
        "/crm/analytics/loss-reasons?since_days=not-an-int",
    ],
)
def test_invalid_positive_query_values_are_safe_400_without_service_call(
    path: str,
) -> None:
    """Invalid pagination windows must never reach queue or aggregate repositories."""
    role = "boss" if "analytics" in path else "sales"
    app, opportunities, _ = _app(role=role)

    response = _Client(app).request("GET", path, headers=_HEADERS)

    assert response.status_code == 400
    assert response.json() == {
        "code": "validation_error",
        "message": "请求参数无效",
    }
    assert opportunities.calls == []


def test_domain_packet_denial_uses_safe_error_and_stops_after_one_call() -> None:
    """Second-gate denial must propagate to the safe mapper without a fallback read."""
    app, opportunities, _ = _app()
    opportunities.raise_on["handoff_packet"] = PermissionDenied(
        "hand-secret customer-secret"
    )

    response = _Client(app).request(
        "GET", "/crm/handoffs/hand-secret", headers=_HEADERS
    )

    assert response.status_code == 403
    assert response.json() == {"code": "forbidden", "message": "没有权限"}
    assert len(opportunities.calls) == 1
    assert "hand-secret" not in response.text
    assert "customer-secret" not in response.text
