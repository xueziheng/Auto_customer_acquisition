"""领域事件到持久通知任务的安全投影。"""

from __future__ import annotations

import hashlib
import importlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from infra.db.outbox import serialize
from notification_gateway.jobs import NotificationKind
from notification_gateway.models import NotificationPriority
from shared.errors import TenantIsolationViolation, ValidationError
from shared.events.catalog import (
    ApprovalDecided,
    CommitmentOverdue,
    DomainEvent,
    HandoffQueueBacklogged,
    HandoffRequested,
    ReputationThresholdBreached,
    SendingIdentitySuspended,
)
from shared.schemas.identifiers import (
    ApprovalId,
    CommitmentId,
    EmployeeId,
    HandoffId,
    OpportunityId,
    SendingIdentityId,
    TenantId,
    new_id,
)
from workflows.human_handoff.flow import HandoffEscalationNotice

_NOW = datetime(2026, 8, 14, 9, 30, tzinfo=UTC)
_TENANT = TenantId(new_id("tn"))
_RECIPIENT = EmployeeId(new_id("emp"))


def _load(name: str):
    try:
        return getattr(
            importlib.import_module("apps.scheduler_worker.notification_projection"),
            name,
        )
    except (AttributeError, ModuleNotFoundError) as exc:
        pytest.fail(f"RED：通知投影 {name} 尚未实现（{exc}）")


class _Audience:
    def __init__(self, members: tuple[object, ...]) -> None:
        self.members = members
        self.calls: list[tuple[TenantId, DomainEvent]] = []

    async def recipients_for(
        self, tenant_id: TenantId, event: DomainEvent
    ) -> tuple[object, ...]:
        self.calls.append((tenant_id, event))
        return self.members


class _Jobs:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.unique: set[tuple[object, ...]] = set()

    async def enqueue(self, job: object) -> bool:
        self.calls.append(job)
        key = (
            job.tenant_id,
            job.source_event_fingerprint,
            job.recipient,
            job.context.kind,
        )
        if key in self.unique:
            return False
        self.unique.add(key)
        return True


