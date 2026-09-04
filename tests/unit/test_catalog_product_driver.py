from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError as PydanticValidationError

from apps.scheduler_worker.catalog_products import CatalogProductDriver
from domains.demand.service import (
    CatalogClusterCursor,
    CatalogClusterIdPage,
    CatalogEvidenceSummary,
    NeedClusterCatalogFacts,
)
from domains.products.catalog_rules import (
    catalog_policy_content_hash,
    evaluate_catalog_facts,
)
from domains.products.service import (
    CatalogPolicyChangeSnapshot,
    CatalogPolicyReconciliationItem,
    CatalogPolicyReconciliationPage,
    CatalogProductProposalView,
    CatalogProposalEvaluationView,
    CatalogProposalPolicyContent,
    CatalogProposalPolicyView,
    CatalogProposalReconciliationItem,
    CatalogProposalReconciliationPage,
    CatalogReconciliationCursor,
    ProductActor,
    ProductRole,
)
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import (
    CatalogProductProposalId,
    CatalogProposalEvaluationId,
    CatalogProposalPolicyVersionId,
    EmployeeId,
    NeedClusterId,
    ProspectAccountId,
    RunId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.provenance import SourceType
from workflows.catalog_product_proposal.mapping import map_catalog_facts

TENANT = TenantId("tn_01M0VKA9S6KX7HRBG3G3ETYD12")
OTHER_TENANT = TenantId("tn_01M0VKA9S6KX7HRBG3G3ETYD13")
NOW = datetime(2026, 9, 5, 9, tzinfo=UTC)
OWNER = EmployeeId("emp_01M0VKA9S6KX7HRBG3G3ETYD12")
POLICY_ID = CatalogProposalPolicyVersionId("cpv_01M0VKA9S6KX7HRBG3G3ETYD12")


def _facts(cluster_id: NeedClusterId) -> NeedClusterCatalogFacts:
    return NeedClusterCatalogFacts(
        tenant_id=TENANT,
        cluster_id=cluster_id,
        cluster_category="three-wheelers",
        member_need_ids=(
            ValidatedNeedId("vnd_01M0VKA9S6KX7HRBG3G3ETYD11"),
            ValidatedNeedId("vnd_01M0VKA9S6KX7HRBG3G3ETYD12"),
            ValidatedNeedId("vnd_01M0VKA9S6KX7HRBG3G3ETYD13"),
        ),
        distinct_account_ids=(
            ProspectAccountId("acct_01M0VKA9S6KX7HRBG3G3ETYD11"),
            ProspectAccountId("acct_01M0VKA9S6KX7HRBG3G3ETYD12"),
            ProspectAccountId("acct_01M0VKA9S6KX7HRBG3G3ETYD13"),
        ),
        member_count=3,
        distinct_account_count=3,
        known_country_codes=(),
        unknown_country_account_count=3,
        recurring_true_account_count=0,
        recurring_false_account_count=0,
        recurring_unknown_account_count=3,
        quantity_unit_covered_account_count=0,
        unified_unit=None,
        safe_total_quantity=None,
        evidence_summaries=(
            CatalogEvidenceSummary(
                source_type=SourceType.CONVERSATION,
                source_id="msg_01M0VKA9S6KX7HRBG3G3ETYD12",
                extracted_by="human",
                confirmed_by=OWNER,
                confirmed_at=NOW,
                observed_at=NOW,
                content_hash="b" * 64,
            ),
        ),
        display_codes=(),
        facts_observed_at=NOW,
        facts_hash="f" * 64,
    )


def _policy(
    *,
    policy_id: CatalogProposalPolicyVersionId = POLICY_ID,
    state: str = "active",
    created_at: datetime = NOW,
) -> CatalogProposalPolicyView:
    content = CatalogProposalPolicyContent(
        minimum_distinct_accounts=3,
        minimum_recurring_accounts=None,
        minimum_distinct_countries=None,
        minimum_quantity_unit_accounts=None,
        require_unified_unit=False,
    )
    return CatalogProposalPolicyView(
        policy_version_id=policy_id,
        content=content,
        content_hash=catalog_policy_content_hash(content),
        base_active_version_id=None,
        proposed_by=OWNER,
        approval_id=None,
        state=state,
        created_at=created_at,
        activated_at=NOW if state == "active" else None,
        terminal_at=None,
    )


def _proposal(
    cluster_id: NeedClusterId,
    *,
    suffix: str,
) -> tuple[CatalogProductProposalView, CatalogProposalEvaluationView]:
    demand_facts = _facts(cluster_id)
    facts = map_catalog_facts(demand_facts)
    result = evaluate_catalog_facts(_policy().content, facts)
    evaluation = CatalogProposalEvaluationView(
        evaluation_id=CatalogProposalEvaluationId(f"cpe_{suffix}"),
        cluster_id=cluster_id,
        policy_version_id=POLICY_ID,
        facts_hash=facts.facts_hash,
        facts=facts,
        rule_results=result.rule_results,
        overall_passed=result.overall_passed,
        blocked_reason=result.blocked_reason,
        proposed_by_run=RunId(f"run_{suffix}"),
        created_at=NOW,
    )
    proposal = CatalogProductProposalView(
        proposal_id=CatalogProductProposalId(f"cpr_{suffix}"),
        evaluation_id=evaluation.evaluation_id,
        cluster_id=cluster_id,
        policy_version_id=POLICY_ID,
        facts_hash=facts.facts_hash,
        owner_employee=OWNER,
        proposed_by_run=evaluation.proposed_by_run,
        approval_id=None,
        state="awaiting_approval_submission",
        created_at=NOW,
        updated_at=NOW,
    )
    return proposal, evaluation


class _Demand:
    def __init__(self, pages: list[CatalogClusterIdPage]) -> None:
        self.pages = pages
        self.page_calls: list[CatalogClusterCursor | None] = []
        self.facts = {}

    async def list_catalog_cluster_id_page(self, tenant_id, *, limit, cursor=None):
        assert tenant_id == TENANT
        assert limit == 2
        self.page_calls.append(cursor)
        return self.pages.pop(0)

    async def get_cluster_catalog_facts(self, tenant_id, cluster_id):
        assert tenant_id == TENANT
        return self.facts.get(cluster_id, _facts(cluster_id))


class _Products:
    def __init__(self) -> None:
        self.active = None
        self.policy_pages: list[CatalogPolicyReconciliationPage] = [
            CatalogPolicyReconciliationPage(
                tenant_id=TENANT, items=(), next_cursor=None
            )
        ]
        self.proposal_pages: list[CatalogProposalReconciliationPage] = [
            CatalogProposalReconciliationPage(
                tenant_id=TENANT, items=(), next_cursor=None
            )
        ]
        self.policy_calls = []
        self.proposal_calls = []
        self.snapshots = {}
        self.evaluations = {}
        self.proposals = {}

    async def list_pending_policy_reconciliation(
        self, tenant_id, *, actor, limit, cursor=None
    ):
        assert tenant_id == actor.tenant_id == TENANT
        assert limit == 2
        self.policy_calls.append(cursor)
        return self.policy_pages.pop(0)

    async def list_awaiting_proposal_reconciliation(
        self, tenant_id, *, actor, limit, cursor=None
    ):
        assert tenant_id == actor.tenant_id == TENANT
        assert limit == 2
        self.proposal_calls.append(cursor)
        return self.proposal_pages.pop(0)

    async def get_policy_change_snapshot(self, tenant_id, policy_id, *, actor):
        assert tenant_id == actor.tenant_id == TENANT
        return self.snapshots[policy_id]

    async def get_active_policy(self, tenant_id, *, actor):
        assert tenant_id == actor.tenant_id == TENANT
        if isinstance(self.active, BaseException):
            raise self.active
        return self.active

    async def get_evaluation(self, tenant_id, evaluation_id, *, actor):
        assert tenant_id == actor.tenant_id == TENANT
        return self.evaluations[evaluation_id]

    async def get_proposal(self, tenant_id, proposal_id, *, actor):
        assert tenant_id == actor.tenant_id == TENANT
        return self.proposals[proposal_id]


class _Engine:
    def __init__(self) -> None:
        self.starts = []
        self.result: object = RunId("run_01M0VKA9S6KX7HRBG3G3ETYD12")

    async def start(
        self,
        tenant_id,
        workflow_type,
        subject_ref,
        initial_context,
        idempotency_key,
        *,
        scheduled_at=None,
    ):
        self.starts.append(
            (
                tenant_id,
                workflow_type,
                subject_ref,
                initial_context,
                idempotency_key,
                scheduled_at,
            )
        )
        return self.result


def _driver(demand: _Demand, products: _Products, engine: _Engine):
    return CatalogProductDriver(
        demand=demand,
        products=products,
        engine=engine,
        system_actor=ProductActor(
            "system:catalog-scheduler", ProductRole.SYSTEM, TENANT
        ),
        tenant_id=TENANT,
        batch_limit=2,
    )


def test_public_reconciliation_cursors_reject_wrong_tenant_stream_and_time() -> None:
    with pytest.raises(PydanticValidationError):
        CatalogReconciliationCursor(
            tenant_id=TENANT,
            stream="awaiting_proposals",
            position_at=NOW.replace(tzinfo=None),
            entity_id=str(POLICY_ID),
        )
    with pytest.raises(PydanticValidationError):
        CatalogPolicyReconciliationPage(
            tenant_id=TENANT,
            items=(),
            next_cursor=CatalogReconciliationCursor(
                tenant_id=OTHER_TENANT,
                stream="pending_policies",
                position_at=NOW,
                entity_id=str(POLICY_ID),
            ),
        )
    with pytest.raises(PydanticValidationError):
        CatalogProposalReconciliationPage(
            tenant_id=TENANT,
            items=(),
            next_cursor=CatalogReconciliationCursor(
                tenant_id=TENANT,
                stream="pending_policies",
                position_at=NOW,
                entity_id=str(POLICY_ID),
            ),
        )
    with pytest.raises(PydanticValidationError):
        CatalogClusterIdPage(
            tenant_id=TENANT,
            cluster_ids=(),
            next_cursor=CatalogClusterCursor(
                tenant_id=OTHER_TENANT,
                created_at=NOW,
                cluster_id=NeedClusterId("ncl_01M0VKA9S6KX7HRBG3G3ETYD12"),
            ),
        )


@pytest.mark.asyncio
async def test_no_active_policy_still_recovers_policy_and_proposal_runs() -> None:
    cluster_id = NeedClusterId("ncl_01M0VKA9S6KX7HRBG3G3ETYD12")
    pending = _policy(state="pending_approval")
    proposal, evaluation = _proposal(cluster_id, suffix="01M0VKA9S6KX7HRBG3G3ETYD12")
    products = _Products()
    products.policy_pages = [
        CatalogPolicyReconciliationPage(
            tenant_id=TENANT,
            items=(
                CatalogPolicyReconciliationItem(
                    policy_version_id=pending.policy_version_id,
                    created_at=pending.created_at,
                ),
            ),
            next_cursor=None,
        )
    ]
    products.proposal_pages = [
        CatalogProposalReconciliationPage(
            tenant_id=TENANT,
            items=(
                CatalogProposalReconciliationItem(
                    proposal_id=proposal.proposal_id,
                    created_at=proposal.created_at,
                ),
            ),
            next_cursor=None,
        )
    ]
    products.snapshots[pending.policy_version_id] = CatalogPolicyChangeSnapshot(
        candidate=pending,
        base=None,
        current=None,
        base_is_current=True,
    )
    products.evaluations[evaluation.evaluation_id] = evaluation
    products.proposals[proposal.proposal_id] = proposal
    demand = _Demand([])
    engine = _Engine()

    result = await _driver(demand, products, engine).scan_once()

    assert result.started_policy_runs == 1
    assert result.started_proposal_runs == 0
    assert result.started_evaluation_runs == 0
    assert result.stop_reason == "policy_not_configured"
    assert [item[1] for item in engine.starts] == [
        "catalog_proposal_policy_change",
    ]
    assert products.proposal_calls == [None]
    assert demand.page_calls == []


@pytest.mark.asyncio
async def test_active_policy_advances_cluster_cursor_then_wraps_without_starvation() -> (
    None
):
    first = NeedClusterId("ncl_01M0VKA9S6KX7HRBG3G3ETYD11")
    second = NeedClusterId("ncl_01M0VKA9S6KX7HRBG3G3ETYD12")
    third = NeedClusterId("ncl_01M0VKA9S6KX7HRBG3G3ETYD13")
    cursor = CatalogClusterCursor(tenant_id=TENANT, created_at=NOW, cluster_id=second)
    demand = _Demand(
        [
            CatalogClusterIdPage(
                tenant_id=TENANT,
                cluster_ids=(first, second),
                next_cursor=cursor,
            ),
            CatalogClusterIdPage(
                tenant_id=TENANT,
                cluster_ids=(third,),
                next_cursor=None,
            ),
            CatalogClusterIdPage(
                tenant_id=TENANT,
                cluster_ids=(first, second),
                next_cursor=cursor,
            ),
        ]
    )
    products = _Products()
    products.active = _policy()
    products.policy_pages *= 3
    products.proposal_pages *= 3
    engine = _Engine()
    driver = _driver(demand, products, engine)

    assert (await driver.scan_once()).started_evaluation_runs == 2
    assert (await driver.scan_once()).started_evaluation_runs == 1
    assert (await driver.scan_once()).started_evaluation_runs == 2

    assert demand.page_calls == [None, cursor, None]
    assert [item[2] for item in engine.starts] == [
        str(first),
        str(second),
        str(third),
        str(first),
        str(second),
    ]


@pytest.mark.asyncio
async def test_unknown_policy_read_is_fixed_retryable_and_skips_cluster_page() -> None:
    products = _Products()
    products.active = RuntimeError("secret facts and dsn")
    demand = _Demand([])

    with pytest.raises(TransientError, match="目录产品调度活动策略暂不可用"):
        await _driver(demand, products, _Engine()).scan_once()

    assert demand.page_calls == []


@pytest.mark.asyncio
async def test_malformed_page_does_not_advance_cursor() -> None:
    first = NeedClusterId("ncl_01M0VKA9S6KX7HRBG3G3ETYD11")
    bad = CatalogClusterIdPage(
        tenant_id=TENANT,
        cluster_ids=(first,),
        next_cursor=CatalogClusterCursor(
            tenant_id=TENANT, created_at=NOW, cluster_id=first
        ),
    )
    demand = _Demand([bad, bad])
    products = _Products()
    products.active = _policy()
    products.policy_pages *= 2
    products.proposal_pages *= 2
    engine = _Engine()
    engine.result = object()
    driver = _driver(demand, products, engine)

    with pytest.raises(TransientError, match="目录产品调度需求簇评估暂不可用"):
        await driver.scan_once()
    with pytest.raises(TransientError, match="目录产品调度需求簇评估暂不可用"):
        await driver.scan_once()

    assert demand.page_calls == [None, None]


@pytest.mark.asyncio
async def test_known_stale_item_does_not_poison_later_policy_item() -> None:
    first = _policy(
        policy_id=CatalogProposalPolicyVersionId("cpv_01M0VKA9S6KX7HRBG3G3ETYD11"),
        state="pending_approval",
    )
    second = _policy(
        policy_id=CatalogProposalPolicyVersionId("cpv_01M0VKA9S6KX7HRBG3G3ETYD12"),
        state="pending_approval",
    )
    products = _Products()
    products.policy_pages = [
        CatalogPolicyReconciliationPage(
            tenant_id=TENANT,
            items=(
                CatalogPolicyReconciliationItem(
                    policy_version_id=first.policy_version_id,
                    created_at=first.created_at,
                ),
                CatalogPolicyReconciliationItem(
                    policy_version_id=second.policy_version_id,
                    created_at=second.created_at,
                ),
            ),
            next_cursor=None,
        )
    ]
    products.proposal_pages = [
        CatalogProposalReconciliationPage(tenant_id=TENANT, items=(), next_cursor=None)
    ]
    products.snapshots[first.policy_version_id] = ValidationError("stale")
    products.snapshots[second.policy_version_id] = CatalogPolicyChangeSnapshot(
        candidate=second,
        base=None,
        current=None,
        base_is_current=True,
    )

    original = products.get_policy_change_snapshot

    async def get_snapshot(*args, **kwargs):
        value = await original(*args, **kwargs)
        if isinstance(value, BaseException):
            raise value
        return value

    products.get_policy_change_snapshot = get_snapshot
    result = await _driver(_Demand([]), products, _Engine()).scan_once()

    assert result.started_policy_runs == 1


def test_driver_rejects_cross_tenant_actor_and_hidden_batch_default() -> None:
    with pytest.raises(ValidationError):
        CatalogProductDriver(
            demand=_Demand([]),
            products=_Products(),
            engine=_Engine(),
            system_actor=ProductActor(
                "system:catalog-scheduler", ProductRole.SYSTEM, OTHER_TENANT
            ),
            tenant_id=TENANT,
            batch_limit=2,
        )
    with pytest.raises(TypeError):
        CatalogProductDriver(
            demand=_Demand([]),
            products=_Products(),
            engine=_Engine(),
            system_actor=ProductActor(
                "system:catalog-scheduler", ProductRole.SYSTEM, TENANT
            ),
            tenant_id=TENANT,
        )
