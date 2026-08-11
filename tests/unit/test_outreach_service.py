"""Campaign 生命周期服务的授权顺序、审批版本与不可变版本行为。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest

from domains.outreach.errors import (
    CampaignApprovalRequiredError,
    SendingIdentityUnavailableError,
)
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.schemas import (
    CampaignApprovalSnapshot,
    CampaignApprovalState,
    CampaignCreateRequest,
    OutreachSenderRole,
    SendingIdentityEligibilitySnapshot,
    SequenceStepRequest,
)
from shared.errors import PermissionDenied, TenantIsolationViolation
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
    FakeAuthorizer,
    FakeSenders,
    FakeStore,
    FakeUowFactory,
    Trace,
    UnusedProvider,
    denied_authorizer,
)

_models = importlib.import_module("domains.outreach.models")
CampaignState = _models.CampaignState
StepIntent = _models.StepIntent

NOW = datetime(2026, 8, 11, 6, 0, tzinfo=UTC)
TENANT = TenantId(new_id("tn"))
SENDER = SendingIdentityId(new_id("sid"))
BOSS = Actor("boss:test", OutreachScope(level=ScopeLevel.TENANT), "boss")


def _request(name: str = "Hardware discovery") -> CampaignCreateRequest:
    return CampaignCreateRequest(
        name=name,
        markets=("US",),
        target_entity_types=("importer",),
        allowed_categories=("hardware",),
        sender_identity_ids=(SENDER,),
        steps=(SequenceStepRequest(1, StepIntent.DISCOVERY, 0),),
        daily_new_contact_limit=2,
        daily_total_message_limit=3,
        handoff_triggers=("quote_requested",),
    )


def _sender(*, role: OutreachSenderRole = OutreachSenderRole.COLD_OUTREACH, authenticated: bool = True):
    return SendingIdentityEligibilitySnapshot(
        tenant_id=TENANT,
        identity_id=SENDER,
        role=role,
        authentication_passed=authenticated,
        sendable=True,
        remaining_slots=0,
        observed_at=NOW,
    )


def _service(*, denied: bool = False):
    try:
        service_type = importlib.import_module(
            "domains.outreach.service_impl"
        ).OutreachServiceImpl
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"缺少 OutreachServiceImpl: {exc}")
    trace = Trace()
    store = FakeStore()
    approvals = FakeApprovals(trace)
    audit = FakeAudit(trace)
    authorizer = denied_authorizer(trace) if denied else FakeAuthorizer(trace)
    service = service_type(
        FakeUowFactory(store, trace),
        UnusedProvider(),
        FakeSenders({SENDER: _sender()}, trace),
        approvals,
        UnusedProvider(),
        authorizer,
        audit,
        now=lambda: NOW,
    )
    return service, trace, store, approvals, audit


@pytest.mark.parametrize(
    ("method", "action"),
    [
        ("create_campaign", "campaign:create"),
        ("submit_campaign", "campaign:submit"),
        ("revise_campaign", "campaign:revise"),
        ("activate_campaign", "campaign:activate"),
        ("pause_campaign", "campaign:pause"),
        ("cancel_campaign", "campaign:cancel"),
        ("get_campaign", "campaign:read"),
        ("list_campaigns", "campaign:list"),
    ],
)
async def test_campaign_methods_preauthorize_before_clock_provider_or_uow(
    method: str, action: str
) -> None:
    service, trace, _, _, audit = _service(denied=True)
    campaign_id = new_id("cmp")
    calls = {
        "create_campaign": lambda: service.create_campaign(TENANT, _request(), actor=BOSS),
        "submit_campaign": lambda: service.submit_campaign(TENANT, campaign_id, actor=BOSS),
        "revise_campaign": lambda: service.revise_campaign(TENANT, campaign_id, _request(), actor=BOSS),
        "activate_campaign": lambda: service.activate_campaign(TENANT, campaign_id, actor=BOSS),
        "pause_campaign": lambda: service.pause_campaign(TENANT, campaign_id, "manual", actor=BOSS),
        "cancel_campaign": lambda: service.cancel_campaign(TENANT, campaign_id, actor=BOSS),
        "get_campaign": lambda: service.get_campaign(TENANT, campaign_id, actor=BOSS),
        "list_campaigns": lambda: service.list_campaigns(
            TENANT, BOSS.scope, limit=10, actor=BOSS
        ),
    }
    with pytest.raises(PermissionDenied):
        await calls[method]()
    assert trace.calls == [
        ("preauthorize", action),
        ("audit", action, "deny:authorization"),
    ]
    assert [record["rule"] for record in audit.records] == ["deny:authorization"]


async def test_campaign_lifecycle_binds_exact_approval_and_preserves_v1() -> None:
    service, trace, store, approvals, audit = _service()
    created = await service.create_campaign(TENANT, _request(), actor=BOSS)
    assert created.state is CampaignState.DRAFT
    assert created.today_new_contacts_reserved == 0
    assert created.today_messages_reserved == 0
    pending = await service.submit_campaign(TENANT, created.campaign_id, actor=BOSS)
    assert pending.state is CampaignState.PENDING_APPROVAL
    approval = CampaignApprovalSnapshot(
        tenant_id=TENANT,
        campaign_id=created.campaign_id,
        version=1,
        approval_id=ApprovalId(new_id("apr")),
        state=CampaignApprovalState.APPROVED,
        approved_by=EmployeeId(new_id("emp")),
        approved_at=NOW,
    )
    approvals.values[(created.campaign_id, 1)] = approval
    active = await service.activate_campaign(TENANT, created.campaign_id, actor=BOSS)
    assert (active.state, active.approval_id) == (CampaignState.ACTIVE, approval.approval_id)
    frozen_v1 = store.versions[(created.campaign_id, 1)]
    revised = await service.revise_campaign(
        TENANT, created.campaign_id, _request("Hardware discovery v2"), actor=BOSS
    )
    assert (revised.state, revised.version, revised.approval_id) == (
        CampaignState.PENDING_APPROVAL,
        2,
        None,
    )
    assert store.versions[(created.campaign_id, 1)] == frozen_v1
    with pytest.raises(CampaignApprovalRequiredError):
        await service.activate_campaign(TENANT, created.campaign_id, actor=BOSS)
    assert sum(record["rule"].startswith("allow:") for record in audit.records) == 4
    assert trace.calls[-1][0] == "uow_exit"


@pytest.mark.parametrize(
    "snapshot",
    [
        _sender(role=OutreachSenderRole.PRIMARY_BUSINESS),
        _sender(authenticated=False),
        SendingIdentityEligibilitySnapshot(
            tenant_id=TenantId(new_id("tn")),
            identity_id=SENDER,
            role=OutreachSenderRole.COLD_OUTREACH,
            authentication_passed=True,
            sendable=True,
            remaining_slots=1,
            observed_at=NOW,
        ),
    ],
)
async def test_create_rejects_sender_snapshot_mismatch_without_uow(
    snapshot, caplog: pytest.LogCaptureFixture
) -> None:
    service, trace, store, _, audit = _service()
    service._senders = FakeSenders({SENDER: snapshot}, trace)
    expected = (
        TenantIsolationViolation
        if snapshot.tenant_id != TENANT
        else SendingIdentityUnavailableError
    )
    with (
        caplog.at_level("CRITICAL", logger="security.tenant_isolation"),
        pytest.raises(expected),
    ):
        await service.create_campaign(TENANT, _request(), actor=BOSS)
    assert store.campaigns == {}
    assert all(call[0] != "uow_enter" for call in trace.calls)
    assert all(record["rule"] != "allow:campaign:create" for record in audit.records)
    if expected is TenantIsolationViolation:
        records = [record for record in caplog.records if record.name == "security.tenant_isolation"]
        assert [(record.levelname, record.message) for record in records] == [
            ("CRITICAL", "检测到跨租户数据隔离违规")
        ]
        assert str(SENDER) not in caplog.text
