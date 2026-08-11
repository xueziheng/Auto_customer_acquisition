"""用真实 PostgreSQL 演示 Slice 4B-1 触达服务，不构成生产 composition。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from collections import Counter
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from domains.outreach.permissions import (
    Actor,
    OutreachScope,
    Phase1OutreachAuthorizer,
    ScopeLevel,
    StandardAuditLogger,
)
from domains.outreach.schemas import (
    CampaignApprovalSnapshot,
    CampaignApprovalState,
    CampaignCreateRequest,
    ContactEligibilitySnapshot,
    ContactLegalBasis,
    ContactVerificationStatus,
    EnrollmentCreateRequest,
    OutreachSenderRole,
    ReplyState,
    ReplyStatusSnapshot,
    SendingIdentityEligibilitySnapshot,
    SequenceStepRequest,
    StepIntent,
    SuppressionReason,
    SuppressionRequest,
    SuppressionTarget,
)
from domains.outreach.service_impl import OutreachServiceImpl
from infra.db.outreach_uow import SqlAlchemyOutreachUnitOfWork
from infra.db.session import create_engine_from
from infra.db.tables import OutboxEventRow, OutreachCampaignRow
from shared.errors import PermissionDenied
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    ContactPointId,
    EmployeeId,
    EnrollmentId,
    IdempotencyKey,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    new_id,
)

_AUDIT_LOGGER_NAME = "security.authorization.outreach.demo"
_FAILURE_MESSAGE = "触达演示运行失败"
_NOW = datetime(2026, 8, 11, 14, tzinfo=UTC)


class _SafeAuditFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "action": getattr(record, "action", ""),
                "actor": getattr(record, "actor", ""),
                "message": "授权审计",
                "rule": getattr(record, "rule", ""),
                "scope": getattr(record, "scope", ""),
                "tenant_id": getattr(record, "tenant_id", ""),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


def _configure_safe_audit_logging() -> None:
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(logging.NullHandler())
    root.setLevel(logging.CRITICAL)
    logger = logging.getLogger(_AUDIT_LOGGER_NAME)
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_SafeAuditFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


class _DemoContacts:
    def __init__(
        self,
        tenant_id: TenantId,
        snapshots: dict[
            tuple[ContactPointId, ProspectAccountId], ContactEligibilitySnapshot
        ],
    ) -> None:
        self._tenant_id = tenant_id
        self._snapshots = snapshots

    async def get_contact_eligibility(
        self,
        tenant_id: TenantId,
        contact_point_id: ContactPointId,
        account_id: ProspectAccountId,
    ) -> ContactEligibilitySnapshot:
        snapshot = self._snapshots.get((contact_point_id, account_id))
        if tenant_id != self._tenant_id or snapshot is None:
            raise PermissionDenied("演示联系人事实拒绝")
        return snapshot


class _DemoSenders:
    def __init__(
        self,
        tenant_id: TenantId,
        snapshots: dict[SendingIdentityId, SendingIdentityEligibilitySnapshot],
    ) -> None:
        self._tenant_id = tenant_id
        self._snapshots = snapshots

    async def get_sending_identity_eligibility(
        self, tenant_id: TenantId, identity_id: SendingIdentityId
    ) -> SendingIdentityEligibilitySnapshot:
        snapshot = self._snapshots.get(identity_id)
        if tenant_id != self._tenant_id or snapshot is None:
            raise PermissionDenied("演示发件身份事实拒绝")
        return snapshot


class _DemoApprovals:
    def __init__(self, tenant_id: TenantId) -> None:
        self._tenant_id = tenant_id
        self.values: dict[tuple[CampaignId, int], CampaignApprovalSnapshot] = {}

    async def get_campaign_approval(
        self, tenant_id: TenantId, campaign_id: CampaignId, version: int
    ) -> CampaignApprovalSnapshot | None:
        if tenant_id != self._tenant_id:
            raise PermissionDenied("演示审批事实拒绝")
        return self.values.get((campaign_id, version))


class _DemoReplies:
    def __init__(
        self,
        tenant_id: TenantId,
        snapshots: dict[
            tuple[ContactPointId, ProspectAccountId], ReplyStatusSnapshot
        ],
    ) -> None:
        self._tenant_id = tenant_id
        self._snapshots = snapshots

    async def get_reply_status(
        self,
        tenant_id: TenantId,
        contact_point_id: ContactPointId,
        account_id: ProspectAccountId,
    ) -> ReplyStatusSnapshot:
        snapshot = self._snapshots.get((contact_point_id, account_id))
        if tenant_id != self._tenant_id or snapshot is None:
            raise PermissionDenied("演示回复事实拒绝")
        return snapshot


class _UowFactory:
    def __init__(
        self, factory: async_sessionmaker, now: Callable[[], datetime]
    ) -> None:
        self._factory = factory
        self._now = now

    def __call__(self, tenant_id: TenantId) -> Any:
        return SqlAlchemyOutreachUnitOfWork(
            self._factory, tenant_id, now=self._now
        )


def _boss() -> Actor:
    return Actor(
        "boss:outreach-demo",
        OutreachScope(level=ScopeLevel.TENANT),
        "boss",
    )


def _system_for_enrollment(enrollment_id: EnrollmentId) -> Actor:
    return Actor(
        "system:outreach-demo",
        OutreachScope(
            level=ScopeLevel.SYSTEM,
            allowed_enrollment_ids=frozenset({enrollment_id}),
        ),
        "system",
    )


def _system_for_target(target: SuppressionTarget) -> Actor:
    return Actor(
        "system:outreach-demo",
        OutreachScope(
            level=ScopeLevel.SYSTEM,
            allowed_suppression_targets=frozenset({target.canonical_id}),
        ),
        "system",
    )


async def _summary(
    factory: async_sessionmaker,
    tenant_id: TenantId,
    campaign_id: CampaignId,
    enrollment_ids: list[str],
    attempt_id: str,
    suppression_id: str,
    stopped_count: int,
) -> dict[str, object]:
    async with factory() as session:
        campaign = await session.get(
            OutreachCampaignRow, (str(tenant_id), str(campaign_id))
        )
        outbox = (
            await session.execute(
                select(OutboxEventRow).where(
                    OutboxEventRow.tenant_id == tenant_id
                )
            )
        ).scalars().all()
    counts = Counter(row.event_type for row in outbox)
    if (
        campaign is None
        or campaign.state != "active"
        or campaign.current_version != 1
        or counts != Counter({"SuppressionAdded": 1})
    ):
        raise RuntimeError("演示持久化摘要不符合预期")
    return {
        "campaign_id": str(campaign_id),
        "campaign_version": campaign.current_version,
        "enrollment_ids": enrollment_ids,
        "message_attempt_id": attempt_id,
        "outbox_counts": dict(sorted(counts.items())),
        "stopped_count": stopped_count,
        "suppression_id": suppression_id,
        "tenant_id": str(tenant_id),
    }


async def _exercise(
    service: OutreachServiceImpl,
    approvals: _DemoApprovals,
    tenant_id: TenantId,
    accounts: tuple[ProspectAccountId, ProspectAccountId],
    contacts: tuple[ContactPointId, ContactPointId],
    senders: tuple[SendingIdentityId, SendingIdentityId],
    factory: async_sessionmaker,
) -> dict[str, object]:
    boss = _boss()
    campaign = await service.create_campaign(
        tenant_id,
        CampaignCreateRequest(
            name="Demo discovery campaign",
            markets=("US",),
            target_entity_types=("importer",),
            allowed_categories=("hardware",),
            sender_identity_ids=senders,
            steps=(
                SequenceStepRequest(1, StepIntent.DISCOVERY, 0),
                SequenceStepRequest(2, StepIntent.FOLLOW_UP, 2),
            ),
            daily_new_contact_limit=5,
            daily_total_message_limit=10,
            handoff_triggers=(),
        ),
        actor=boss,
    )
    await service.submit_campaign(
        tenant_id, campaign.campaign_id, actor=boss
    )
    approver = EmployeeId(new_id("emp"))
    approval = CampaignApprovalSnapshot(
        tenant_id,
        campaign.campaign_id,
        1,
        ApprovalId(new_id("apr")),
        CampaignApprovalState.APPROVED,
        approver,
        _NOW,
    )
    approvals.values[(campaign.campaign_id, 1)] = approval
    await service.activate_campaign(
        tenant_id, campaign.campaign_id, actor=boss
    )

    enrollments = []
    for index, (account, contact) in enumerate(
        zip(accounts, contacts, strict=True), start=1
    ):
        enrollments.append(
            await service.enroll(
                tenant_id,
                campaign.campaign_id,
                EnrollmentCreateRequest(
                    account,
                    contact,
                    IdempotencyKey(f"demo-enrollment-{index}"),
                ),
                actor=boss,
            )
        )
    if len({row.sending_identity_id for row in enrollments}) != 2:
        raise RuntimeError("演示轮询分配不符合预期")

    attempt = await service.prepare_message_attempt(
        tenant_id,
        enrollments[1].enrollment_id,
        actor=_system_for_enrollment(enrollments[1].enrollment_id),
    )
    target = SuppressionTarget(account_id=accounts[0])
    suppressed = await service.add_suppression(
        tenant_id,
        SuppressionRequest(
            target,
            SuppressionReason.UNSUBSCRIBE,
            _NOW,
            "demo_reply_source",
            IdempotencyKey("demo-suppression-1"),
        ),
        actor=_system_for_target(target),
    )
    return await _summary(
        factory,
        tenant_id,
        campaign.campaign_id,
        [str(row.enrollment_id) for row in enrollments],
        str(attempt.attempt_id),
        str(suppressed.suppression.suppression_id),
        suppressed.stopped_count,
    )


async def main() -> None:
    database_url = os.environ["DATABASE_URL"]
    _configure_safe_audit_logging()
    tenant_id = TenantId(new_id("tn"))
    ordered_senders = sorted(
        (
            SendingIdentityId(new_id("sid")),
            SendingIdentityId(new_id("sid")),
        )
    )
    senders = (ordered_senders[0], ordered_senders[1])
    accounts = (
        ProspectAccountId(new_id("acc")),
        ProspectAccountId(new_id("acc")),
    )
    contacts = (
        ContactPointId(new_id("cp")),
        ContactPointId(new_id("cp")),
    )
    contact_snapshots = {
        (contact, account): ContactEligibilitySnapshot(
            tenant_id,
            contact,
            account,
            ContactVerificationStatus.VERIFIED,
            _NOW,
            ContactLegalBasis.LEGITIMATE_INTEREST,
            "demo_contact_basis",
            True,
            "US",
            "importer",
            frozenset({"hardware"}),
            _NOW,
        )
        for contact, account in zip(contacts, accounts, strict=True)
    }
    sender_snapshots = {
        sender: SendingIdentityEligibilitySnapshot(
            tenant_id,
            sender,
            OutreachSenderRole.COLD_OUTREACH,
            True,
            True,
            10,
            _NOW,
        )
        for sender in senders
    }
    reply_snapshots = {
        (contact, account): ReplyStatusSnapshot(
            tenant_id, contact, account, ReplyState.NO_REPLY, None, _NOW
        )
        for contact, account in zip(contacts, accounts, strict=True)
    }
    engine = create_engine_from(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        approvals = _DemoApprovals(tenant_id)
        service = OutreachServiceImpl(
            _UowFactory(factory, lambda: _NOW),
            _DemoContacts(tenant_id, contact_snapshots),
            _DemoSenders(tenant_id, sender_snapshots),
            approvals,
            _DemoReplies(tenant_id, reply_snapshots),
            Phase1OutreachAuthorizer(tenant_id),
            StandardAuditLogger(_AUDIT_LOGGER_NAME),
            now=lambda: _NOW,
        )
        summary = await _exercise(
            service,
            approvals,
            tenant_id,
            accounts,
            contacts,
            senders,
            factory,
        )
    finally:
        await engine.dispose()
    print(
        json.dumps(
            summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _run(entrypoint: Callable[[], Coroutine[Any, Any, None]]) -> int:
    try:
        asyncio.run(entrypoint())
    except Exception:  # noqa: BLE001 - 进程安全边界固定收敛失败输出
        print(_FAILURE_MESSAGE, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_run(main))