def _events() -> list[tuple[DomainEvent, object, tuple[object, ...]]]:
    handoff_id = HandoffId(new_id("han"))
    opportunity_id = OpportunityId(new_id("opp"))
    identity_id = SendingIdentityId(new_id("sid"))
    commitment_id = CommitmentId(new_id("com"))
    approval_id = ApprovalId(new_id("apr"))
    decider = EmployeeId(new_id("emp"))
    return [
        (
            HandoffRequested(
                _TENANT,
                _NOW,
                None,
                handoff_id,
                opportunity_id,
                _RECIPIENT,
                "customer_free_text_must_not_persist",
            ),
            NotificationPriority.URGENT,
            (
                NotificationKind.HANDOFF_ESCALATION,
                str(handoff_id),
                str(opportunity_id),
                None,
                None,
            ),
        ),
        (
            HandoffQueueBacklogged(_TENANT, _NOW, None, 7, 480),
            NotificationPriority.URGENT,
            (
                NotificationKind.HANDOFF_QUEUE_BACKLOGGED,
                "handoff_queue",
                None,
                None,
                7,
            ),
        ),
        (
            SendingIdentitySuspended(
                _TENANT, _NOW, None, identity_id, "hard_bounce_rate"
            ),
            NotificationPriority.URGENT,
            (
                NotificationKind.SENDING_IDENTITY_SUSPENDED,
                str(identity_id),
                None,
                "hard_bounce_rate",
                None,
            ),
        ),
        (
            ReputationThresholdBreached(
                _TENANT,
                _NOW,
                None,
                identity_id,
                "complaint_rate",
                "0.01",
                "0.001",
                "watch",
            ),
            NotificationPriority.NORMAL,
            (
                NotificationKind.REPUTATION_THRESHOLD_BREACHED,
                str(identity_id),
                None,
                "complaint_rate:watch",
                None,
            ),
        ),
        (
            CommitmentOverdue(_TENANT, _NOW, None, str(commitment_id), 3600),
            NotificationPriority.URGENT,
            (
                NotificationKind.COMMITMENT_OVERDUE,
                str(commitment_id),
                None,
                None,
                None,
            ),
        ),
        (
            ApprovalDecided(
                _TENANT, _NOW, None, str(approval_id), "approved", decider
            ),
            NotificationPriority.NORMAL,
            (
                NotificationKind.APPROVAL_DECIDED,
                str(approval_id),
                str(decider),
                "approved",
                None,
            ),
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("event,priority,expected_context", _events())
async def test_supported_events_project_exact_typed_jobs(
    event: DomainEvent,
    priority: NotificationPriority,
    expected_context: tuple[object, ...],
) -> None:
    """错 kind/ID/priority/recipient 会把员工带到错误任务。"""
    Member = _load("NotificationAudienceMember")
    Handler = _load("NotificationProjectionHandler")
    jobs = _Jobs()
    audience = _Audience((Member(_TENANT, _RECIPIENT),))
    handler = Handler(
        tenant_id=_TENANT,
        audience=audience,
        jobs=jobs,
        now=lambda: _NOW,
        id_factory=lambda prefix: f"{prefix}_fixed",
    )

    await handler.handle(event)

    assert len(jobs.calls) == 1
    job = jobs.calls[0]
    assert (job.tenant_id, job.recipient, job.priority) == (
        _TENANT,
        _RECIPIENT,
        priority,
    )
    assert (
        job.context.kind,
        job.context.primary_id,
        job.context.secondary_id,
        job.context.reason_code,
        job.context.level,
    ) == expected_context
    assert job.source_event == type(event).__name__
    assert job.dedup_key == f"{job.source_event_fingerprint}:{_RECIPIENT}"
    assert "customer_free_text" not in repr(job.context)


def test_source_fingerprint_is_canonical_sha256_and_payload_sensitive() -> None:
    """随机编码或忽略 typed payload 会破坏 Outbox 重放幂等。"""
    fingerprint = _load("notification_source_fingerprint")
    first = HandoffQueueBacklogged(_TENANT, _NOW, None, 7, 480)
    second = HandoffQueueBacklogged(_TENANT, _NOW, None, 8, 480)
    canonical = json.dumps(serialize(first), sort_keys=True, separators=(",", ":"))
    expected = hashlib.sha256(
        f"HandoffQueueBacklogged:{canonical}".encode()
    ).hexdigest()

    assert fingerprint(first) == expected
    assert fingerprint(second) != expected
    assert len(expected) == 64 and expected == expected.lower()
    assert canonical not in expected


@pytest.mark.asyncio
async def test_duplicate_delivery_enqueues_twice_but_has_one_durable_job() -> None:
    """跳过第二次 enqueue 会让 handler 无法依赖数据库唯一键闭合重放窗口。"""
    Member = _load("NotificationAudienceMember")
    Handler = _load("NotificationProjectionHandler")
    jobs = _Jobs()
    handler = Handler(
        tenant_id=_TENANT,
        audience=_Audience((Member(_TENANT, _RECIPIENT),)),
        jobs=jobs,
        now=lambda: _NOW,
        id_factory=lambda prefix: f"{prefix}_{len(jobs.calls)}",
    )
    event = HandoffQueueBacklogged(_TENANT, _NOW, None, 7, 480)

    await handler.handle(event)
    await handler.handle(event)

    assert len(jobs.calls) == 2
    assert len(jobs.unique) == 1


@pytest.mark.asyncio
async def test_projection_rejects_unsupported_before_audience_lookup() -> None:
    """未知事件不可借 audience lookup 产生任何副作用。"""
    Handler = _load("NotificationProjectionHandler")
    audience = _Audience(())
    jobs = _Jobs()
    handler = Handler(
        tenant_id=_TENANT,
        audience=audience,
        jobs=jobs,
        now=lambda: _NOW,
        id_factory=lambda prefix: f"{prefix}_fixed",
    )

    with pytest.raises(ValidationError, match="通知事件类型不受支持"):
        await handler.handle(DomainEvent(_TENANT, _NOW))
    assert audience.calls == [] and jobs.calls == []


@pytest.mark.asyncio
async def test_projection_wrong_tenant_foreign_member_and_empty_audience_fail_closed() -> None:
    """事件或 resolver 跨租户、无收件人时均不得创建任务。"""
    Member = _load("NotificationAudienceMember")
    Handler = _load("NotificationProjectionHandler")
    event = HandoffQueueBacklogged(_TENANT, _NOW, None, 7, 480)
    other = TenantId(new_id("tn"))
    for handler_tenant, members, error in (
        (other, (Member(other, _RECIPIENT),), TenantIsolationViolation),
        (_TENANT, (Member(other, _RECIPIENT),), TenantIsolationViolation),
        (_TENANT, (), None),
    ):
        jobs = _Jobs()
        handler = Handler(
            tenant_id=handler_tenant,
            audience=_Audience(members),
            jobs=jobs,
            now=lambda: _NOW,
            id_factory=lambda prefix: f"{prefix}_fixed",
        )
        if error is None:
            await handler.handle(event)
        else:
            with pytest.raises(error):
                await handler.handle(event)
        assert jobs.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("level", ["owner", "manager", "boss"])
async def test_handoff_notifier_persists_string_level_as_reason_code(level: str) -> None:
    """升级字符串不能伪造成整数 level，也不能在 scheduler 内直投渠道。"""
    Notifier = _load("NotificationJobHandoffNotifier")
    jobs = _Jobs()
    handoff_id = HandoffId(new_id("han"))
    opportunity_id = OpportunityId(new_id("opp"))
    notice = HandoffEscalationNotice(
        _TENANT,
        handoff_id,
        opportunity_id,
        _RECIPIENT,
        _RECIPIENT,
        level,
        _NOW,
        _NOW + timedelta(hours=1),
        f"human_handoff:{handoff_id}:{level}",
    )
    notifier = Notifier(
        jobs,
        now=lambda: _NOW,
        id_factory=lambda prefix: f"{prefix}_fixed",
    )

    await notifier.notify(notice)

    job = jobs.calls[0]
    expected = hashlib.sha256(
        f"HandoffEscalationNotice:{notice.dedup_key}".encode()
    ).hexdigest()
    assert job.source_event_fingerprint == expected
    assert job.context.reason_code == level
    assert job.context.level is None
    assert job.context.secondary_id == str(opportunity_id)
    assert job.dedup_key == notice.dedup_key
