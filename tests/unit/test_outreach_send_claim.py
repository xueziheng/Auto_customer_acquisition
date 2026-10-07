"""发送前 current-fact DTO 与 claim 的公共安全契约。"""

from __future__ import annotations

import copy
import importlib
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest

from shared.errors import TradeOSError, ValidationError
from shared.schemas.identifiers import (
    CampaignId,
    ContactPointId,
    EnrollmentId,
    IdempotencyKey,
    MessageAttemptId,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    new_id,
)


def test_message_send_preflight_is_frozen_and_contains_only_safe_binding() -> None:
    """Gateway 只接收资源 ID/版本/步骤/键，不接收地址、正文或主题。"""
    schemas = importlib.import_module("domains.outreach.schemas")
    values = {
        "tenant_id": TenantId(new_id("tn")),
        "attempt_id": MessageAttemptId(new_id("mat")),
        "campaign_id": CampaignId(new_id("cmp")),
        "enrollment_id": EnrollmentId(new_id("enr")),
        "account_id": ProspectAccountId(new_id("acc")),
        "contact_point_id": ContactPointId(new_id("cp")),
        "sending_identity_id": SendingIdentityId(new_id("sid")),
        "campaign_version": 2,
        "step_number": 1,
        "idempotency_key": IdempotencyKey("send-claim-key"),
    }
    preflight = schemas.MessageSendPreflight(**values)
    assert set(preflight.__dataclass_fields__) == set(values)
    assert not {"address", "subject", "body"} & set(preflight.__dataclass_fields__)
    with pytest.raises(FrozenInstanceError):
        preflight.step_number = 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("campaign_version", 0),
        ("campaign_version", True),
        ("step_number", 0),
        ("step_number", True),
        ("idempotency_key", ""),
    ],
)
def test_message_send_preflight_rejects_invalid_binding(
    field: str, value: object
) -> None:
    schemas = importlib.import_module("domains.outreach.schemas")
    values: dict[str, object] = {
        "tenant_id": TenantId(new_id("tn")),
        "attempt_id": MessageAttemptId(new_id("mat")),
        "campaign_id": CampaignId(new_id("cmp")),
        "enrollment_id": EnrollmentId(new_id("enr")),
        "account_id": ProspectAccountId(new_id("acc")),
        "contact_point_id": ContactPointId(new_id("cp")),
        "sending_identity_id": SendingIdentityId(new_id("sid")),
        "campaign_version": 1,
        "step_number": 1,
        "idempotency_key": IdempotencyKey("send-claim-key"),
    }
    values[field] = value
    with pytest.raises(ValidationError):
        schemas.MessageSendPreflight(**values)


def test_sending_attempt_requires_utc_claim_time() -> None:
    models = importlib.import_module("domains.outreach.models")
    with pytest.raises(ValidationError):
        models.MessageAttempt(
            tenant_id=TenantId(new_id("tn")),
            attempt_id=MessageAttemptId(new_id("mat")),
            message_id=new_id("msg"),
            campaign_id=CampaignId(new_id("cmp")),
            enrollment_id=EnrollmentId(new_id("enr")),
            campaign_version=1,
            step_number=1,
            sending_identity_id=SendingIdentityId(new_id("sid")),
            idempotency_key=IdempotencyKey("send-claim-key"),
            state=models.MessageAttemptState.SENDING,
            provider_ref=None,
            failure_category=None,
            send_claimed_at=datetime(2026, 8, 11, 4, 0, tzinfo=UTC).replace(
                tzinfo=None
            ),
            created_at=datetime(2026, 8, 11, 4, 0, tzinfo=UTC),
            updated_at=datetime(2026, 8, 11, 4, 0, tzinfo=UTC),
        )


async def _prepared_harness() -> tuple[object, object, object]:
    helpers = importlib.import_module("tests.unit.test_outreach_enrollment_service")
    harness = helpers._build()
    enrollment = await harness.service.enroll(
        harness.tenant,
        harness.campaign_id,
        helpers._request(harness),
        actor=harness.boss,
    )
    actor = helpers._system(harness, enrollment.enrollment_id)
    attempt = await harness.service.prepare_message_attempt(
        harness.tenant, enrollment.enrollment_id, actor=actor
    )
    return harness, actor, attempt


