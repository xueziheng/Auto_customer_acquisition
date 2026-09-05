"""Catalog Product Application 的事件编排与安全启动契约。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from domains.demand.service import CatalogEvidenceSummary, NeedClusterCatalogFacts
from domains.products.catalog_rules import (
    catalog_policy_content_hash,
    evaluate_catalog_facts,
)
from domains.products.permissions import ProductActor, ProductRole
from domains.products.schemas import (
    CatalogClusterFactsInput,
    CatalogEvidenceSummaryInput,
    CatalogPolicyChangeSnapshot,
    CatalogProductProposalView,
    CatalogProposalEvaluationView,
    CatalogProposalPolicyContent,
    CatalogProposalPolicyView,
)
from shared.errors import IdempotencyConflict, TransientError, ValidationError
from shared.events.catalog import (
    AccountCountryFactsChanged,
    CatalogProductProposalCreated,
    CatalogProposalPolicyActivated,
    NeedCatalogFactsChanged,
    NeedClusterMembershipChanged,
)
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

try:
    from workflows.catalog_product_proposal import CatalogProductApplication
except (ImportError, ModuleNotFoundError):
    CatalogProductApplication = None  # type: ignore[assignment,misc]


NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)
TENANT = TenantId("tn_01K00000000000000000000000")
CLUSTER = NeedClusterId("ncl_01K0000000000000000000000")
POLICY_ID = CatalogProposalPolicyVersionId("cpv_01K00000000000000000000000")
EVALUATION_ID = CatalogProposalEvaluationId("cpe_01K00000000000000000000000")
PROPOSAL_ID = CatalogProductProposalId("cpr_01K00000000000000000000000")
OWNER = EmployeeId("emp_01K00000000000000000000000")
FACTS_HASH = "a" * 64


def _content() -> CatalogProposalPolicyContent:
    return CatalogProposalPolicyContent(
        minimum_distinct_accounts=3,
        minimum_recurring_accounts=None,
        minimum_distinct_countries=None,
        minimum_quantity_unit_accounts=None,
        require_unified_unit=False,
    )


CONTENT_HASH = catalog_policy_content_hash(_content())


def _policy(state: str = "active") -> CatalogProposalPolicyView:
    return CatalogProposalPolicyView(
        policy_version_id=POLICY_ID,
        content=_content(),
        content_hash=CONTENT_HASH,
        base_active_version_id=None,
        proposed_by=OWNER,
        approval_id=None,
        state=state,
        created_at=NOW,
        activated_at=NOW if state == "active" else None,
        terminal_at=None,
    )


def _facts() -> NeedClusterCatalogFacts:
    return NeedClusterCatalogFacts(
        tenant_id=TENANT,
        cluster_id=CLUSTER,
        cluster_category="three-wheelers",
        member_need_ids=(
            ValidatedNeedId("need_01K00000000000000000000000"),
            ValidatedNeedId("need_01K00000000000000000000001"),
            ValidatedNeedId("need_01K00000000000000000000002"),
        ),
        distinct_account_ids=(
            ProspectAccountId("acct_01K0000000000000000000000"),
            ProspectAccountId("acct_01K0000000000000000000001"),
            ProspectAccountId("acct_01K0000000000000000000002"),
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
                source_id="msg_01K00000000000000000000000",
                extracted_by="human",
                confirmed_by=OWNER,
                confirmed_at=NOW,
                observed_at=NOW,
                content_hash="b" * 64,
            ),
        ),
        display_codes=(),
        facts_observed_at=NOW,
        facts_hash=FACTS_HASH,
    )


class _Demand:
    def __init__(self) -> None:
        self.facts = _facts()
        self.fact_reads: list[tuple[object, ...]] = []
        self.account_reads: list[tuple[object, ...]] = []
        self.cluster_reads: list[tuple[object, ...]] = []

    async def get_cluster_catalog_facts(self, tenant_id, cluster_id):
        self.fact_reads.append((tenant_id, cluster_id))
        return self.facts

    async def list_catalog_cluster_ids_for_account(
        self, tenant_id, account_id, *, limit
    ):
        self.account_reads.append((tenant_id, account_id, limit))
        return (CLUSTER,)

    async def list_catalog_cluster_ids(self, tenant_id, *, limit):
        self.cluster_reads.append((tenant_id, limit))
        return (CLUSTER,)


class _Products:
    def __init__(self) -> None:
        self.active: CatalogProposalPolicyView | None = _policy()
        self.created_key: str | None = None
        self.created_policy_id = POLICY_ID
        self.snapshot_reads: list[tuple[object, ...]] = []
        self.snapshot = CatalogPolicyChangeSnapshot(
            base=None, current=None, candidate=_policy("pending_approval"), base_is_current=True
        )
        product_facts = CatalogClusterFactsInput.model_validate(
            {
                **{
                    name: getattr(_facts(), name)
                    for name in CatalogClusterFactsInput.model_fields
                    if name != "evidence_summaries"
                },
                "evidence_summaries": (
                    CatalogEvidenceSummaryInput(
                        source_type="conversation",
                        source_id="msg_01K00000000000000000000000",
                        extracted_by="human",
                        confirmed_by=OWNER,
                        confirmed_at=NOW,
                        observed_at=NOW,
                        content_hash="b" * 64,
                    ),
                ),
            }
        )
        evaluation = evaluate_catalog_facts(_content(), product_facts)
        self.evaluation = CatalogProposalEvaluationView(
            evaluation_id=EVALUATION_ID,
            cluster_id=CLUSTER,
            policy_version_id=POLICY_ID,
            facts_hash=FACTS_HASH,
            facts=product_facts,
            rule_results=evaluation.rule_results,
            overall_passed=evaluation.overall_passed,
            blocked_reason=evaluation.blocked_reason,
            proposed_by_run=RunId("run_01K00000000000000000000000"),
            created_at=NOW,
        )
        self.proposal = CatalogProductProposalView(
            proposal_id=PROPOSAL_ID,
            evaluation_id=EVALUATION_ID,
            cluster_id=CLUSTER,
            policy_version_id=POLICY_ID,
            facts_hash=FACTS_HASH,
            owner_employee=OWNER,
            proposed_by_run=RunId("run_01K00000000000000000000000"),
            approval_id=None,
            state="awaiting_approval_submission",
            created_at=NOW,
            updated_at=NOW,
        )

    async def get_active_policy(self, tenant_id, *, actor):
        return self.active

    async def create_policy_candidate(self, tenant_id, content, *, idempotency_key, actor):
        self.created_key = idempotency_key
        return self.created_policy_id

    async def get_policy_change_snapshot(self, tenant_id, policy_version_id, *, actor):
        self.snapshot_reads.append((tenant_id, policy_version_id, actor))
        return self.snapshot

    async def get_proposal(self, tenant_id, proposal_id, *, actor):
        return self.proposal

    async def get_evaluation(self, tenant_id, evaluation_id, *, actor):
        return self.evaluation


class _Engine:
    def __init__(self) -> None:
        self.starts: list[tuple[object, ...]] = []

    async def start(
        self, tenant_id, workflow_type, subject_ref, initial_context, idempotency_key, **kwargs
    ):
        self.starts.append(
            (tenant_id, workflow_type, subject_ref, initial_context, idempotency_key)
        )
        return RunId("run_01K00000000000000000000011")


def _application(demand: _Demand, products: _Products, engine: _Engine):
    assert CatalogProductApplication is not None, "RED：Task 11 application 尚未实现"
    return CatalogProductApplication(
        demand,
        products,
        engine,
        ProductActor("system:catalog-products", ProductRole.SYSTEM, TENANT),
    )


@pytest.mark.asyncio
async def test_membership_event_rereads_current_facts_and_starts_metadata_only_run() -> None:
    """若 handler 信任 event.member_count 或把 facts body 放入 context，本测试应失败。"""
    demand, products, engine = _Demand(), _Products(), _Engine()
    app = _application(demand, products, engine)
    event = NeedClusterMembershipChanged(
        tenant_id=TENANT,
        occurred_at=NOW,
        cluster_id=CLUSTER,
        changed_need_id=ValidatedNeedId("need_01K00000000000000000000000"),
        member_count=999,
    )

    await app.handle_need_cluster_membership_changed(event)

    assert demand.fact_reads == [(TENANT, CLUSTER)]
    assert len(engine.starts) == 1
    _, workflow_type, subject, context, key = engine.starts[0]
    assert workflow_type == "catalog_cluster_evaluation"
    assert subject == str(CLUSTER)
    assert key == f"catalog-evaluation:{TENANT}:{CLUSTER}:{POLICY_ID}:{FACTS_HASH}"
    assert context == {
        "cluster_id": str(CLUSTER),
        "policy_version_id": str(POLICY_ID),
        "policy_content_hash": CONTENT_HASH,
        "facts_hash": FACTS_HASH,
    }
    assert "member_count" not in context
    assert "evidence_summaries" not in context


@pytest.mark.asyncio
async def test_missing_policy_and_unclustered_fact_change_are_acknowledged_noops() -> None:
    """若实现制造默认策略、虚构单成员簇或启动 Run，本测试应失败。"""
    demand, products, engine = _Demand(), _Products(), _Engine()
    products.active = None
    app = _application(demand, products, engine)

    result = await app.handle_need_catalog_facts_changed(
        NeedCatalogFactsChanged(
            tenant_id=TENANT,
            occurred_at=NOW,
            need_id=ValidatedNeedId("need_01K00000000000000000000000"),
            cluster_id=None,
            change_kind="quantity",
        )
    )
    membership = await app.handle_need_cluster_membership_changed(
        NeedClusterMembershipChanged(
            tenant_id=TENANT,
            occurred_at=NOW,
            cluster_id=CLUSTER,
            changed_need_id=ValidatedNeedId("need_01K00000000000000000000000"),
            member_count=3,
        )
    )

    assert result is None
    assert membership is None
    assert demand.fact_reads == []
    assert engine.starts == []


@pytest.mark.asyncio
async def test_account_and_policy_events_use_explicit_bounded_cluster_reads() -> None:
    """若 account/policy 事件无界 fan-out 或信任事件携带列表，本测试应失败。"""
    demand, products, engine = _Demand(), _Products(), _Engine()
    app = _application(demand, products, engine)
    account = ProspectAccountId("acct_01K0000000000000000000000")

    await app.handle_account_country_facts_changed(
        AccountCountryFactsChanged(
            tenant_id=TENANT, occurred_at=NOW, account_id=account
        ),
        limit=17,
    )
    await app.handle_catalog_policy_activated(
        CatalogProposalPolicyActivated(
            tenant_id=TENANT,
            occurred_at=NOW,
            policy_version_id=POLICY_ID,
            content_hash=CONTENT_HASH,
        ),
        limit=19,
    )

    assert demand.account_reads == [(TENANT, account, 17)]
    assert demand.cluster_reads == [(TENANT, 19)]
    assert len(engine.starts) == 2


@pytest.mark.asyncio
async def test_policy_event_mismatch_is_permanent_and_dependency_failure_is_redacted_retryable() -> None:
    """若陈旧 policy event 启动评估或泄露底层异常文本，本测试应失败。"""
    demand, products, engine = _Demand(), _Products(), _Engine()
    app = _application(demand, products, engine)
    with pytest.raises(ValidationError, match="目录提案策略激活事件无效"):
        await app.handle_catalog_policy_activated(
            CatalogProposalPolicyActivated(
                tenant_id=TENANT,
                occurred_at=NOW,
                policy_version_id=POLICY_ID,
                content_hash="f" * 64,
            ),
            limit=10,
        )
    assert engine.starts == []

    async def broken(*args, **kwargs):
        raise RuntimeError("postgres://secret@private/database")

    demand.get_cluster_catalog_facts = broken  # type: ignore[method-assign]
    with pytest.raises(TransientError) as failure:
        await app.handle_need_cluster_membership_changed(
            NeedClusterMembershipChanged(
                tenant_id=TENANT,
                occurred_at=NOW,
                cluster_id=CLUSTER,
                changed_need_id=ValidatedNeedId("need_01K00000000000000000000000"),
                member_count=3,
            )
        )
    assert "secret" not in str(failure.value)

    async def malformed_policy(*args, **kwargs):
        raise ValidationError("private malformed policy body")

    products.get_active_policy = malformed_policy  # type: ignore[method-assign]
    with pytest.raises(ValidationError) as malformed:
        await app.handle_catalog_policy_activated(
            CatalogProposalPolicyActivated(
                tenant_id=TENANT,
                occurred_at=NOW,
                policy_version_id=POLICY_ID,
                content_hash=CONTENT_HASH,
            ),
            limit=10,
        )
    assert type(malformed.value) is ValidationError
    assert "private" not in str(malformed.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "active_policy",
    (
        None,
        _policy().model_copy(
            update={
                "policy_version_id": CatalogProposalPolicyVersionId(
                    "cpv_01K00000000000000000000099"
                )
            }
        ),
    ),
)
async def test_stale_proposal_event_is_noop_when_active_policy_is_absent_or_replaced(
    active_policy: CatalogProposalPolicyView | None,
) -> None:
    """旧 proposal outbox 可晚于策略替换投递，不能因此毒化消费者。"""
    demand, products, engine = _Demand(), _Products(), _Engine()
    products.active = active_policy
    app = _application(demand, products, engine)

    result = await app.handle_catalog_product_proposal_created(
        CatalogProductProposalCreated(
            tenant_id=TENANT,
            occurred_at=NOW,
            run_id=RunId("run_01K00000000000000000000000"),
            proposal_id=PROPOSAL_ID,
            evaluation_id=EVALUATION_ID,
            cluster_id=CLUSTER,
            policy_version_id=POLICY_ID,
            facts_hash=FACTS_HASH,
        )
    )

    assert result is None
    assert engine.starts == []


@pytest.mark.asyncio
async def test_submit_policy_forwards_original_key_and_starts_after_commit() -> None:
    """若应用 trim/重建 key 或未在 Products commit 后启动 workflow，本测试应失败。"""
    demand, products, engine = _Demand(), _Products(), _Engine()
    app = _application(demand, products, engine)
    actor = ProductActor(str(OWNER), ProductRole.PRODUCT, TENANT)

    result = await app.submit_policy_candidate(
        TENANT, _content(), idempotency_key=" original-key ", actor=actor
    )

    assert result == products.snapshot.candidate
    assert products.created_key == " original-key "
    assert engine.starts[0][1:3] == ("catalog_proposal_policy_change", str(POLICY_ID))
    assert engine.starts[0][4] == f"catalog-policy-change:{TENANT}:{POLICY_ID}"


@pytest.mark.asyncio
async def test_submit_policy_only_preserves_idempotency_conflict_from_create() -> None:
    demand, products, engine = _Demand(), _Products(), _Engine()
    app = _application(demand, products, engine)
    actor = ProductActor(str(OWNER), ProductRole.PRODUCT, TENANT)

    async def create_conflict(*args, **kwargs):
        del args, kwargs
        raise IdempotencyConflict("private key binding detail")

    products.create_policy_candidate = create_conflict  # type: ignore[method-assign]
    with pytest.raises(IdempotencyConflict):
        await app.submit_policy_candidate(
            TENANT, _content(), idempotency_key="same-key", actor=actor
        )
    assert engine.starts == []

    products = _Products()
    app = _application(demand, products, engine)

    async def snapshot_conflict(*args, **kwargs):
        del args, kwargs
        raise IdempotencyConflict("private approval binding detail")

    products.get_policy_change_snapshot = snapshot_conflict  # type: ignore[method-assign]
    with pytest.raises(TransientError) as failure:
        await app.submit_policy_candidate(
            TENANT, _content(), idempotency_key="new-key", actor=actor
        )
    assert "private" not in str(failure.value)


@pytest.mark.asyncio
async def test_submit_policy_binds_canonical_snapshot_to_created_policy() -> None:
    demand, products, engine = _Demand(), _Products(), _Engine()
    products.snapshot = products.snapshot.model_copy(
        update={
            "candidate": products.snapshot.candidate.model_copy(
                update={
                    "policy_version_id": CatalogProposalPolicyVersionId(
                        "cpv_01K00000000000000000000099"
                    )
                }
            )
        }
    )
    app = _application(demand, products, engine)

    with pytest.raises(TransientError) as failure:
        await app.submit_policy_candidate(
            TENANT,
            _content(),
            idempotency_key="snapshot-binding",
            actor=ProductActor(str(OWNER), ProductRole.PRODUCT, TENANT),
        )

    assert "00099" not in str(failure.value)
    assert engine.starts == []


@pytest.mark.asyncio
async def test_submit_policy_rejects_malformed_create_id_before_snapshot_io() -> None:
    demand, products, engine = _Demand(), _Products(), _Engine()
    malformed_id = CatalogProposalPolicyVersionId("cpv_bad")
    products.created_policy_id = malformed_id
    products.snapshot = products.snapshot.model_copy(
        update={
            "candidate": products.snapshot.candidate.model_copy(
                update={"policy_version_id": malformed_id}
            )
        }
    )
    app = _application(demand, products, engine)

    with pytest.raises(TransientError) as failure:
        await app.submit_policy_candidate(
            TENANT,
            _content(),
            idempotency_key="malformed-create-id",
            actor=ProductActor(str(OWNER), ProductRole.PRODUCT, TENANT),
        )

    assert "cpv_bad" not in str(failure.value)
    assert products.snapshot_reads == []
    assert engine.starts == []


@pytest.mark.asyncio
async def test_proposal_event_rejects_noncanonical_evaluation_before_start() -> None:
    """若 proposal wake-up 不重读/验证 canonical evaluation，本测试应失败。"""
    demand, products, engine = _Demand(), _Products(), _Engine()
    app = _application(demand, products, engine)
    with pytest.raises(ValidationError, match="目录产品提案事件事实无效"):
        await app.handle_catalog_product_proposal_created(
            CatalogProductProposalCreated(
                tenant_id=TENANT,
                occurred_at=NOW,
                run_id=RunId("run_01K00000000000000000000000"),
                proposal_id=PROPOSAL_ID,
                evaluation_id=CatalogProposalEvaluationId(
                    "cpe_01K00000000000000000000099"
                ),
                cluster_id=CLUSTER,
                policy_version_id=POLICY_ID,
                facts_hash=FACTS_HASH,
            )
        )
    assert engine.starts == []

    products.evaluation = _Products().evaluation
    products.proposal = products.proposal.model_copy(
        update={
            "proposal_id": CatalogProductProposalId(
                "cpr_01K00000000000000000000099"
            )
        }
    )
    with pytest.raises(ValidationError, match="目录产品提案事件事实无效"):
        await app.handle_catalog_product_proposal_created(
            CatalogProductProposalCreated(
                tenant_id=TENANT,
                occurred_at=NOW,
                run_id=RunId("run_01K00000000000000000000000"),
                proposal_id=PROPOSAL_ID,
                evaluation_id=EVALUATION_ID,
                cluster_id=CLUSTER,
                policy_version_id=POLICY_ID,
                facts_hash=FACTS_HASH,
            )
        )
    assert engine.starts == []
