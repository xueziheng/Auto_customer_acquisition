"""Sourcing 主供给到 ESTIMATED 成本表的权限、门禁与幂等契约。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from domains.opportunities.permissions import Actor, OpportunityScope, ScopeLevel
from domains.opportunities.schemas import OpportunityView
from domains.sourcing.permissions import SourcingActor, SourcingScope
from domains.sourcing.schemas import SourcingCostPriceOption, SourcingHandoffSnapshot
from domains.sourcing.service import SourcingReview
from shared.errors import PermissionDenied, TransientError, ValidationError
from shared.events.catalog import SourcingCaseHandedToCosting
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    OpportunityId,
    ProductId,
    RunId,
    SourcingCaseId,
    SourcingReviewId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
    TenantId,
    ValidatedNeedId,
)
from workflows.engine.runner import StepStatus, WorkflowRun
from workflows.sourcing_case.application import SourcingCaseApplication

NOW = datetime(2026, 8, 31, 15, tzinfo=UTC)
TENANT = TenantId("tenant-sourcing-cost")
OTHER_TENANT = TenantId("tenant-sourcing-cost-other")
CASE_ID = SourcingCaseId("src-cost")
REVIEW_ID = SourcingReviewId("srv-cost")
NEED_ID = ValidatedNeedId("need-cost")
OPPORTUNITY_ID = OpportunityId("opp-cost")
PRIMARY_OPTION_ID = SourcingSupplyOptionId("sop-primary")
ALTERNATE_OPTION_ID = SourcingSupplyOptionId("sop-alternate")
PRODUCT_ID = ProductId("prd-primary")
CANDIDATE_ID = SupplierCandidateId("spc-primary")
EVIDENCE_ID = ArtifactId("art_primary_price")
SYSTEM_SOURCING_ACTOR = SourcingActor(
    "system:sourcing-cost", TENANT, SourcingScope.SYSTEM, "system"
)
SYSTEM_OPPORTUNITY_ACTOR = Actor(
    "system:sourcing-cost",
    OpportunityScope(level=ScopeLevel.SYSTEM),
    role="system",
)


def _symbol(module_name: str, symbol_name: str) -> Any:
    module = importlib.import_module(module_name)
    if not hasattr(module, symbol_name):
        pytest.fail(f"{module_name}.{symbol_name} 尚未实现")
    return getattr(module, symbol_name)


def _run(*, tenant_id: TenantId = TENANT) -> WorkflowRun:
    return WorkflowRun(
        run_id=RunId("run-sourcing-cost"),
        tenant_id=tenant_id,
        workflow_type="sourcing_case",
        workflow_version=2,
        subject_ref=str(CASE_ID),
        current_step="handoff_costing",
        status=StepStatus.RUNNING,
        created_at=NOW,
        context={
            "case_id": str(CASE_ID),
            "need_id": str(NEED_ID),
            "need_snapshot_hash": "a" * 64,
        },
    )


def _opportunity(*, need_id: str = str(NEED_ID)) -> OpportunityView:
    return OpportunityView(
        opportunity_id=str(OPPORTUNITY_ID),
        account_id="acc-cost",
        account_name="Cost Buyer",
        country="US",
        need_id=need_id,
        product_category="hardware",
        state="sourcing",
        created_at=NOW,
    )


class _Opportunities:
    def __init__(self, result: OpportunityView | None) -> None:
        self.result = result
        self.calls: list[tuple[Any, ...]] = []

    async def get_by_need(self, tenant_id, need_id, *, actor):
        self.calls.append((tenant_id, need_id, actor))
        return self.result


class _HandoffSourcing:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def hand_to_costing(self, tenant_id, case_id, opportunity_id, *, actor):
        self.calls.append((tenant_id, case_id, opportunity_id, actor))
        return _snapshot()


@pytest.mark.asyncio
async def test_handoff_step_requires_exact_need_opportunity_before_sourcing_call() -> None:
    """移除 Opportunity 门禁会让游离成本表重新出现。"""

    step_type = _symbol("workflows.sourcing_case.steps", "HandoffCostingStep")
    opportunities = _Opportunities(None)
    sourcing = _HandoffSourcing()
    step = step_type(
        opportunities=opportunities,
        sourcing=sourcing,
        opportunity_actor=SYSTEM_OPPORTUNITY_ACTOR,
        sourcing_actor=SYSTEM_SOURCING_ACTOR,
    )

    action, next_step, patch = await step.execute(_run())

    assert (action, next_step) == ("wait", None)
    assert patch == {"sourcing_stop_reason": "opportunity_required"}
    assert opportunities.calls == [(TENANT, NEED_ID, SYSTEM_OPPORTUNITY_ACTOR)]
    assert sourcing.calls == []


@pytest.mark.asyncio
async def test_handoff_step_rejects_wrong_need_projection_and_hands_off_exact_match() -> None:
    """按企业或近似 Need 返回的机会不能满足精确绑定。"""

    step_type = _symbol("workflows.sourcing_case.steps", "HandoffCostingStep")
    sourcing = _HandoffSourcing()
    wrong = step_type(
        opportunities=_Opportunities(_opportunity(need_id="need-other")),
        sourcing=sourcing,
        opportunity_actor=SYSTEM_OPPORTUNITY_ACTOR,
        sourcing_actor=SYSTEM_SOURCING_ACTOR,
    )
    with pytest.raises(ValidationError, match="Opportunity"):
        await wrong.execute(_run())
    assert sourcing.calls == []

    exact = step_type(
        opportunities=_Opportunities(_opportunity()),
        sourcing=sourcing,
        opportunity_actor=SYSTEM_OPPORTUNITY_ACTOR,
        sourcing_actor=SYSTEM_SOURCING_ACTOR,
    )
    assert await exact.execute(_run()) == (
        "complete",
        None,
        {
            "sourcing_stop_reason": None,
            "opportunity_id": str(OPPORTUNITY_ID),
            "review_id": str(REVIEW_ID),
        },
    )
    assert sourcing.calls == [
        (TENANT, CASE_ID, OPPORTUNITY_ID, SYSTEM_SOURCING_ACTOR)
    ]


def _snapshot(
    *,
    quantity: int = 500,
    moq: int = 100,
    opportunity_id: OpportunityId = OPPORTUNITY_ID,
) -> SourcingHandoffSnapshot:
    return SourcingHandoffSnapshot(
        case_id=CASE_ID,
        review_id=REVIEW_ID,
        need_id=NEED_ID,
        opportunity_id=opportunity_id,
        primary_option_id=PRIMARY_OPTION_ID,
        product_id=PRODUCT_ID,
        supplier_candidate_id=CANDIDATE_ID,
        quantity=quantity,
        moq=moq,
        price_options=(
            SourcingCostPriceOption(
                minimum_quantity=100,
                unit_amount=Decimal("1.234567890123"),
                currency="USD",
                unit="piece",
                evidence_ref=EVIDENCE_ID,
                source_kind="supplier_candidate",
            ),
            SourcingCostPriceOption(
                minimum_quantity=500,
                unit_amount=Decimal("1.100000000001"),
                currency="USD",
                unit="piece",
                evidence_ref=ArtifactId("art_primary_price_500"),
                source_kind="supplier_candidate",
            ),
            SourcingCostPriceOption(
                minimum_quantity=1000,
                unit_amount=Decimal("0.990000000001"),
                currency="USD",
                unit="piece",
                evidence_ref=ArtifactId("art_primary_price_1000"),
                source_kind="supplier_candidate",
            ),
        ),
    )


def _handoff_event(*, tenant_id: TenantId = TENANT) -> SourcingCaseHandedToCosting:
    return SourcingCaseHandedToCosting(
        tenant_id=tenant_id,
        occurred_at=NOW,
        case_id=CASE_ID,
        need_id=NEED_ID,
        opportunity_id=OPPORTUNITY_ID,
        review_id=REVIEW_ID,
    )


class _SnapshotReader:
    def __init__(self, snapshot: SourcingHandoffSnapshot | None = None) -> None:
        self.snapshot = snapshot or _snapshot()
        self.calls: list[tuple[Any, ...]] = []
        self.error: Exception | None = None

    async def get_handoff_snapshot(self, tenant_id, actor, case_id, review_id):
        self.calls.append((tenant_id, actor, case_id, review_id))
        if self.error is not None:
            raise self.error
        return self.snapshot


class _EstimateCosting:
    def __init__(self) -> None:
        self.commands: dict[str, Any] = {}
        self.calls: list[tuple[Any, ...]] = []
        self.error: Exception | None = None

    async def create_sourcing_estimate(self, tenant_id, command, *, actor):
        self.calls.append((tenant_id, command, actor))
        if self.error is not None:
            raise self.error
        self.commands.setdefault(str(command.sourcing_case_id), command)
        return "cost-estimated"


@pytest.mark.asyncio
async def test_handler_uses_highest_applicable_primary_tier_with_decimal_precision() -> None:
    """错误取最低价或备用候选会直接污染内部成本判断。"""

    handler_type = _symbol(
        "apps.scheduler_worker.sourcing_costing", "SourcingCostHandoffHandler"
    )
    costing_permissions = importlib.import_module("domains.costing.permissions")
    reader = _SnapshotReader()
    costing = _EstimateCosting()
    costing_actor = costing_permissions.CostingActor(
        actor_id="system:sourcing-cost",
        role="system",
        scope=costing_permissions.CostingScope.SYSTEM,
        tenant_id=TENANT,
    )
    handler = handler_type(
        sourcing=reader,
        costing=costing,
        tenant_id=TENANT,
        sourcing_actor=SourcingActor(
            "employee-finance", TENANT, SourcingScope.TENANT, "finance"
        ),
        costing_actor=costing_actor,
    )

    await handler.handle(_handoff_event())
    await handler.handle(_handoff_event())

    assert len(costing.commands) == 1
    command = costing.commands[str(CASE_ID)]
    assert command.primary_option_id == PRIMARY_OPTION_ID
    assert command.product_id == PRODUCT_ID
    assert command.supplier_candidate_id == CANDIDATE_ID
    assert command.quantity == 500
    assert command.unit_amount == Decimal("1.100000000001")
    assert command.currency == "USD"
    assert command.evidence_ref == ArtifactId("art_primary_price_500")
    assert len(costing.calls) == 2


@pytest.mark.asyncio
async def test_handler_rejects_moq_and_cross_tenant_without_costing() -> None:
    """MOQ 不适用或跨租户事件都不能形成估算成本。"""

    handler_type = _symbol(
        "apps.scheduler_worker.sourcing_costing", "SourcingCostHandoffHandler"
    )
    costing_permissions = importlib.import_module("domains.costing.permissions")
    reader = _SnapshotReader(_snapshot(quantity=50, moq=100))
    costing = _EstimateCosting()
    handler = handler_type(
        sourcing=reader,
        costing=costing,
        tenant_id=TENANT,
        sourcing_actor=SourcingActor(
            "employee-finance", TENANT, SourcingScope.TENANT, "finance"
        ),
        costing_actor=costing_permissions.CostingActor(
            "system:sourcing-cost",
            "system",
            costing_permissions.CostingScope.SYSTEM,
            TENANT,
        ),
    )

    await handler.handle(_handoff_event(tenant_id=OTHER_TENANT))
    with pytest.raises(ValidationError, match="成本交接快照"):
        await handler.handle(_handoff_event())
    reader.snapshot = _snapshot(quantity=50, moq=1)
    with pytest.raises(ValidationError, match="适用数量档"):
        await handler.handle(_handoff_event())
    assert len(reader.calls) == 2
    assert costing.calls == []


@pytest.mark.asyncio
async def test_handler_detaches_raw_storage_error_as_retryable() -> None:
    """原始 DSN/凭证文本不能进入 Outbox 错误，存储故障仍须可重试。"""

    handler_type = _symbol(
        "apps.scheduler_worker.sourcing_costing", "SourcingCostHandoffHandler"
    )
    costing_permissions = importlib.import_module("domains.costing.permissions")
    reader = _SnapshotReader()
    reader.error = RuntimeError("postgres://user:secret@db/private token=raw-secret")
    handler = handler_type(
        sourcing=reader,
        costing=_EstimateCosting(),
        tenant_id=TENANT,
        sourcing_actor=SourcingActor(
            "employee-finance", TENANT, SourcingScope.TENANT, "finance"
        ),
        costing_actor=costing_permissions.CostingActor(
            "system:sourcing-cost",
            "system",
            costing_permissions.CostingScope.SYSTEM,
            TENANT,
        ),
    )

    with pytest.raises(TransientError, match="交接快照暂不可用") as caught:
        await handler.handle(_handoff_event())
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "secret" not in str(caught.value)

    reader.error = None
    costing = _EstimateCosting()
    costing.error = RuntimeError("postgres://user:secret@db/private")
    costing_handler = handler_type(
        sourcing=reader,
        costing=costing,
        tenant_id=TENANT,
        sourcing_actor=SourcingActor(
            "employee-finance", TENANT, SourcingScope.TENANT, "finance"
        ),
        costing_actor=costing_permissions.CostingActor(
            "system:sourcing-cost",
            "system",
            costing_permissions.CostingScope.SYSTEM,
            TENANT,
        ),
    )
    with pytest.raises(TransientError, match="估算成本创建暂不可用") as cost_error:
        await costing_handler.handle(_handoff_event())
    assert cost_error.value.__cause__ is None
    assert cost_error.value.__context__ is None
    assert "secret" not in str(cost_error.value)


class _PendingReviewSourcing:
    def __init__(self, review: SourcingReview) -> None:
        self.review_fact = review

    async def review(self, tenant_id, case_id, command, *, actor):
        return self.review_fact


class _EngineMustNotWake:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"pending review 不得访问 Workflow Engine: {name}")


class _UnusedQuota:
    pass


@pytest.mark.asyncio
async def test_product_review_stays_pending_and_never_wakes_workflow() -> None:
    """把提交等同确认会抹掉老板门禁。"""

    command_type = _symbol("domains.sourcing.schemas", "SourcingReviewCommand")
    command = command_type(
        primary_option_id=PRIMARY_OPTION_ID,
        alternate_option_ids=(ALTERNATE_OPTION_ID,),
        reason="证据最完整",
        expected_case_version=8,
    )
    pending = SourcingReview(
        review_id=REVIEW_ID,
        tenant_id=TENANT,
        case_id=CASE_ID,
        primary_option_id=PRIMARY_OPTION_ID,
        alternate_option_ids=(ALTERNATE_OPTION_ID,),
        reason="证据最完整",
        expected_case_version=8,
        submitted_by=EmployeeId("employee-product"),
        submitted_at=NOW,
    )
    application = SourcingCaseApplication(
        sourcing=_PendingReviewSourcing(pending),
        quota=_UnusedQuota(),
        engine=_EngineMustNotWake(),
    )

    result = await application.review(
        TENANT,
        CASE_ID,
        command,
        request_id="review-pending",
        actor=SourcingActor(
            "employee-product", TENANT, SourcingScope.TENANT, "product"
        ),
    )

    assert result == pending


def test_costing_system_action_is_tenant_bound_and_http_roles_are_denied() -> None:
    """若普通 HTTP 角色可调用自动成本动作，调用方可伪造 indicative 成本。"""

    permissions = importlib.import_module("domains.costing.permissions")
    action = permissions.CostingAction.SOURCING_ESTIMATE_CREATE
    scope = permissions.CostingScope.SYSTEM
    authorizer = permissions.Phase1CostingAuthorizer(TENANT)
    system = permissions.CostingActor(
        "system:sourcing-cost", "system", scope, TENANT
    )
    assert authorizer.require(system, action, TENANT).endswith(action.value)
    with pytest.raises(PermissionDenied):
        authorizer.require(system, action, OTHER_TENANT)
    with pytest.raises(PermissionDenied):
        authorizer.require(
            permissions.CostingActor(
                "employee-finance", "finance", permissions.CostingScope.TENANT
            ),
            action,
            TENANT,
        )


def test_sourcing_estimate_command_rejects_currency_and_numeric_precision_drift() -> None:
    """币种大小写或第十三位小数若被接受，DB 会静默改写 canonical 来源。"""

    command_type = _symbol("domains.costing.schemas", "SourcingEstimateCreate")
    values = {
        "sourcing_case_id": CASE_ID,
        "primary_option_id": PRIMARY_OPTION_ID,
        "supplier_candidate_id": CANDIDATE_ID,
        "product_id": PRODUCT_ID,
        "opportunity_id": OPPORTUNITY_ID,
        "quantity": 500,
        "unit_amount": Decimal("1.100000000001"),
        "currency": "USD",
        "evidence_ref": EVIDENCE_ID,
    }
    with pytest.raises(PydanticValidationError):
        command_type(**{**values, "currency": "usd"})
    with pytest.raises(PydanticValidationError, match="存储精度"):
        command_type(
            **{**values, "unit_amount": Decimal("1.1000000000001")}
        )