async def test_prepare_attempt_reads_approval_before_campaign_lock() -> None:
    """审批查询若放在 Campaign 锁内，同 Attempt 并发会耗尽连接池。"""
    helpers = importlib.import_module("tests.unit.test_outreach_enrollment_service")
    harness = helpers._build()
    enrollment = await harness.service.enroll(
        harness.tenant,
        harness.campaign_id,
        helpers._request(harness),
        actor=harness.boss,
    )
    actor = helpers._system(harness, enrollment.enrollment_id)
    harness.trace.calls.clear()

    await harness.service.prepare_message_attempt(
        harness.tenant, enrollment.enrollment_id, actor=actor
    )

    approval_index = next(
        index for index, call in enumerate(harness.trace.calls) if call[0] == "approval"
    )
    campaign_lock_index = next(
        index
        for index, call in enumerate(harness.trace.calls)
        if call[0] == "campaign_lock"
    )
    assert approval_index < campaign_lock_index


async def test_preflight_reads_approval_before_campaign_lock() -> None:
    """发送 current-fact 复核必须在锁外取不可变审批事实。"""
    harness, actor, attempt = await _prepared_harness()
    harness.trace.calls.clear()

    await harness.service.preflight_message_send(
        harness.tenant, attempt.attempt_id, actor=actor
    )

    approval_index = next(
        index for index, call in enumerate(harness.trace.calls) if call[0] == "approval"
    )
    campaign_lock_index = next(
        index
        for index, call in enumerate(harness.trace.calls)
        if call[0] == "campaign_lock"
    )
    assert approval_index < campaign_lock_index


@pytest.mark.parametrize(
    "mutation",
    [
        "campaign_inactive",
        "wrong_version",
        "approval_rejected",
        "enrollment_stopped",
        "step_mismatch",
        "reply_received",
        "contact_suppressed",
        "account_suppressed",
        "contact_unverified",
        "wrong_account",
        "illegal_basis",
        "disallowed_country",
        "disallowed_entity",
        "disallowed_category",
        "sender_outside_campaign",
        "sender_unavailable",
    ],
)
async def test_preflight_rejects_each_stale_current_fact_without_write_or_allow(
    mutation: str,
) -> None:
    """准备时曾通过的事实失效后，Gateway preflight 必须重新读取并拒绝。"""
    helpers = importlib.import_module("tests.unit.test_outreach_enrollment_service")
    models = importlib.import_module("domains.outreach.models")
    schemas = importlib.import_module("domains.outreach.schemas")
    harness, actor, attempt = await _prepared_harness()
    enrollment = harness.store.enrollments[attempt.enrollment_id]
    if mutation == "campaign_inactive":
        harness.store.campaigns[harness.campaign_id].state = models.CampaignState.PAUSED
    elif mutation == "wrong_version":
        harness.store.attempts[attempt.attempt_id].campaign_version = 2
    elif mutation == "approval_rejected":
        current = harness.approvals.values[(harness.campaign_id, 1)]
        harness.approvals.values[(harness.campaign_id, 1)] = schemas.CampaignApprovalSnapshot(
            current.tenant_id,
            current.campaign_id,
            current.version,
            current.approval_id,
            schemas.CampaignApprovalState.REJECTED,
            None,
            None,
        )
    elif mutation == "enrollment_stopped":
        enrollment.state = models.EnrollmentState.STOPPED_MANUAL
        enrollment.stopped_at = helpers.NOW
        enrollment.stop_reason = models.EnrollmentStopReason.MANUAL
        enrollment.next_send_at = None
    elif mutation == "step_mismatch":
        harness.store.attempts[attempt.attempt_id].step_number = 2
    elif mutation == "reply_received":
        harness.replies.snapshots[(harness.contact, harness.account)] = schemas.ReplyStatusSnapshot(
            harness.tenant,
            harness.contact,
            harness.account,
            schemas.ReplyState.REPLIED,
            helpers.NOW,
            helpers.NOW,
        )
    elif mutation in {"contact_suppressed", "account_suppressed"}:
        target = (
            schemas.SuppressionTarget(contact_point_id=harness.contact)
            if mutation == "contact_suppressed"
            else schemas.SuppressionTarget(account_id=harness.account)
        )
        harness.store.suppressions.append(
            models.SuppressionEntry(
                harness.tenant,
                new_id("sup"),
                target,
                models.SuppressionReason.MANUAL_BLOCK,
                helpers.NOW,
                "manual_ref_1",
                IdempotencyKey(f"{mutation}-key"),
                helpers.NOW,
            )
        )
    elif mutation in {
        "contact_unverified",
        "wrong_account",
        "illegal_basis",
        "disallowed_country",
        "disallowed_entity",
        "disallowed_category",
    }:
        changes: dict[str, object] = {
            "contact_unverified": {
                "verification": schemas.ContactVerificationStatus.UNVERIFIED,
                "verified_at": None,
            },
            "wrong_account": {"contact_belongs_to_account": False},
            "illegal_basis": {},
            "disallowed_country": {"country": "CN"},
            "disallowed_entity": {"entity_type": "retailer"},
            "disallowed_category": {"qualified_categories": frozenset({"toys"})},
        }[mutation]
        harness.contacts.snapshots[(harness.contact, harness.account)] = helpers._contact(
            harness.tenant, harness.contact, harness.account, **changes
        )
        if mutation == "illegal_basis":
            object.__setattr__(
                harness.contacts.snapshots[(harness.contact, harness.account)],
                "legal_basis",
                None,
            )
    elif mutation == "sender_outside_campaign":
        outside = SendingIdentityId(new_id("sid"))
        harness.store.attempts[attempt.attempt_id].sending_identity_id = outside
        harness.senders.snapshots[outside] = helpers._sender(harness.tenant, outside)
    elif mutation == "sender_unavailable":
        harness.senders.snapshots[attempt.sending_identity_id] = helpers._sender(
            harness.tenant, attempt.sending_identity_id, sendable=False
        )
    before = copy.deepcopy(harness.store)
    allow_before = sum(record["rule"].startswith("allow:") for record in harness.audit.records)
    with pytest.raises(TradeOSError):
        await harness.service.preflight_message_send(
            harness.tenant, attempt.attempt_id, actor=actor
        )
    assert harness.store == before
    assert sum(
        record["rule"].startswith("allow:") for record in harness.audit.records
    ) == allow_before


