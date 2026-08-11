"""全局抑制通过真实 PostgreSQL 原子停止匹配 Enrollment。"""

from __future__ import annotations

import asyncio
import importlib
from collections import Counter
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.schemas import SuppressionRequest, SuppressionTarget
from shared.schemas.identifiers import (
    ApprovalId,
    CampaignId,
    ContactPointId,
    EnrollmentId,
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

_models = importlib.import_module("domains.outreach.models")
Enrollment = _models.Enrollment
EnrollmentState = _models.EnrollmentState
SuppressionReason = _models.SuppressionReason


@pytest_asyncio.fixture
async def suppression_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = importlib.import_module("infra.db.session").create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _request(
    target: SuppressionTarget,
    *,
    key: str = "suppression-integration-key",
    reason=SuppressionReason.UNSUBSCRIBE,
) -> SuppressionRequest:
    return SuppressionRequest(
        target,
        reason,
        NOW,
        "normalized_reply_event_1",
        IdempotencyKey(key),
    )


async def _seed_enrollment(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    campaign: CampaignId,
    account: ProspectAccountId,
    contact: ContactPointId,
    sender: SendingIdentityId,
    *,
    key: str,
) -> Enrollment:
    uow_type = importlib.import_module(
        "infra.db.outreach_uow"
    ).SqlAlchemyOutreachUnitOfWork
    enrollment = Enrollment(
        tenant,
        EnrollmentId(new_id("enr")),
        campaign,
        1,
        account,
        contact,
        sender,
        EnrollmentState.ENROLLED,
        0,
        NOW,
        NOW,
        None,
        None,
        IdempotencyKey(key),
    )
    async with uow_type(factory, tenant, now=lambda: NOW) as uow:
        result = await uow.enrollments.insert_if_absent(enrollment)
        assert result.winner is not None
    return enrollment


async def _seed_two_campaign_contact_rows(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    *,
    contact: ContactPointId | None = None,
) -> tuple[object, tuple[Enrollment, Enrollment], ContactPointId]:
    sender = SendingIdentityId(new_id("sid"))
    campaigns = (CampaignId(new_id("cmp")), CampaignId(new_id("cmp")))
    approvals = (ApprovalId(new_id("apr")), ApprovalId(new_id("apr")))
    for campaign, approval in zip(campaigns, approvals, strict=True):
        await _seed_active_campaign(
            factory,
            tenant,
            campaign,
            (sender,),
            approval,
            new_contact_limit=20,
            message_limit=20,
        )
    contact = contact or ContactPointId(new_id("cp"))
    rows = (
        await _seed_enrollment(
            factory,
            tenant,
            campaigns[0],
            ProspectAccountId(new_id("acc")),
            contact,
            sender,
            key="cross-campaign-1",
        ),
        await _seed_enrollment(
            factory,
            tenant,
            campaigns[1],
            ProspectAccountId(new_id("acc")),
            contact,
            sender,
            key="cross-campaign-2",
        ),
    )
    service = _service(
        factory,
        tenant,
        campaigns[0],
        approvals[0],
        (sender,),
        {},
        MutableClock(NOW),
    )
    return service, rows, contact


async def test_contact_suppression_stops_cross_campaign_rows_and_is_tenant_isolated(
    suppression_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(suppression_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    other_tenant = TenantId(new_id("tn"))
    service, enrollments, contact = await _seed_two_campaign_contact_rows(
        factory, tenant
    )
    other_service, other_rows, _ = await _seed_two_campaign_contact_rows(
        factory, other_tenant, contact=contact
    )
    del other_service
    boss = Actor("boss:suppression", OutreachScope(level=ScopeLevel.TENANT), "boss")
    result = await service.add_suppression(
        tenant,
        _request(SuppressionTarget(contact_point_id=contact)),
        actor=boss,
    )
    assert result.created is True and result.stopped_count == 2

    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        stored = (
            await session.execute(
                select(rows.OutreachEnrollmentRow)
                .where(rows.OutreachEnrollmentRow.tenant_id == tenant)
                .order_by(rows.OutreachEnrollmentRow.enrollment_id)
            )
        ).scalars().all()
        assert [row.enrollment_id for row in stored] == sorted(
            str(row.enrollment_id) for row in enrollments
        )
        assert {row.state for row in stored} == {"stopped_suppressed"}
        other_stored = (
            await session.execute(
                select(rows.OutreachEnrollmentRow).where(
                    rows.OutreachEnrollmentRow.tenant_id == other_tenant
                )
            )
        ).scalars().all()
        assert {row.enrollment_id for row in other_stored} == {
            str(row.enrollment_id) for row in other_rows
        }
        assert {row.state for row in other_stored} == {"enrolled"}
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutreachActionRow)
            .where(rows.OutreachActionRow.tenant_id == tenant)
        ) == 3
        events = (
            await session.execute(
                select(rows.OutboxEventRow).where(
                    rows.OutboxEventRow.tenant_id == tenant,
                    rows.OutboxEventRow.event_type == "SuppressionAdded",
                )
            )
        ).scalars().all()
        assert len(events) == 1
        assert set(events[0].event_payload) == {
            "tenant_id",
            "occurred_at",
            "run_id",
            "scope",
            "target_id",
            "reason",
        }


async def test_twenty_same_suppression_key_create_one_fact_actions_and_outbox(
    suppression_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(suppression_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    service, enrollments, contact = await _seed_two_campaign_contact_rows(
        factory, tenant
    )
    boss = Actor("boss:suppression", OutreachScope(level=ScopeLevel.TENANT), "boss")
    request = _request(SuppressionTarget(contact_point_id=contact), key="same-key-20")
    results = await asyncio.gather(
        *(service.add_suppression(tenant, request, actor=boss) for _ in range(20))
    )
    assert Counter(result.created for result in results) == {True: 1, False: 19}
    assert Counter(result.stopped_count for result in results) == {2: 1, 0: 19}
    assert len({result.suppression.suppression_id for result in results}) == 1

    rows = importlib.import_module("infra.db.tables")
    async with factory() as session:
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutreachSuppressionRow)
            .where(rows.OutreachSuppressionRow.tenant_id == tenant)
        ) == 1
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutreachActionRow)
            .where(rows.OutreachActionRow.tenant_id == tenant)
        ) == len(enrollments) + 1
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutboxEventRow)
            .where(
                rows.OutboxEventRow.tenant_id == tenant,
                rows.OutboxEventRow.event_type == "SuppressionAdded",
            )
        ) == 1


