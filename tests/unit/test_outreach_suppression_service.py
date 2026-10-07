"""全局抑制的权限、幂等、跨 Campaign 停止与 fail-closed 读取。"""

from __future__ import annotations

import copy
import importlib
from dataclasses import replace
from datetime import timedelta

import pytest

from domains.outreach.errors import IdempotencyConflictError
from domains.outreach.permissions import (
    Actor,
    OutreachScope,
    Phase1OutreachAuthorizer,
    ScopeLevel,
)
from domains.outreach.schemas import SuppressionRequest, SuppressionTarget
from shared.errors import PermissionDenied, TransientError, ValidationError
from shared.events.catalog import SuppressionAdded
from shared.schemas.identifiers import (
    CampaignId,
    ContactPointId,
    EnrollmentId,
    IdempotencyKey,
    ProspectAccountId,
    SendingIdentityId,
    SuppressionId,
    TenantId,
    new_id,
)
from tests.unit.test_outreach_enrollment_service import NOW, _build

_models = importlib.import_module("domains.outreach.models")
Enrollment = _models.Enrollment
EnrollmentState = _models.EnrollmentState
EnrollmentStopReason = _models.EnrollmentStopReason
SuppressionEntry = _models.SuppressionEntry
SuppressionReason = _models.SuppressionReason


def _suppression_request(
    target: SuppressionTarget,
    *,
    reason=SuppressionReason.UNSUBSCRIBE,
    key: str = "suppression-key-1",
    source_ref: str = "reply_event_ref_1",
    occurred_at=NOW,
) -> SuppressionRequest:
    return SuppressionRequest(
        target,
        reason,
        occurred_at,
        source_ref,
        IdempotencyKey(key),
    )


def _system(tenant: TenantId, target: SuppressionTarget) -> Actor:
    return Actor(
        "system:suppression",
        OutreachScope(
            level=ScopeLevel.SYSTEM,
            allowed_suppression_targets=frozenset({target.canonical_id}),
        ),
        "system",
    )


def _manager(target: SuppressionTarget) -> Actor:
    return Actor(
        "manager:suppression",
        OutreachScope(
            level=ScopeLevel.MANAGER,
            allowed_account_ids=frozenset({ProspectAccountId(new_id("acc"))}),
            allowed_suppression_targets=frozenset({target.canonical_id}),
        ),
        "manager",
    )


def _sales() -> Actor:
    return Actor(
        "sales:suppression",
        OutreachScope(
            level=ScopeLevel.SELF,
            allowed_enrollment_ids=frozenset({EnrollmentId(new_id("enr"))}),
        ),
        "sales",
    )


def _active_enrollment(
    harness,
    *,
    campaign_id: CampaignId | None = None,
    account_id: ProspectAccountId | None = None,
    contact_point_id: ContactPointId | None = None,
) -> Enrollment:
    return Enrollment(
        tenant_id=harness.tenant,
        enrollment_id=EnrollmentId(new_id("enr")),
        campaign_id=campaign_id or harness.campaign_id,
        campaign_version=1,
        account_id=account_id or harness.account,
        contact_point_id=contact_point_id or harness.contact,
        sending_identity_id=SendingIdentityId(new_id("sid")),
        state=EnrollmentState.ENROLLED,
        current_step=0,
        next_send_at=NOW,
        enrolled_at=NOW,
        stopped_at=None,
        stop_reason=None,
        idempotency_key=IdempotencyKey(str(new_id("idem"))),
    )


@pytest.mark.parametrize("method", ["add", "get", "list"])
async def test_suppression_methods_preauthorize_before_clock_or_uow(method: str) -> None:
    harness = _build(denied=True)
    target = SuppressionTarget(contact_point_id=harness.contact)
    calls = {
        "add": lambda: harness.service.add_suppression(
            harness.tenant,
            _suppression_request(target),
            actor=harness.boss,
        ),
        "get": lambda: harness.service.is_suppressed(
            harness.tenant, target, actor=harness.boss
        ),
        "list": lambda: harness.service.list_suppressions(
            harness.tenant,
            harness.boss.scope,
            limit=10,
            actor=harness.boss,
        ),
    }
    with pytest.raises(PermissionDenied):
        await calls[method]()
    assert harness.trace.calls[0] == ("preauthorize", {
        "add": "suppression:add",
        "get": "suppression:read",
        "list": "suppression:list",
    }[method])
    assert harness.trace.calls[-1] == (
        "audit",
        {
            "add": "suppression:add",
            "get": "suppression:read",
            "list": "suppression:list",
        }[method],
        "deny:authorization",
    )
    assert not any(call[0] == "uow_enter" for call in harness.trace.calls)