async def test_preflight_returns_exact_safe_binding_and_claim_is_idempotent() -> None:
    """相同 Attempt 重复 claim 只产生一次状态转换与一次 action。"""
    models = importlib.import_module("domains.outreach.models")
    harness, actor, attempt = await _prepared_harness()
    preflight = await harness.service.preflight_message_send(
        harness.tenant, attempt.attempt_id, actor=actor
    )
    assert preflight.attempt_id == attempt.attempt_id
    assert preflight.idempotency_key == attempt.idempotency_key
    assert preflight.account_id == harness.account
    actions_before = len(harness.store.actions)
    claimed = await harness.service.claim_message_send(
        harness.tenant, attempt.attempt_id, actor=actor
    )
    retry = await harness.service.claim_message_send(
        harness.tenant, attempt.attempt_id, actor=actor
    )
    assert claimed == retry
    assert claimed.state is models.MessageAttemptState.SENDING
    assert claimed.send_claimed_at == helpers_now()
    assert len(harness.store.actions) == actions_before + 1


async def test_record_sent_and_failure_require_an_authoritative_send_claim() -> None:
    """域服务不能绕过 Gateway claim，直接把 RESERVED 记成已发或失败。"""
    models = importlib.import_module("domains.outreach.models")
    harness, actor, attempt = await _prepared_harness()
    for operation in (
        lambda: harness.service.record_sent(
            harness.tenant, attempt.attempt_id, "provider_ref_1", actor=actor
        ),
        lambda: harness.service.record_send_failure(
            harness.tenant,
            attempt.attempt_id,
            models.SendFailureCategory.PROVIDER_TRANSIENT,
            actor=actor,
        ),
    ):
        with pytest.raises(TradeOSError):
            await operation()
    assert harness.store.attempts[attempt.attempt_id].state is models.MessageAttemptState.RESERVED
    assert harness.store.events == []


@pytest.mark.parametrize(
    ("category", "expected", "stops"),
    [
        ("rate_limited", "failed_transient", False),
        ("provider_transient", "failed_transient", False),
        ("provider_auth_required", "failed_transient", False),
        ("provider_permanent", "failed_permanent", False),
        ("identity_unavailable", "failed_permanent", True),
    ],
)
async def test_claimed_attempt_failure_mapping_is_exact_and_idempotent(
    category: str, expected: str, stops: bool
) -> None:
    models = importlib.import_module("domains.outreach.models")
    harness, actor, attempt = await _prepared_harness()
    await harness.service.claim_message_send(
        harness.tenant, attempt.attempt_id, actor=actor
    )
    typed_category = models.SendFailureCategory(category)
    first = await harness.service.record_send_failure(
        harness.tenant, attempt.attempt_id, typed_category, actor=actor
    )
    retry = await harness.service.record_send_failure(
        harness.tenant, attempt.attempt_id, typed_category, actor=actor
    )
    assert first == retry
    assert first.state.value == expected
    enrollment = harness.store.enrollments[attempt.enrollment_id]
    assert (enrollment.state is models.EnrollmentState.STOPPED_IDENTITY_UNAVAILABLE) is stops


