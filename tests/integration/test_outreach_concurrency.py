"""触达 Enrollment、配额、Attempt 与轮询游标的真实 PostgreSQL 竞争。"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.outreach.errors import (
    AccountAlreadyEnrolledError,
    CampaignQuotaExceededError,
)
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.schemas import (
    ContactEligibilitySnapshot,
    ContactLegalBasis,
    ContactVerificationStatus,
    EnrollmentCreateRequest,
)
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    ContactPointId,
    IdempotencyKey,
    ProspectAccountId,
    SendingIdentityId,
    TenantId,
    new_id,
)
from tests.integration.test_outreach_enrollment_lifecycle import (
    NOW,
    MutableClock,
    _seed_active_campaign,
    _service,
)


@pytest_asyncio.fixture
async def concurrency_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = importlib.import_module("infra.db.session").create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _snapshot(
    tenant: TenantId,
    contact: ContactPointId,
    account: ProspectAccountId,
) -> ContactEligibilitySnapshot:
    return ContactEligibilitySnapshot(
        tenant,
        contact,
        account,
        ContactVerificationStatus.VERIFIED,
        NOW,
        ContactLegalBasis.LEGITIMATE_INTEREST,
        "basis_ref_1",
        True,
        "US",
        "importer",
        frozenset({"hardware"}),
        NOW,
    )


async def _counts(
    factory: async_sessionmaker[AsyncSession], tenant: TenantId, campaign: CampaignId
) -> tuple[int, int, int]:
    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        enrollments = await session.scalar(
            select(func.count())
            .select_from(rows.OutreachEnrollmentRow)
            .where(
                rows.OutreachEnrollmentRow.tenant_id == tenant,
                rows.OutreachEnrollmentRow.campaign_id == campaign,
            )
        )
        attempts = await session.scalar(
            select(func.count())
            .select_from(rows.OutreachMessageAttemptRow)
            .where(
                rows.OutreachMessageAttemptRow.tenant_id == tenant,
                rows.OutreachMessageAttemptRow.campaign_id == campaign,
            )
        )
        messages = await session.scalar(
            select(func.sum(rows.OutreachDailyQuotaRow.messages_reserved)).where(
                rows.OutreachDailyQuotaRow.tenant_id == tenant,
                rows.OutreachDailyQuotaRow.campaign_id == campaign,
            )
        )
        return (
            int(enrollments or 0),
            int(attempts or 0),
            int(messages or 0),
        )


async def test_twenty_same_account_enrollments_leave_exactly_one_active(
    concurrency_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(concurrency_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign = CampaignId(new_id("cmp"))
    approval = ApprovalId(new_id("apr"))
    senders = (SendingIdentityId(new_id("sid")),)
    account = ProspectAccountId(new_id("acc"))
    contacts = [ContactPointId(new_id("cp")) for _ in range(20)]
    snapshots = {(contact, account): _snapshot(tenant, contact, account) for contact in contacts}
    await _seed_active_campaign(factory, tenant, campaign, senders, approval, new_contact_limit=20, message_limit=20)
    service = _service(factory, tenant, campaign, approval, senders, snapshots, MutableClock(NOW))
    boss = Actor("boss:concurrency", OutreachScope(level=ScopeLevel.TENANT), "boss")

    async def attempt(index: int):
        try:
            return await service.enroll(
                tenant,
                campaign,
                EnrollmentCreateRequest(account, contacts[index], IdempotencyKey(f"same-account-{index}")),
                actor=boss,
            )
        except AccountAlreadyEnrolledError:
            return None

    results = await asyncio.gather(*(attempt(index) for index in range(20)))
    assert sum(result is not None for result in results) == 1
    enrollments, attempts, _messages = await _counts(factory, tenant, campaign)
    assert (enrollments, attempts) == (1, 0)
    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        quota = await session.get(rows.OutreachDailyQuotaRow, (str(tenant), str(campaign), NOW.date()))
        assert quota is not None and quota.new_contacts_reserved == 1


async def test_twenty_distinct_accounts_never_exceed_new_contact_cap_five(
    concurrency_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(concurrency_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign = CampaignId(new_id("cmp"))
    approval = ApprovalId(new_id("apr"))
    senders = (SendingIdentityId(new_id("sid")),)
    targets = [
        (ContactPointId(new_id("cp")), ProspectAccountId(new_id("acc")))
        for _ in range(20)
    ]
    snapshots = {target: _snapshot(tenant, *target) for target in targets}
    await _seed_active_campaign(factory, tenant, campaign, senders, approval, new_contact_limit=5, message_limit=20)
    service = _service(factory, tenant, campaign, approval, senders, snapshots, MutableClock(NOW))
    boss = Actor("boss:concurrency", OutreachScope(level=ScopeLevel.TENANT), "boss")

    async def attempt(index: int):
        contact, account = targets[index]
        try:
            return await service.enroll(
                tenant,
                campaign,
                EnrollmentCreateRequest(account, contact, IdempotencyKey(f"cap-{index}")),
                actor=boss,
            )
        except CampaignQuotaExceededError:
            return None

    results = await asyncio.gather(*(attempt(index) for index in range(20)))
    assert sum(result is not None for result in results) == 5
    enrollments, attempts, _messages = await _counts(factory, tenant, campaign)
    assert (enrollments, attempts) == (5, 0)


async def test_twenty_attempts_never_exceed_message_cap_seven(
    concurrency_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(concurrency_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign = CampaignId(new_id("cmp"))
    approval = ApprovalId(new_id("apr"))
    senders = (SendingIdentityId(new_id("sid")),)
    targets = [
        (ContactPointId(new_id("cp")), ProspectAccountId(new_id("acc")))
        for _ in range(20)
    ]
    snapshots = {target: _snapshot(tenant, *target) for target in targets}
    await _seed_active_campaign(factory, tenant, campaign, senders, approval, new_contact_limit=7, message_limit=7)
    clock = MutableClock(NOW)
    service = _service(factory, tenant, campaign, approval, senders, snapshots, clock)
    boss = Actor("boss:concurrency", OutreachScope(level=ScopeLevel.TENANT), "boss")
    enrollments = []
    for start, stop in ((0, 7), (7, 14), (14, 20)):
        enrollments.extend(
            await asyncio.gather(
                *(
                    service.enroll(
                        tenant,
                        campaign,
                        EnrollmentCreateRequest(
                            targets[index][1],
                            targets[index][0],
                            IdempotencyKey(f"attempt-cap-{index}"),
                        ),
                        actor=boss,
                    )
                    for index in range(start, stop)
                )
            )
        )
        clock.value += timedelta(days=1)

    async def prepare(enrollment):
        actor = Actor(
            "system:concurrency",
            OutreachScope(
                level=ScopeLevel.SYSTEM,
                allowed_enrollment_ids=frozenset({enrollment.enrollment_id}),
            ),
            "system",
        )
        try:
            return await service.prepare_message_attempt(tenant, enrollment.enrollment_id, actor=actor)
        except CampaignQuotaExceededError:
            return None

    results = await asyncio.gather(*(prepare(enrollment) for enrollment in enrollments))
    assert sum(result is not None for result in results) == 7
    durable_enrollments, attempts, messages = await _counts(factory, tenant, campaign)
    assert (durable_enrollments, attempts, messages) == (20, 7, 7)


async def test_same_attempt_key_twenty_times_reserves_message_quota_once(
    concurrency_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(concurrency_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign = CampaignId(new_id("cmp"))
    approval = ApprovalId(new_id("apr"))
    senders = (SendingIdentityId(new_id("sid")),)
    account = ProspectAccountId(new_id("acc"))
    contact = ContactPointId(new_id("cp"))
    snapshots = {(contact, account): _snapshot(tenant, contact, account)}
    await _seed_active_campaign(factory, tenant, campaign, senders, approval, new_contact_limit=2, message_limit=20)
    service = _service(factory, tenant, campaign, approval, senders, snapshots, MutableClock(NOW))
    boss = Actor("boss:concurrency", OutreachScope(level=ScopeLevel.TENANT), "boss")
    enrollment = await service.enroll(
        tenant,
        campaign,
        EnrollmentCreateRequest(account, contact, IdempotencyKey("attempt-same-enrollment")),
        actor=boss,
    )
    system = Actor(
        "system:concurrency",
        OutreachScope(
            level=ScopeLevel.SYSTEM,
            allowed_enrollment_ids=frozenset({enrollment.enrollment_id}),
        ),
        "system",
    )
    results = await asyncio.gather(
        *(service.prepare_message_attempt(tenant, enrollment.enrollment_id, actor=system) for _ in range(20))
    )
    assert len({result.attempt_id for result in results}) == 1
    _enrollments, attempts, messages = await _counts(factory, tenant, campaign)
    assert (attempts, messages) == (1, 1)


async def test_two_concurrent_cursor_allocations_choose_distinct_canonical_senders(
    concurrency_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(concurrency_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaign = CampaignId(new_id("cmp"))
    approval = ApprovalId(new_id("apr"))
    senders = tuple(sorted((SendingIdentityId(new_id("sid")), SendingIdentityId(new_id("sid")))))
    targets = [
        (ContactPointId(new_id("cp")), ProspectAccountId(new_id("acc")))
        for _ in range(2)
    ]
    snapshots = {target: _snapshot(tenant, *target) for target in targets}
    await _seed_active_campaign(factory, tenant, campaign, senders, approval)
    service = _service(factory, tenant, campaign, approval, senders, snapshots, MutableClock(NOW))
    boss = Actor("boss:concurrency", OutreachScope(level=ScopeLevel.TENANT), "boss")
    results = await asyncio.gather(
        *(
            service.enroll(
                tenant,
                campaign,
                EnrollmentCreateRequest(account, contact, IdempotencyKey(f"cursor-{index}")),
                actor=boss,
            )
            for index, (contact, account) in enumerate(targets)
        )
    )
    assert {result.sending_identity_id for result in results} == set(senders)


async def test_two_campaigns_competing_for_one_account_commit_one_winner(
    concurrency_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(concurrency_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    campaigns = (CampaignId(new_id("cmp")), CampaignId(new_id("cmp")))
    approvals = (ApprovalId(new_id("apr")), ApprovalId(new_id("apr")))
    sender = SendingIdentityId(new_id("sid"))
    account = ProspectAccountId(new_id("acc"))
    contacts = (ContactPointId(new_id("cp")), ContactPointId(new_id("cp")))
    snapshots = {
        (contact, account): _snapshot(tenant, contact, account)
        for contact in contacts
    }
    for campaign, approval in zip(campaigns, approvals, strict=True):
        await _seed_active_campaign(
            factory,
            tenant,
            campaign,
            (sender,),
            approval,
            new_contact_limit=5,
            message_limit=5,
        )
    services = tuple(
        _service(
            factory,
            tenant,
            campaign,
            approval,
            (sender,),
            snapshots,
            MutableClock(NOW),
        )
        for campaign, approval in zip(campaigns, approvals, strict=True)
    )
    boss = Actor("boss:concurrency", OutreachScope(level=ScopeLevel.TENANT), "boss")

    async def enroll(index: int):
        try:
            return await services[index].enroll(
                tenant,
                campaigns[index],
                EnrollmentCreateRequest(
                    account,
                    contacts[index],
                    IdempotencyKey(f"cross-campaign-{index}"),
                ),
                actor=boss,
            )
        except AccountAlreadyEnrolledError:
            return None

    results = await asyncio.gather(enroll(0), enroll(1))
    assert sum(result is not None for result in results) == 1
    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutreachEnrollmentRow)
            .where(rows.OutreachEnrollmentRow.tenant_id == tenant)
        ) == 1
        assert await session.scalar(
            select(func.sum(rows.OutreachDailyQuotaRow.new_contacts_reserved)).where(
                rows.OutreachDailyQuotaRow.tenant_id == tenant
            )
        ) == 1