@pytest.mark.parametrize(
    "reason",
    [
        SuppressionReason.UNSUBSCRIBE,
        SuppressionReason.COMPLAINT,
        SuppressionReason.HARD_BOUNCE,
    ],
)
async def test_exact_target_system_may_add_only_automatic_reason(reason) -> None:
    harness = _build()
    target = SuppressionTarget(contact_point_id=harness.contact)
    harness.service._authorizer = Phase1OutreachAuthorizer(harness.tenant)
    result = await harness.service.add_suppression(
        harness.tenant,
        _suppression_request(target, reason=reason, key=f"auto-{reason.value}"),
        actor=_system(harness.tenant, target),
    )
    assert result.created is True


@pytest.mark.parametrize(
    "reason",
    [
        SuppressionReason.MANUAL_BLOCK,
        SuppressionReason.COMPETITOR,
        SuppressionReason.EXISTING_CUSTOMER_CONFLICT,
    ],
)
async def test_system_cannot_add_human_reason(reason) -> None:
    harness = _build()
    target = SuppressionTarget(account_id=harness.account)
    harness.service._authorizer = Phase1OutreachAuthorizer(harness.tenant)
    with pytest.raises(PermissionDenied):
        await harness.service.add_suppression(
            harness.tenant,
            _suppression_request(target, reason=reason),
            actor=_system(harness.tenant, target),
        )
    assert harness.store.suppressions == []


@pytest.mark.parametrize("reason", list(SuppressionReason))
async def test_boss_may_add_every_typed_reason(reason) -> None:
    harness = _build()
    harness.service._authorizer = Phase1OutreachAuthorizer(harness.tenant)
    target = SuppressionTarget(account_id=harness.account)
    result = await harness.service.add_suppression(
        harness.tenant,
        _suppression_request(target, reason=reason, key=f"boss-{reason.value}"),
        actor=harness.boss,
    )
    assert result.created is True


@pytest.mark.parametrize("actor_factory", [_manager, lambda _target: _sales()])
async def test_manager_and_sales_cannot_add_suppression(actor_factory) -> None:
    harness = _build()
    harness.service._authorizer = Phase1OutreachAuthorizer(harness.tenant)
    target = SuppressionTarget(account_id=harness.account)
    with pytest.raises(PermissionDenied):
        await harness.service.add_suppression(
            harness.tenant,
            _suppression_request(target),
            actor=actor_factory(target),
        )


async def test_wrong_system_target_is_denied_before_clock_and_uow() -> None:
    harness = _build()
    target = SuppressionTarget(contact_point_id=harness.contact)
    other = SuppressionTarget(contact_point_id=ContactPointId(new_id("cp")))
    harness.service._authorizer = Phase1OutreachAuthorizer(harness.tenant)
    harness.service._now = lambda: (_ for _ in ()).throw(AssertionError("clock called"))
    with pytest.raises(PermissionDenied):
        await harness.service.add_suppression(
            harness.tenant,
            _suppression_request(target),
            actor=_system(harness.tenant, other),
        )
    assert not any(call[0] == "uow_enter" for call in harness.trace.calls)


async def test_contact_suppression_stops_only_matching_active_rows_and_writes_once() -> None:
    harness = _build()
    matching_one = _active_enrollment(harness)
    matching_two = _active_enrollment(
        harness, campaign_id=CampaignId(new_id("cmp"))
    )
    other_contact = _active_enrollment(
        harness, contact_point_id=ContactPointId(new_id("cp"))
    )
    terminal = copy.deepcopy(matching_one)
    terminal.enrollment_id = EnrollmentId(new_id("enr"))
    terminal.transition_to(
        EnrollmentState.COMPLETED, at=NOW, reason=None
    )
    for row in (matching_two, other_contact, terminal, matching_one):
        harness.store.enrollments[row.enrollment_id] = row

    target = SuppressionTarget(contact_point_id=harness.contact)
    result = await harness.service.add_suppression(
        harness.tenant, _suppression_request(target), actor=harness.boss
    )

    assert result.created is True and result.stopped_count == 2
    assert [
        row.state for row in (matching_one, matching_two)
    ] == [EnrollmentState.ENROLLED, EnrollmentState.ENROLLED]
    assert all(
        harness.store.enrollments[row.enrollment_id].state
        is EnrollmentState.STOPPED_SUPPRESSED
        for row in (matching_one, matching_two)
    )
    assert harness.store.enrollments[other_contact.enrollment_id].state is EnrollmentState.ENROLLED
    assert harness.store.enrollments[terminal.enrollment_id].state is EnrollmentState.COMPLETED
    assert len(harness.store.actions) == 3
    assert len(harness.store.events) == 1
    assert isinstance(harness.store.events[0], SuppressionAdded)
    allows = [row for row in harness.audit.records if row["rule"].startswith("allow:")]
    assert len(allows) == 1
    rendered_audit = repr(harness.audit.records)
    assert target.canonical_id not in rendered_audit
    assert "reply_event_ref_1" not in rendered_audit