async def test_claimed_attempt_records_sent_once_and_rejects_changed_provider_ref() -> None:
    harness, actor, attempt = await _prepared_harness()
    await harness.service.claim_message_send(
        harness.tenant, attempt.attempt_id, actor=actor
    )
    first = await harness.service.record_sent(
        harness.tenant, attempt.attempt_id, "provider_ref_1", actor=actor
    )
    retry = await harness.service.record_sent(
        harness.tenant, attempt.attempt_id, "provider_ref_1", actor=actor
    )
    assert first == retry
    assert len(harness.store.events) == 1
    with pytest.raises(TradeOSError):
        await harness.service.record_sent(
            harness.tenant, attempt.attempt_id, "provider_ref_2", actor=actor
        )


@pytest.mark.parametrize("reason", ["manual", "reply", "suppression", "hard_bounce", "identity_unavailable"])
@pytest.mark.parametrize("final_step", [False, True])
async def test_inflight_send_completion_preserves_enrollment_stop_without_scheduling(
    reason: str, final_step: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已获 claim 的发送完成是事实，不能重启已停止的序列。"""
    models = importlib.import_module("domains.outreach.models")
    harness, actor, attempt = await _prepared_harness()
    if final_step:
        await harness.service.claim_message_send(
            harness.tenant, attempt.attempt_id, actor=actor
        )
        await harness.service.record_sent(
            harness.tenant, attempt.attempt_id, "provider_ref_first", actor=actor
        )
        monkeypatch.setattr(harness.service, "_now", lambda: helpers_now() + timedelta(days=2))
        attempt = await harness.service.prepare_message_attempt(
            harness.tenant, attempt.enrollment_id, actor=actor
        )
    await harness.service.claim_message_send(
        harness.tenant, attempt.attempt_id, actor=actor
    )
    stopped = await harness.service.stop_enrollment(
        harness.tenant, attempt.enrollment_id, models.EnrollmentStopReason(reason),
        actor=harness.boss if reason == "manual" else actor,
    )
    event_count = len(harness.store.events)
    action_count = len(harness.store.actions)
    monkeypatch.setattr(
        harness.service,
        "_now",
        lambda: helpers_now() + timedelta(days=2 if final_step else 0, minutes=1),
    )

    sent = await harness.service.record_sent(
        harness.tenant, attempt.attempt_id, "provider_ref_confirmed", actor=actor
    )
    replay = await harness.service.record_sent(
        harness.tenant, attempt.attempt_id, "provider_ref_confirmed", actor=actor
    )

    assert sent == replay
    assert sent.state is models.MessageAttemptState.SENT
    assert sent.provider_ref == "provider_ref_confirmed"
    current = await harness.service.get_enrollment(
        harness.tenant, attempt.enrollment_id, actor=harness.boss
    )
    assert (current.state, current.stop_reason, current.stopped_at) == (
        stopped.state, stopped.stop_reason, stopped.stopped_at
    )
    assert current.current_step == (2 if final_step else 1)
    assert current.next_send_at is None
    assert len(harness.store.events) == event_count + 1
    assert len(harness.store.actions) == action_count + 1
    with pytest.raises(TradeOSError):
        await harness.service.prepare_message_attempt(
            harness.tenant, attempt.enrollment_id, actor=actor
        )
    with pytest.raises(TradeOSError):
        await harness.service.claim_message_send(
            harness.tenant, attempt.attempt_id, actor=actor
        )


async def test_sent_attempt_preflight_reaches_idempotency_but_cannot_be_claimed_again() -> None:
    """已成功发送只允许 Gateway 查 canonical；账本缺失时仍不得再次 claim。"""
    harness, actor, attempt = await _prepared_harness()
    await harness.service.claim_message_send(
        harness.tenant, attempt.attempt_id, actor=actor
    )
    await harness.service.record_sent(
        harness.tenant, attempt.attempt_id, "provider_ref_1", actor=actor
    )

    preflight = await harness.service.preflight_message_send(
        harness.tenant, attempt.attempt_id, actor=actor
    )

    assert preflight.attempt_id == attempt.attempt_id
    assert preflight.idempotency_key == attempt.idempotency_key
    with pytest.raises(TradeOSError):
        await harness.service.claim_message_send(
            harness.tenant, attempt.attempt_id, actor=actor
        )


def helpers_now() -> datetime:
    helpers = importlib.import_module("tests.unit.test_outreach_enrollment_service")
    return helpers.NOW