async def test_suppression_update_and_delete_are_rejected_by_postgres(
    suppression_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(suppression_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    service, _enrollments, contact = await _seed_two_campaign_contact_rows(
        factory, tenant
    )
    boss = Actor("boss:suppression", OutreachScope(level=ScopeLevel.TENANT), "boss")
    result = await service.add_suppression(
        tenant,
        _request(SuppressionTarget(contact_point_id=contact)),
        actor=boss,
    )
    rows = importlib.import_module("infra.db.tables")
    for statement in (
        update(rows.OutreachSuppressionRow)
        .where(
            rows.OutreachSuppressionRow.tenant_id == tenant,
            rows.OutreachSuppressionRow.suppression_id
            == result.suppression.suppression_id,
        )
        .values(source_ref="changed"),
        delete(rows.OutreachSuppressionRow).where(
            rows.OutreachSuppressionRow.tenant_id == tenant,
            rows.OutreachSuppressionRow.suppression_id
            == result.suppression.suppression_id,
        ),
    ):
        async with factory() as session:
            with pytest.raises(DBAPIError) as captured:
                await session.execute(statement)
                await session.commit()
            assert "normalized_reply_event_1" not in str(captured.value)
            await session.rollback()


async def test_commit_failure_rolls_back_suppression_all_rows_actions_and_outbox(
    suppression_engine: AsyncEngine,
) -> None:
    class CommitFailure(RuntimeError):
        pass

    class FailingSession(AsyncSession):
        async def commit(self) -> None:
            raise CommitFailure()

    normal = async_sessionmaker(suppression_engine, expire_on_commit=False)
    failing = async_sessionmaker(
        suppression_engine, expire_on_commit=False, class_=FailingSession
    )
    tenant = TenantId(new_id("tn"))
    _normal_service, enrollments, contact = await _seed_two_campaign_contact_rows(
        normal, tenant
    )
    campaign = enrollments[0].campaign_id
    sender = enrollments[0].sending_identity_id
    service = _service(
        failing,
        tenant,
        campaign,
        ApprovalId(new_id("apr")),
        (sender,),
        {},
        MutableClock(NOW),
    )
    boss = Actor("boss:suppression", OutreachScope(level=ScopeLevel.TENANT), "boss")
    with pytest.raises(CommitFailure):
        await service.add_suppression(
            tenant,
            _request(SuppressionTarget(contact_point_id=contact)),
            actor=boss,
        )

    rows = importlib.import_module("infra.db.tables")
    async with normal() as session:
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutreachSuppressionRow)
            .where(rows.OutreachSuppressionRow.tenant_id == tenant)
        ) == 0
        stored = (
            await session.execute(
                select(rows.OutreachEnrollmentRow).where(
                    rows.OutreachEnrollmentRow.tenant_id == tenant
                )
            )
        ).scalars().all()
        assert {row.state for row in stored} == {"enrolled"}
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutreachActionRow)
            .where(rows.OutreachActionRow.tenant_id == tenant)
        ) == 0
        assert await session.scalar(
            select(func.count())
            .select_from(rows.OutboxEventRow)
            .where(rows.OutboxEventRow.tenant_id == tenant)
        ) == 0
