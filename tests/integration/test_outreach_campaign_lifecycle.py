"""Campaign 生命周期通过真实 PostgreSQL UoW 持久化。"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from domains.outreach.permissions import (
    Actor,
    OutreachScope,
    Phase1OutreachAuthorizer,
    ScopeLevel,
)
from domains.outreach.schemas import (
    CampaignApprovalSnapshot,
    CampaignApprovalState,
    CampaignCreateRequest,
    OutreachSenderRole,
    SendingIdentityEligibilitySnapshot,
    SequenceStepRequest,
)
from shared.schemas.identifiers import (
    ApprovalId,
    EmployeeId,
    SendingIdentityId,
    TenantId,
    new_id,
)
from tests.outreach_fakes import (
    FakeApprovals,
    FakeAudit,
    FakeSenders,
    Trace,
    UnusedProvider,
)

_models = importlib.import_module("domains.outreach.models")
CampaignState = _models.CampaignState
StepIntent = _models.StepIntent

NOW = datetime(2026, 8, 11, 7, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def lifecycle_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    engine = importlib.import_module("infra.db.session").create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_exact_version_approval_and_revision_are_durable(
    lifecycle_engine: AsyncEngine,
) -> None:
    try:
        service_type = importlib.import_module(
            "domains.outreach.service_impl"
        ).OutreachServiceImpl
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"缺少 OutreachServiceImpl: {exc}")
    uow_type = importlib.import_module("infra.db.outreach_uow").SqlAlchemyOutreachUnitOfWork
    rows = importlib.import_module("infra.db.tables")
    tenant = TenantId(new_id("tn"))
    sender_id = SendingIdentityId(new_id("sid"))
    actor = Actor("boss:integration", OutreachScope(level=ScopeLevel.TENANT), "boss")
    factory = async_sessionmaker(lifecycle_engine, expire_on_commit=False)
    trace = Trace()
    approvals = FakeApprovals(trace)
    senders = FakeSenders(
        {
            sender_id: SendingIdentityEligibilitySnapshot(
                tenant_id=tenant,
                identity_id=sender_id,
                role=OutreachSenderRole.COLD_OUTREACH,
                authentication_passed=True,
                sendable=True,
                remaining_slots=0,
                observed_at=NOW,
            )
        },
        trace,
    )
    service = service_type(
        lambda requested: uow_type(factory, requested, now=lambda: NOW),
        UnusedProvider(),
        senders,
        approvals,
        UnusedProvider(),
        Phase1OutreachAuthorizer(tenant),
        FakeAudit(trace),
        now=lambda: NOW,
    )

    def request(name: str) -> CampaignCreateRequest:
        return CampaignCreateRequest(
            name=name,
            markets=("US",),
            target_entity_types=("importer",),
            allowed_categories=("hardware",),
            sender_identity_ids=(sender_id,),
            steps=(SequenceStepRequest(1, StepIntent.DISCOVERY, 0),),
            daily_new_contact_limit=2,
            daily_total_message_limit=3,
            handoff_triggers=(),
        )

    created = await service.create_campaign(tenant, request("Discovery v1"), actor=actor)
    await service.submit_campaign(tenant, created.campaign_id, actor=actor)
    approval = CampaignApprovalSnapshot(
        tenant_id=tenant,
        campaign_id=created.campaign_id,
        version=1,
        approval_id=ApprovalId(new_id("apr")),
        state=CampaignApprovalState.APPROVED,
        approved_by=EmployeeId(new_id("emp")),
        approved_at=NOW,
    )
    approvals.values[(created.campaign_id, 1)] = approval
    active = await service.activate_campaign(tenant, created.campaign_id, actor=actor)
    revised = await service.revise_campaign(
        tenant, created.campaign_id, request("Discovery v2"), actor=actor
    )
    assert (active.state, revised.state, revised.version) == (
        CampaignState.ACTIVE,
        CampaignState.PENDING_APPROVAL,
        2,
    )
    async with factory() as session:
        campaign = await session.get(rows.OutreachCampaignRow, (str(tenant), str(created.campaign_id)))
        versions = (
            await session.execute(
                select(rows.OutreachCampaignVersionRow)
                .where(rows.OutreachCampaignVersionRow.tenant_id == tenant)
                .order_by(rows.OutreachCampaignVersionRow.version)
            )
        ).scalars().all()
        assert campaign is not None
        assert (campaign.state, campaign.current_version, campaign.approval_id) == (
            "pending_approval",
            2,
            None,
        )
        assert [(row.version, row.name) for row in versions] == [
            (1, "Discovery v1"),
            (2, "Discovery v2"),
        ]