async def test_account_suppression_stops_all_contacts_for_account() -> None:
    harness = _build()
    first = _active_enrollment(harness)
    second = _active_enrollment(
        harness,
        campaign_id=CampaignId(new_id("cmp")),
        contact_point_id=ContactPointId(new_id("cp")),
    )
    other = _active_enrollment(
        harness, account_id=ProspectAccountId(new_id("acc"))
    )
    for row in (first, second, other):
        harness.store.enrollments[row.enrollment_id] = row
    result = await harness.service.add_suppression(
        harness.tenant,
        _suppression_request(SuppressionTarget(account_id=harness.account)),
        actor=harness.boss,
    )
    assert result.stopped_count == 2
    assert harness.store.enrollments[other.enrollment_id].state is EnrollmentState.ENROLLED


async def test_same_key_same_payload_returns_original_and_changed_payload_conflicts() -> None:
    harness = _build()
    target = SuppressionTarget(contact_point_id=harness.contact)
    request = _suppression_request(target)
    first = await harness.service.add_suppression(
        harness.tenant, request, actor=harness.boss
    )
    action_count = len(harness.store.actions)
    event_count = len(harness.store.events)
    second = await harness.service.add_suppression(
        harness.tenant, request, actor=harness.boss
    )
    assert first.created is True
    assert second.created is False
    assert second.suppression == first.suppression
    assert second.stopped_count == 0
    assert len(harness.store.actions) == action_count
    assert len(harness.store.events) == event_count

    changed = (
        replace(
            request,
            target=SuppressionTarget(account_id=harness.account),
        ),
        replace(request, reason=SuppressionReason.COMPLAINT),
        replace(request, source_ref="different_ref"),
        replace(request, occurred_at=NOW + timedelta(seconds=1)),
    )
    for candidate in changed:
        with pytest.raises(IdempotencyConflictError):
            await harness.service.add_suppression(
                harness.tenant, candidate, actor=harness.boss
            )
    assert len(harness.store.suppressions) == 1


async def test_suppression_time_boundary_uses_only_injected_utc_clock() -> None:
    accepted = _build()
    target = SuppressionTarget(contact_point_id=accepted.contact)
    result = await accepted.service.add_suppression(
        accepted.tenant,
        _suppression_request(
            target,
            occurred_at=NOW + timedelta(minutes=5),
        ),
        actor=accepted.boss,
    )
    assert result.created is True

    rejected = _build()
    with pytest.raises(ValidationError):
        await rejected.service.add_suppression(
            rejected.tenant,
            _suppression_request(
                SuppressionTarget(contact_point_id=rejected.contact),
                occurred_at=NOW + timedelta(minutes=5, microseconds=1),
            ),
            actor=rejected.boss,
        )
    assert rejected.store.suppressions == []


@pytest.mark.parametrize("failure_point", ["lock", "action", "outbox"])
async def test_suppression_write_failure_rolls_back_everything_and_zero_allow(
    failure_point: str,
) -> None:
    class StorageFailure(RuntimeError):
        pass

    sentinel = StorageFailure("storage failure marker")
    harness = _build()
    enrollment = _active_enrollment(harness)
    harness.store.enrollments[enrollment.enrollment_id] = enrollment
    original = harness.service._uow_factory

    def failing_factory(tenant):
        uow = original(tenant)
        if failure_point == "lock":
            async def fail_lock(*_args):
                raise sentinel

            uow.enrollments.lock_matching_active = fail_lock
        elif failure_point == "action":
            async def fail_action(*_args):
                raise sentinel

            uow.actions.append = fail_action
        else:
            async def fail_outbox(*_args):
                raise sentinel

            uow.bus.publish = fail_outbox
        return uow

    harness.service._uow_factory = failing_factory
    with pytest.raises(StorageFailure) as captured:
        await harness.service.add_suppression(
            harness.tenant,
            _suppression_request(
                SuppressionTarget(contact_point_id=harness.contact)
            ),
            actor=harness.boss,
        )
    assert captured.value is sentinel
    assert harness.store.suppressions == []
    assert harness.store.enrollments[enrollment.enrollment_id].state is EnrollmentState.ENROLLED
    assert harness.store.actions == {}
    assert harness.store.events == []
    assert not any(row["rule"].startswith("allow:") for row in harness.audit.records)


async def test_existing_winner_from_other_tenant_fails_closed() -> None:
    harness = _build()
    target = SuppressionTarget(contact_point_id=harness.contact)
    other = TenantId(new_id("tn"))
    winner = SuppressionEntry(
        other,
        SuppressionId(new_id("sup")),
        target,
        SuppressionReason.UNSUBSCRIBE,
        NOW,
        "reply_event_ref_1",
        IdempotencyKey("suppression-key-1"),
        NOW,
    )
    contract = importlib.import_module("domains.outreach.repository")
    original = harness.service._uow_factory

    def malicious_factory(tenant):
        uow = original(tenant)

        async def malicious_append(_entry):
            return contract.SuppressionAppendResult(
                contract.AppendStatus.EXISTING, winner
            )

        uow.suppressions.append_if_absent = malicious_append
        return uow

    harness.service._uow_factory = malicious_factory
    with pytest.raises(importlib.import_module("shared.errors").TenantIsolationViolation):
        await harness.service.add_suppression(
            harness.tenant,
            _suppression_request(target),
            actor=harness.boss,
        )
    assert not any(row["rule"].startswith("allow:") for row in harness.audit.records)


async def test_repository_winner_payload_is_rechecked_by_service() -> None:
    harness = _build()
    target = SuppressionTarget(contact_point_id=harness.contact)
    winner = SuppressionEntry(
        harness.tenant,
        SuppressionId(new_id("sup")),
        SuppressionTarget(account_id=harness.account),
        SuppressionReason.UNSUBSCRIBE,
        NOW,
        "reply_event_ref_1",
        IdempotencyKey("suppression-key-1"),
        NOW,
    )
    contract = importlib.import_module("domains.outreach.repository")
    original = harness.service._uow_factory

    def malicious_factory(tenant):
        uow = original(tenant)

        async def malicious_append(_entry):
            return contract.SuppressionAppendResult(
                contract.AppendStatus.EXISTING, winner
            )

        uow.suppressions.append_if_absent = malicious_append
        return uow

    harness.service._uow_factory = malicious_factory
    with pytest.raises(IdempotencyConflictError):
        await harness.service.add_suppression(
            harness.tenant,
            _suppression_request(target),
            actor=harness.boss,
        )
    assert not any(row["rule"].startswith("allow:") for row in harness.audit.records)


async def test_suppression_reads_return_copies_after_resource_authorization() -> None:
    harness = _build()
    target = SuppressionTarget(account_id=harness.account)
    created = await harness.service.add_suppression(
        harness.tenant,
        _suppression_request(target),
        actor=harness.boss,
    )
    found = await harness.service.is_suppressed(
        harness.tenant, target, actor=harness.boss
    )
    listed = await harness.service.list_suppressions(
        harness.tenant,
        harness.boss.scope,
        limit=10,
        actor=harness.boss,
    )
    assert found == created.suppression
    assert listed == [created.suppression]
    assert found is not created.suppression
    assert listed[0] is not found


async def test_is_suppressed_propagates_storage_failure() -> None:
    harness = _build()
    target = SuppressionTarget(contact_point_id=harness.contact)

    async def fail(*_args):
        raise TransientError("storage unavailable")

    probe = harness.service._uow_factory(harness.tenant)
    probe.suppressions.find_current = fail
    harness.service._uow_factory = lambda _tenant: probe
    with pytest.raises(TransientError):
        await harness.service.is_suppressed(
            harness.tenant, target, actor=harness.boss
        )
    assert not any(row["rule"].startswith("allow:") for row in harness.audit.records)


async def test_list_reauthorizes_every_row_and_fails_whole_result() -> None:
    harness = _build()
    allowed = SuppressionTarget(account_id=harness.account)
    other = SuppressionTarget(account_id=ProspectAccountId(new_id("acc")))
    for index, target in enumerate((allowed, other), 1):
        harness.store.suppressions.append(
            SuppressionEntry(
                harness.tenant,
                SuppressionId(new_id("sup")),
                target,
                SuppressionReason.MANUAL_BLOCK,
                NOW,
                f"safe_ref_{index}",
                IdempotencyKey(f"list-key-{index}"),
                NOW,
            )
        )
    actor = _manager(allowed)
    harness.service._authorizer = Phase1OutreachAuthorizer(harness.tenant)
    original = harness.service._uow_factory

    def malicious_factory(tenant):
        uow = original(tenant)

        async def malicious_list(*_args):
            return copy.deepcopy(harness.store.suppressions)

        uow.suppressions.list_scoped = malicious_list
        return uow

    harness.service._uow_factory = malicious_factory
    with pytest.raises(PermissionDenied):
        await harness.service.list_suppressions(
            harness.tenant, actor.scope, limit=10, actor=actor
        )
    assert not any(row["rule"].startswith("allow:") for row in harness.audit.records)


async def test_list_authorizes_requested_scope_before_limit_validation() -> None:
    harness = _build(denied=True)
    with pytest.raises(PermissionDenied):
        await harness.service.list_suppressions(
            harness.tenant, harness.boss.scope, limit=0, actor=harness.boss
        )
    assert harness.trace.calls == [
        ("preauthorize", "suppression:list"),
        ("audit", "suppression:list", "deny:authorization"),
    ]
