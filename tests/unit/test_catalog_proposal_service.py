"""Catalog Product Proposal 评估、审批与培养生命周期单元测试。"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self

import pytest

from domains.products.catalog_rules import (
    catalog_policy_content_hash,
)
from domains.products.catalog_service_impl import CatalogProposalServiceImpl
from domains.products.errors import (
    CatalogCultivationCaseNotFoundError,
    CatalogCultivationConflictError,
    CatalogEvaluationConflictError,
    CatalogEvaluationNotFoundError,
    CatalogProposalApprovalConflictError,
    CatalogProposalDecisionInvalidError,
    CatalogProposalNotFoundError,
    CatalogProposalStateTransitionError,
)
from domains.products.permissions import (
    Phase2ProductAuthorizer,
    ProductActor,
    ProductRole,
)
from domains.products.schemas import (
    CatalogApprovalDecisionInput,
    CatalogBlockedFactsInput,
    CatalogClusterFactsInput,
    CatalogEvidenceSummaryInput,
    CatalogProposalPolicyContent,
)
from domains.products.service import (
    CatalogCultivationCase,
    CatalogProductProposal,
    CatalogProductProposalState,
    CatalogProposalEvaluation,
    CatalogProposalPolicyState,
    CatalogProposalPolicyVersion,
    catalog_cultivation_change_set_ref,
    catalog_policy_creation_request_hash,
)
from shared.errors import (
    IdempotencyConflict,
    InvalidStateTransition,
    PermissionDenied,
    TenantIsolationViolation,
    TransientError,
    ValidationError,
)
from shared.events.catalog import (
    CatalogCultivationQueued,
    CatalogProductProposalCreated,
)
from shared.schemas.identifiers import (
    ApprovalId,
    CatalogCultivationCaseId,
    CatalogProductProposalId,
    CatalogProposalEvaluationId,
    CatalogProposalPolicyVersionId,
    EmployeeId,
    NeedClusterId,
    RunId,
    TenantId,
)

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
TENANT = TenantId("tn_catalog_proposal_unit")
OTHER_TENANT = TenantId("tn_catalog_proposal_other")
OWNER = EmployeeId("emp_catalog_owner")
REVIEWER = EmployeeId("emp_catalog_reviewer")
RUN = RunId("run_catalog_evaluation")
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def test_cultivation_reference_is_the_only_exact_subject_format() -> None:
    assert (
        catalog_cultivation_change_set_ref(
            CatalogProductProposalId("cpr_exact"),
            CatalogProposalPolicyVersionId("cpv_exact"),
            HASH_A,
        )
        == f"catalog-cultivation:cpr_exact:cpv_exact:{HASH_A}"
    )

    for proposal_id, policy_id, facts_hash in (
        (
            CatalogProductProposalId("proposal"),
            CatalogProposalPolicyVersionId("cpv_x"),
            HASH_A,
        ),
        (
            CatalogProductProposalId("cpr_x"),
            CatalogProposalPolicyVersionId("policy"),
            HASH_A,
        ),
        (
            CatalogProductProposalId("cpr_x"),
            CatalogProposalPolicyVersionId("cpv_x"),
            "A" * 64,
        ),
    ):
        with pytest.raises(ValidationError):
            catalog_cultivation_change_set_ref(proposal_id, policy_id, facts_hash)


def _content(accounts: int = 3) -> CatalogProposalPolicyContent:
    return CatalogProposalPolicyContent(
        minimum_distinct_accounts=accounts,
        minimum_recurring_accounts=None,
        minimum_distinct_countries=None,
        minimum_quantity_unit_accounts=None,
        require_unified_unit=False,
    )


def _active_policy(marker: str = "active") -> CatalogProposalPolicyVersion:
    content = _content()
    return CatalogProposalPolicyVersion(
        tenant_id=TENANT,
        policy_version_id=CatalogProposalPolicyVersionId(f"cpv_{marker}"),
        content=content,
        content_hash=catalog_policy_content_hash(content),
        base_active_version_id=None,
        proposed_by=OWNER,
        creation_key=f"create-{marker}",
        creation_request_hash=catalog_policy_creation_request_hash(
            content, OWNER, None
        ),
        approval_id=ApprovalId(f"apr_policy_{marker}"),
        state=CatalogProposalPolicyState.ACTIVE,
        created_at=NOW - timedelta(days=1),
        activated_at=NOW - timedelta(hours=23),
    )


def _evidence(marker: str = "1") -> CatalogEvidenceSummaryInput:
    return CatalogEvidenceSummaryInput(
        source_type="conversation",
        source_id=f"msg_catalog_{marker}",
        extracted_by="employee",
        confirmed_by=OWNER,
        confirmed_at=NOW - timedelta(hours=2),
        observed_at=NOW - timedelta(hours=2),
        content_hash=HASH_B,
    )


def _facts(
    *,
    accounts: int = 3,
    facts_hash: str = HASH_A,
    tenant_id: TenantId = TENANT,
) -> CatalogClusterFactsInput:
    needs = tuple(f"need_{index}" for index in range(1, accounts + 1))
    account_ids = tuple(f"acc_{index}" for index in range(1, accounts + 1))
    return CatalogClusterFactsInput(
        tenant_id=tenant_id,
        cluster_id=NeedClusterId("ncl_catalog_unit"),
        cluster_category="three_wheelers",
        member_need_ids=needs,
        distinct_account_ids=account_ids,
        member_count=accounts,
        distinct_account_count=accounts,
        known_country_codes=("KE",),
        unknown_country_account_count=accounts - 1,
        recurring_true_account_count=0,
        recurring_false_account_count=0,
        recurring_unknown_account_count=accounts,
        quantity_unit_covered_account_count=0,
        unified_unit=None,
        safe_total_quantity=None,
        evidence_summaries=(_evidence(),),
        display_codes=("country_unknown",),
        facts_observed_at=NOW - timedelta(hours=1),
        facts_hash=facts_hash,
    )


def _system(tenant_id: TenantId = TENANT) -> ProductActor:
    return ProductActor(
        "system:catalog-proposal-workflow", ProductRole.SYSTEM, tenant_id
    )


def _reader(role: ProductRole = ProductRole.PRODUCT) -> ProductActor:
    return ProductActor(str(OWNER), role, TENANT)


@dataclass(frozen=True)
class _ApprovalFact:
    approval_id: ApprovalId
    request_hash: str
    proposal_id: CatalogProductProposalId
    policy_version_id: CatalogProposalPolicyVersionId
    facts_hash: str
    owner_employee: EmployeeId
    proposed_by_run: RunId


class _Store:
    def __init__(self) -> None:
        self.policies: dict[
            CatalogProposalPolicyVersionId, CatalogProposalPolicyVersion
        ] = {}
        self.evaluations: dict[
            CatalogProposalEvaluationId, CatalogProposalEvaluation
        ] = {}
        self.proposals: dict[CatalogProductProposalId, CatalogProductProposal] = {}
        self.cases: dict[CatalogCultivationCaseId, CatalogCultivationCase] = {}
        self.approvals: dict[ApprovalId, _ApprovalFact] = {}
        self.events: list[object] = []
        self.enter_count = 0
        self.namespace_lock_count = 0
        self.active_lock_count = 0
        self.run_guard_count = 0
        self.run_guard_error: Exception | None = None
        self.read_error: Exception | None = None


@dataclass(frozen=True)
class _Page[T]:
    items: tuple[T, ...]


class _Policies:
    def __init__(self, store: _Store, uow: _Uow) -> None:
        self._store = store
        self._uow = uow

    async def lock_policy_namespace(self, tenant_id: TenantId) -> None:
        assert tenant_id == TENANT
        self._uow.namespace_locked = True
        self._store.namespace_lock_count += 1

    async def get_active(
        self, tenant_id: TenantId, *, for_update: bool = False
    ) -> CatalogProposalPolicyVersion | None:
        if self._store.read_error is not None:
            raise self._store.read_error
        if for_update:
            assert self._uow.namespace_locked
            self._store.active_lock_count += 1
        return next(
            (
                item
                for item in self._store.policies.values()
                if item.tenant_id == tenant_id
                and item.state is CatalogProposalPolicyState.ACTIVE
            ),
            None,
        )


class _Evaluations:
    def __init__(self, store: _Store) -> None:
        self._store = store

    async def require_trusted_catalog_evaluation_run(
        self,
        tenant_id: TenantId,
        run_id: RunId,
        cluster_id: NeedClusterId,
    ) -> None:
        assert tenant_id == TENANT
        assert str(run_id).startswith("run_")
        assert cluster_id == NeedClusterId("ncl_catalog_unit")
        self._store.run_guard_count += 1
        if self._store.run_guard_error is not None:
            raise self._store.run_guard_error

    async def add(
        self, tenant_id: TenantId, evaluation: CatalogProposalEvaluation
    ) -> CatalogProposalEvaluation:
        assert tenant_id == evaluation.tenant_id
        existing = await self.get_by_subject(
            tenant_id,
            evaluation.cluster_id,
            evaluation.policy_version_id,
            evaluation.facts_hash,
        )
        if existing is not None:
            if existing != evaluation:
                raise IdempotencyConflict("evaluation mismatch")
            return existing
        self._store.evaluations[evaluation.evaluation_id] = evaluation
        return evaluation

    async def get(
        self, tenant_id: TenantId, evaluation_id: CatalogProposalEvaluationId
    ) -> CatalogProposalEvaluation | None:
        if self._store.read_error is not None:
            raise self._store.read_error
        item = self._store.evaluations.get(evaluation_id)
        return item if item is not None and item.tenant_id == tenant_id else None

    async def get_by_subject(
        self,
        tenant_id: TenantId,
        cluster_id: NeedClusterId,
        policy_version_id: CatalogProposalPolicyVersionId,
        facts_hash: str,
    ) -> CatalogProposalEvaluation | None:
        return next(
            (
                item
                for item in self._store.evaluations.values()
                if item.tenant_id == tenant_id
                and item.cluster_id == cluster_id
                and item.policy_version_id == policy_version_id
                and item.facts_hash == facts_hash
            ),
            None,
        )

    async def list_evaluations(
        self, tenant_id: TenantId, *, limit: int, cursor: object = None
    ) -> _Page[CatalogProposalEvaluation]:
        del cursor
        values = sorted(
            (
                item
                for item in self._store.evaluations.values()
                if item.tenant_id == tenant_id
            ),
            key=lambda item: (item.created_at, str(item.evaluation_id)),
            reverse=True,
        )[:limit]
        return _Page(tuple(values))


class _Proposals:
    def __init__(self, store: _Store) -> None:
        self._store = store

    async def add(
        self, tenant_id: TenantId, proposal: CatalogProductProposal
    ) -> CatalogProductProposal:
        assert tenant_id == proposal.tenant_id
        existing = await self.get_by_evaluation(tenant_id, proposal.evaluation_id)
        if existing is not None:
            immutable = (
                existing.evaluation_id,
                existing.cluster_id,
                existing.policy_version_id,
                existing.facts_hash,
                existing.owner_employee,
                existing.proposed_by_run,
            )
            requested = (
                proposal.evaluation_id,
                proposal.cluster_id,
                proposal.policy_version_id,
                proposal.facts_hash,
                proposal.owner_employee,
                proposal.proposed_by_run,
            )
            if immutable != requested:
                raise IdempotencyConflict("proposal mismatch")
            return existing
        self._store.proposals[proposal.proposal_id] = proposal
        return proposal

    async def get(
        self, tenant_id: TenantId, proposal_id: CatalogProductProposalId
    ) -> CatalogProductProposal | None:
        if self._store.read_error is not None:
            raise self._store.read_error
        item = self._store.proposals.get(proposal_id)
        return item if item is not None and item.tenant_id == tenant_id else None

    async def get_for_update(
        self, tenant_id: TenantId, proposal_id: CatalogProductProposalId
    ) -> CatalogProductProposal | None:
        return await self.get(tenant_id, proposal_id)

    async def get_by_evaluation(
        self, tenant_id: TenantId, evaluation_id: CatalogProposalEvaluationId
    ) -> CatalogProductProposal | None:
        return next(
            (
                item
                for item in self._store.proposals.values()
                if item.tenant_id == tenant_id and item.evaluation_id == evaluation_id
            ),
            None,
        )

    async def update(
        self, tenant_id: TenantId, proposal: CatalogProductProposal
    ) -> CatalogProductProposal:
        assert tenant_id == proposal.tenant_id
        self._store.proposals[proposal.proposal_id] = proposal
        return proposal

    async def bind_approval(
        self,
        tenant_id: TenantId,
        proposal_id: CatalogProductProposalId,
        approval_id: ApprovalId,
        expected_request_hash: str,
        bound_at: datetime,
    ) -> CatalogProductProposal | None:
        proposal = await self.get_for_update(tenant_id, proposal_id)
        if proposal is None:
            return None
        approval = self._store.approvals.get(approval_id)
        if approval is None or (
            approval.request_hash,
            approval.proposal_id,
            approval.policy_version_id,
            approval.facts_hash,
            approval.owner_employee,
            approval.proposed_by_run,
        ) != (
            expected_request_hash,
            proposal.proposal_id,
            proposal.policy_version_id,
            proposal.facts_hash,
            proposal.owner_employee,
            proposal.proposed_by_run,
        ):
            raise IdempotencyConflict("approval mismatch")
        if proposal.approval_id is not None:
            if (
                proposal.approval_id != approval_id
                or proposal.approval_request_hash != expected_request_hash
            ):
                raise IdempotencyConflict("approval already bound")
            return proposal
        if (
            proposal.state
            is not CatalogProductProposalState.AWAITING_APPROVAL_SUBMISSION
        ):
            raise InvalidStateTransition("proposal terminal")
        bound = replace(
            proposal,
            approval_id=approval_id,
            approval_request_hash=expected_request_hash,
            state=CatalogProductProposalState.PENDING_REVIEW,
            updated_at=bound_at,
        )
        self._store.proposals[proposal_id] = bound
        return bound

    async def list_proposals(
        self, tenant_id: TenantId, *, limit: int, cursor: object = None
    ) -> _Page[CatalogProductProposal]:
        del cursor
        if self._store.read_error is not None:
            raise self._store.read_error
        values = sorted(
            (
                item
                for item in self._store.proposals.values()
                if item.tenant_id == tenant_id
            ),
            key=lambda item: (item.updated_at, str(item.proposal_id)),
            reverse=True,
        )[:limit]
        return _Page(tuple(values))


class _Cases:
    def __init__(self, store: _Store) -> None:
        self._store = store

    async def add(
        self, tenant_id: TenantId, cultivation_case: CatalogCultivationCase
    ) -> CatalogCultivationCase:
        assert tenant_id == cultivation_case.tenant_id
        existing = await self.get_by_proposal(tenant_id, cultivation_case.proposal_id)
        if existing is not None:
            immutable = (
                existing.approval_id,
                existing.cluster_id,
                existing.policy_version_id,
                existing.facts_hash,
                existing.evidence_refs,
            )
            requested = (
                cultivation_case.approval_id,
                cultivation_case.cluster_id,
                cultivation_case.policy_version_id,
                cultivation_case.facts_hash,
                cultivation_case.evidence_refs,
            )
            if immutable != requested:
                raise IdempotencyConflict("cultivation mismatch")
            return existing
        self._store.cases[cultivation_case.cultivation_case_id] = cultivation_case
        return cultivation_case

    async def get(
        self, tenant_id: TenantId, cultivation_case_id: CatalogCultivationCaseId
    ) -> CatalogCultivationCase | None:
        item = self._store.cases.get(cultivation_case_id)
        return item if item is not None and item.tenant_id == tenant_id else None

    async def get_by_proposal(
        self, tenant_id: TenantId, proposal_id: CatalogProductProposalId
    ) -> CatalogCultivationCase | None:
        return next(
            (
                item
                for item in self._store.cases.values()
                if item.tenant_id == tenant_id and item.proposal_id == proposal_id
            ),
            None,
        )

    async def list_cases(
        self, tenant_id: TenantId, *, limit: int, cursor: object = None
    ) -> _Page[CatalogCultivationCase]:
        del cursor
        values = sorted(
            (
                item
                for item in self._store.cases.values()
                if item.tenant_id == tenant_id
            ),
            key=lambda item: (item.queued_at, str(item.cultivation_case_id)),
            reverse=True,
        )[:limit]
        return _Page(tuple(values))


class _Bus:
    def __init__(self, store: _Store) -> None:
        self._store = store

    async def publish(self, event: object) -> None:
        self._store.events.append(event)


class _Uow:
    def __init__(self, store: _Store) -> None:
        self._store = store
        self.namespace_locked = False
        self.policies = _Policies(store, self)
        self.evaluations = _Evaluations(store)
        self.proposals = _Proposals(store)
        self.cultivation_cases = _Cases(store)
        self.bus = _Bus(store)

    async def __aenter__(self) -> Self:
        self._store.enter_count += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        del exc_type, exc, tb


def _service(store: _Store) -> CatalogProposalServiceImpl:
    return CatalogProposalServiceImpl(
        lambda _tenant: _Uow(store),
        Phase2ProductAuthorizer(TENANT),
        now=lambda: NOW,
    )


async def _evaluate(
    store: _Store, facts: CatalogClusterFactsInput | None = None
) -> tuple[CatalogProposalServiceImpl, CatalogProductProposal]:
    store.policies[_active_policy().policy_version_id] = _active_policy()
    service = _service(store)
    await service.evaluate_cluster(
        TENANT, facts or _facts(), proposed_by_run=RUN, actor=_system()
    )
    return service, next(iter(store.proposals.values()))


def _approval_fact(proposal: CatalogProductProposal) -> _ApprovalFact:
    return _ApprovalFact(
        approval_id=ApprovalId("apr_catalog_cultivation"),
        request_hash=HASH_C,
        proposal_id=proposal.proposal_id,
        policy_version_id=proposal.policy_version_id,
        facts_hash=proposal.facts_hash,
        owner_employee=proposal.owner_employee,
        proposed_by_run=proposal.proposed_by_run,
    )


async def _bind(
    store: _Store, service: CatalogProposalServiceImpl, proposal: CatalogProductProposal
) -> CatalogProductProposal:
    approval = _approval_fact(proposal)
    store.approvals[approval.approval_id] = approval
    await service.bind_proposal_approval(
        TENANT,
        proposal.proposal_id,
        approval.approval_id,
        approval.request_hash,
        actor=_system(),
    )
    return store.proposals[proposal.proposal_id]


def _decision(
    proposal: CatalogProductProposal,
    state: str = "approved",
    **changes: object,
) -> CatalogApprovalDecisionInput:
    decided = state in {"approved", "rejected"}
    payload: dict[str, object] = {
        "approval_id": proposal.approval_id,
        "approval_type": "catalog_product_cultivation",
        "contract_namespace": "catalog-cultivation-v1",
        "change_set_ref": (
            f"catalog-cultivation:{proposal.proposal_id}:"
            f"{proposal.policy_version_id}:{proposal.facts_hash}"
        ),
        "request_hash": proposal.approval_request_hash,
        "state": state,
        "proposed_by_run": proposal.proposed_by_run,
        "proposed_by_employee": None,
        "owner_employee": proposal.owner_employee,
        "decided_by_employee": REVIEWER if decided else None,
        "decided_at": NOW - timedelta(minutes=1) if decided else None,
        "expires_at": NOW if state == "expired" else NOW + timedelta(days=3),
    }
    payload.update(changes)
    return CatalogApprovalDecisionInput.model_validate(payload)


@pytest.mark.asyncio
async def test_evaluation_requires_system_actor_trusted_run_and_active_policy() -> None:
    store = _Store()
    service = _service(store)

    with pytest.raises(PermissionDenied):
        await service.evaluate_cluster(
            TENANT, _facts(), proposed_by_run=RUN, actor=_reader()
        )
    with pytest.raises(ValidationError):
        await service.evaluate_cluster(
            TENANT,
            _facts(),
            proposed_by_run=RunId("emp_not_a_run"),
            actor=_system(),
        )
    with pytest.raises(TransientError, match="没有活动目录提案策略"):
        await service.evaluate_cluster(
            TENANT, _facts(), proposed_by_run=RUN, actor=_system()
        )

    assert store.evaluations == {}
    assert store.proposals == {}


@pytest.mark.asyncio
async def test_evaluation_rejects_cross_tenant_locator_before_uow() -> None:
    store = _Store()
    service = _service(store)

    with pytest.raises(TenantIsolationViolation, match="跨租户"):
        await service.evaluate_cluster(
            TENANT,
            _facts(tenant_id=OTHER_TENANT),
            proposed_by_run=RunId("not_a_run"),
            actor=_system(),
        )

    assert store.enter_count == 0
    assert store.run_guard_count == 0


@pytest.mark.asyncio
async def test_evaluation_requires_persisted_trusted_run_before_writes() -> None:
    store = _Store()
    policy = _active_policy()
    store.policies[policy.policy_version_id] = policy
    store.run_guard_error = ValidationError("目录评估 Run 不可信")
    service = _service(store)

    with pytest.raises(ValidationError, match="^目录评估 Run 不可信$"):
        await service.evaluate_cluster(
            TENANT, _facts(), proposed_by_run=RUN, actor=_system()
        )

    assert store.run_guard_count == 1
    assert store.evaluations == {}
    assert store.proposals == {}
    assert store.events == []


@pytest.mark.asyncio
async def test_passed_evaluation_copies_snapshot_and_creates_owned_proposal_event() -> (
    None
):
    store = _Store()
    policy = _active_policy()
    store.policies[policy.policy_version_id] = policy
    service = _service(store)
    facts = _facts()

    view = await service.evaluate_cluster(
        TENANT, facts, proposed_by_run=RUN, actor=_system()
    )

    evaluation = store.evaluations[view.evaluation_id]
    proposal = next(iter(store.proposals.values()))
    assert evaluation.facts == facts
    assert evaluation.policy_version_id == policy.policy_version_id
    assert proposal.evaluation_id == evaluation.evaluation_id
    assert proposal.owner_employee == policy.proposed_by
    assert proposal.proposed_by_run == RUN
    assert proposal.state is CatalogProductProposalState.AWAITING_APPROVAL_SUBMISSION
    assert store.namespace_lock_count == 1
    assert store.active_lock_count == 1
    assert store.run_guard_count == 1
    assert len(store.events) == 1
    event = store.events[0]
    assert isinstance(event, CatalogProductProposalCreated)
    assert event.proposal_id == proposal.proposal_id
    assert "facts" not in vars(event)

    store.policies[policy.policy_version_id] = replace(
        policy, state=CatalogProposalPolicyState.SUPERSEDED, terminal_at=NOW
    )
    historical = await service.get_evaluation(
        TENANT, evaluation.evaluation_id, actor=_reader()
    )
    assert historical.facts == facts
    assert historical.rule_results == evaluation.rule_results


@pytest.mark.asyncio
async def test_failed_and_damaged_evaluations_persist_without_proposal() -> None:
    store = _Store()
    policy = _active_policy()
    store.policies[policy.policy_version_id] = policy
    service = _service(store)

    failed = await service.evaluate_cluster(
        TENANT, _facts(accounts=2), proposed_by_run=RUN, actor=_system()
    )
    assert failed.overall_passed is False
    assert failed.blocked_reason is None
    assert store.proposals == {}

    damaged = _facts(facts_hash=HASH_C).model_copy(update={"member_count": 99})
    blocked = await service.evaluate_cluster(
        TENANT,
        damaged,
        proposed_by_run=RunId("run_catalog_damaged"),
        actor=_system(),
    )
    persisted = store.evaluations[blocked.evaluation_id]
    assert blocked.blocked_reason == "catalog_facts_invalid"
    assert isinstance(persisted.facts, CatalogBlockedFactsInput)
    assert persisted.facts.model_dump() == {
        "tenant_id": TENANT,
        "cluster_id": "ncl_catalog_unit",
        "facts_hash": HASH_C,
    }
    assert all(item.actual_value is None for item in blocked.rule_results)
    assert store.proposals == {}


@pytest.mark.asyncio
async def test_evaluation_replay_is_exact_and_mismatched_run_conflicts() -> None:
    store = _Store()
    policy = _active_policy()
    store.policies[policy.policy_version_id] = policy
    service = _service(store)

    first = await service.evaluate_cluster(
        TENANT, _facts(), proposed_by_run=RUN, actor=_system()
    )
    replay = await service.evaluate_cluster(
        TENANT, _facts(), proposed_by_run=RUN, actor=_system()
    )
    assert replay == first
    assert len(store.evaluations) == 1
    assert len(store.proposals) == 1
    assert len(store.events) == 1

    with pytest.raises(CatalogEvaluationConflictError):
        await service.evaluate_cluster(
            TENANT,
            _facts(),
            proposed_by_run=RunId("run_catalog_other"),
            actor=_system(),
        )
    with pytest.raises(CatalogEvaluationConflictError):
        await service.evaluate_cluster(
            TENANT,
            _facts().model_copy(update={"display_codes": ("different_display",)}),
            proposed_by_run=RUN,
            actor=_system(),
        )


@pytest.mark.asyncio
async def test_binding_requires_atomic_exact_approval_and_exact_replay() -> None:
    store = _Store()
    service, proposal = await _evaluate(store)
    approval = _approval_fact(proposal)
    store.approvals[approval.approval_id] = approval

    first = await service.bind_proposal_approval(
        TENANT,
        proposal.proposal_id,
        approval.approval_id,
        HASH_C,
        actor=_system(),
    )
    replay = await service.bind_proposal_approval(
        TENANT,
        proposal.proposal_id,
        approval.approval_id,
        HASH_C,
        actor=_system(),
    )
    assert replay == first
    assert first.state == "pending_review"
    persisted = store.proposals[proposal.proposal_id]

    with pytest.raises(CatalogProposalApprovalConflictError):
        await service.bind_proposal_approval(
            TENANT,
            proposal.proposal_id,
            approval.approval_id,
            HASH_B,
            actor=_system(),
        )
    assert store.proposals[proposal.proposal_id] == persisted


@pytest.mark.parametrize(
    ("decision_state", "expected_state"),
    [("rejected", "rejected"), ("expired", "expired")],
)
@pytest.mark.asyncio
async def test_rejected_and_expired_decisions_have_exact_terminal_state(
    decision_state: str, expected_state: str
) -> None:
    store = _Store()
    service, proposal = await _evaluate(store)
    bound = await _bind(store, service, proposal)

    result = await service.apply_cultivation_decision(
        TENANT,
        bound.proposal_id,
        _decision(bound, decision_state),
        _facts(),
        actor=_system(),
    )
    assert result.state == expected_state
    assert store.cases == {}
    assert not any(isinstance(item, CatalogCultivationQueued) for item in store.events)

    with pytest.raises(CatalogProposalStateTransitionError):
        await service.apply_cultivation_decision(
            TENANT,
            bound.proposal_id,
            _decision(bound, "approved"),
            _facts(),
            actor=_system(),
        )


@pytest.mark.asyncio
async def test_stale_policy_or_facts_closes_proposal_without_case() -> None:
    for stale_kind in ("policy", "facts", "damaged_facts"):
        store = _Store()
        service, proposal = await _evaluate(store)
        bound = await _bind(store, service, proposal)
        current_facts = _facts()
        if stale_kind == "policy":
            old = store.policies[bound.policy_version_id]
            store.policies[old.policy_version_id] = replace(
                old, state=CatalogProposalPolicyState.SUPERSEDED, terminal_at=NOW
            )
            replacement = _active_policy("replacement")
            store.policies[replacement.policy_version_id] = replacement
        elif stale_kind == "facts":
            current_facts = _facts(facts_hash=HASH_B)
        else:
            current_facts = _facts().model_copy(update={"member_count": 99})

        if stale_kind == "damaged_facts":
            before_io = store.enter_count
            with pytest.raises(ValidationError):
                await service.apply_cultivation_decision(
                    TENANT,
                    bound.proposal_id,
                    _decision(bound),
                    current_facts,
                    actor=_system(),
                )
            assert store.enter_count == before_io
        else:
            result = await service.apply_cultivation_decision(
                TENANT,
                bound.proposal_id,
                _decision(bound),
                current_facts,
                actor=_system(),
            )
            assert result.state == "stale"
        assert store.cases == {}


def _changed_decision_facts(marker: str) -> CatalogClusterFactsInput:
    facts = _facts()
    changes: dict[str, object] = {
        "cluster_category": "electric_three_wheelers",
        "member_need_ids": ("need_1", "need_2", "need_other"),
        "distinct_account_ids": ("acc_1", "acc_2", "acc_other"),
        "member_count": 4,
        "distinct_account_count": 2,
        "known_country_codes": ("UG",),
        "unknown_country_account_count": 1,
        "recurring_counts": 1,
        "quantity_unit_covered_account_count": 1,
        "unified_unit": "units",
        "safe_total_quantity": 30,
        "evidence_summaries": (_evidence("other"),),
    }
    if marker == "member_count":
        return facts.model_copy(
            update={
                "member_need_ids": ("need_1", "need_2", "need_3", "need_4"),
                "member_count": 4,
            }
        )
    if marker == "distinct_account_count":
        return facts.model_copy(
            update={
                "distinct_account_ids": ("acc_1", "acc_2"),
                "distinct_account_count": 2,
                "unknown_country_account_count": 1,
                "recurring_unknown_account_count": 2,
            }
        )
    if marker == "recurring_counts":
        return facts.model_copy(
            update={
                "recurring_true_account_count": 1,
                "recurring_unknown_account_count": 2,
            }
        )
    if marker == "safe_total_quantity":
        return facts.model_copy(
            update={
                "quantity_unit_covered_account_count": 3,
                "unified_unit": "units",
                "safe_total_quantity": 30,
            }
        )
    return facts.model_copy(update={marker: changes[marker]})


@pytest.mark.parametrize(
    "changed_field",
    [
        "cluster_category",
        "member_need_ids",
        "distinct_account_ids",
        "member_count",
        "distinct_account_count",
        "known_country_codes",
        "unknown_country_account_count",
        "recurring_counts",
        "quantity_unit_covered_account_count",
        "unified_unit",
        "safe_total_quantity",
        "evidence_summaries",
    ],
)
@pytest.mark.asyncio
async def test_hash_covered_current_facts_must_match_historical_snapshot(
    changed_field: str,
) -> None:
    store = _Store()
    service, proposal = await _evaluate(store)
    bound = await _bind(store, service, proposal)

    result = await service.apply_cultivation_decision(
        TENANT,
        bound.proposal_id,
        _decision(bound),
        _changed_decision_facts(changed_field),
        actor=_system(),
    )

    assert result.state == "stale"
    assert store.cases == {}
    assert not any(isinstance(item, CatalogCultivationQueued) for item in store.events)


@pytest.mark.asyncio
async def test_display_only_current_facts_changes_do_not_stale_proposal() -> None:
    store = _Store()
    service, proposal = await _evaluate(store)
    bound = await _bind(store, service, proposal)
    display_only = _facts().model_copy(
        update={
            "display_codes": ("display_changed",),
            "facts_observed_at": NOW,
        }
    )

    result = await service.apply_cultivation_decision(
        TENANT,
        bound.proposal_id,
        _decision(bound),
        display_only,
        actor=_system(),
    )

    assert result.state == "cultivation_queued"
    assert len(store.cases) == 1


@pytest.mark.asyncio
async def test_proposal_with_corrupt_non_full_evaluation_is_fixed_conflict() -> None:
    store = _Store()
    service, proposal = await _evaluate(store)
    bound = await _bind(store, service, proposal)
    evaluation = store.evaluations[bound.evaluation_id]
    object.__setattr__(
        evaluation,
        "facts",
        CatalogBlockedFactsInput(
            tenant_id=TENANT,
            cluster_id=bound.cluster_id,
            facts_hash=bound.facts_hash,
        ),
    )

    with pytest.raises(CatalogCultivationConflictError):
        await service.apply_cultivation_decision(
            TENANT,
            bound.proposal_id,
            _decision(bound),
            _facts(),
            actor=_system(),
        )

    assert store.proposals[bound.proposal_id] == bound
    assert store.cases == {}


@pytest.mark.parametrize(
    ("facts", "expected_error", "expected_uow_entries"),
    [
        (_facts(tenant_id=OTHER_TENANT), TenantIsolationViolation, 0),
        (
            _facts().model_copy(update={"cluster_id": NeedClusterId("ncl_other")}),
            ValidationError,
            1,
        ),
    ],
)
@pytest.mark.asyncio
async def test_apply_rejects_foreign_locator_without_mutation(
    facts: CatalogClusterFactsInput,
    expected_error: type[Exception],
    expected_uow_entries: int,
) -> None:
    store = _Store()
    service, proposal = await _evaluate(store)
    bound = await _bind(store, service, proposal)
    before_io = store.enter_count

    with pytest.raises(expected_error):
        await service.apply_cultivation_decision(
            TENANT,
            bound.proposal_id,
            _decision(bound),
            facts,
            actor=_system(),
        )

    assert store.enter_count == before_io + expected_uow_entries
    assert store.proposals[bound.proposal_id] == bound
    assert store.cases == {}


@pytest.mark.asyncio
async def test_apply_foreign_tenant_precedes_untrusted_decision() -> None:
    store = _Store()
    service = _service(store)

    with pytest.raises(TenantIsolationViolation, match="跨租户"):
        await service.apply_cultivation_decision(
            TENANT,
            CatalogProductProposalId("cpr_missing"),
            CatalogApprovalDecisionInput.model_construct(),
            _facts(tenant_id=OTHER_TENANT),
            actor=_system(),
        )

    assert store.enter_count == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"request_hash": HASH_B},
        {"approval_id": ApprovalId("apr_wrong")},
        {"change_set_ref": f"catalog-cultivation:cpr_wrong:cpv_wrong:{HASH_A}"},
        {"proposed_by_run": RunId("run_wrong")},
        {"owner_employee": REVIEWER},
        {
            "approval_type": "catalog_proposal_policy_change",
            "contract_namespace": "catalog-policy-v1",
        },
    ],
)
@pytest.mark.asyncio
async def test_wrong_decision_subject_is_conflict_and_does_not_mutate(
    changes: dict[str, object],
) -> None:
    store = _Store()
    service, proposal = await _evaluate(store)
    bound = await _bind(store, service, proposal)
    wrong = _decision(bound).model_copy(update=changes)

    with pytest.raises(CatalogProposalDecisionInvalidError):
        await service.apply_cultivation_decision(
            TENANT, bound.proposal_id, wrong, _facts(), actor=_system()
        )
    assert store.proposals[bound.proposal_id] == bound
    assert store.cases == {}


@pytest.mark.asyncio
async def test_approved_current_subject_queues_once_and_recovers_response_loss() -> (
    None
):
    store = _Store()
    service, proposal = await _evaluate(store)
    bound = await _bind(store, service, proposal)
    decision = _decision(bound)

    first = await service.apply_cultivation_decision(
        TENANT, bound.proposal_id, decision, _facts(), actor=_system()
    )
    cultivation = next(iter(store.cases.values()))
    assert first.state == "cultivation_queued"
    assert cultivation.proposal_id == bound.proposal_id
    assert cultivation.approval_id == bound.approval_id
    assert cultivation.evidence_refs == ("msg_catalog_1",)
    assert (
        len(
            [
                item
                for item in store.events
                if isinstance(item, CatalogCultivationQueued)
            ]
        )
        == 1
    )

    replay = await service.apply_cultivation_decision(
        TENANT,
        bound.proposal_id,
        decision,
        _facts(facts_hash=HASH_B),
        actor=_system(),
    )
    assert replay == first
    assert len(store.cases) == 1
    assert (
        len(
            [
                item
                for item in store.events
                if isinstance(item, CatalogCultivationQueued)
            ]
        )
        == 1
    )

    detail = await service.get_cultivation_case(
        TENANT, cultivation.cultivation_case_id, actor=_reader()
    )
    queue = await service.list_cultivation_cases(TENANT, actor=_reader(), limit=10)
    assert detail == queue[0]
    assert "request_hash" not in detail.model_dump()


@pytest.mark.asyncio
async def test_safe_reads_limits_missing_and_storage_errors_are_redacted() -> None:
    store = _Store()
    service, proposal = await _evaluate(store)
    evaluation = next(iter(store.evaluations.values()))
    evaluations = await service.list_evaluations(TENANT, actor=_reader(), limit=10)
    proposals = await service.list_proposals(TENANT, actor=_reader(), limit=10)
    assert evaluations[0].evaluation_id == evaluation.evaluation_id
    assert proposals[0].proposal_id == proposal.proposal_id
    assert "approval_request_hash" not in proposals[0].model_dump()

    for limit in (True, 0, 201, "10"):
        with pytest.raises(ValidationError):
            await service.list_evaluations(
                TENANT,
                actor=_reader(),
                limit=limit,  # type: ignore[arg-type]
            )
        with pytest.raises(ValidationError):
            await service.list_proposals(
                TENANT,
                actor=_reader(),
                limit=limit,  # type: ignore[arg-type]
            )
        with pytest.raises(ValidationError):
            await service.list_cultivation_cases(
                TENANT,
                actor=_reader(),
                limit=limit,  # type: ignore[arg-type]
            )

    with pytest.raises(CatalogEvaluationNotFoundError):
        await service.get_evaluation(
            TENANT, CatalogProposalEvaluationId("cpe_missing"), actor=_reader()
        )
    with pytest.raises(CatalogProposalNotFoundError):
        await service.get_proposal(
            TENANT, CatalogProductProposalId("cpr_missing"), actor=_reader()
        )
    with pytest.raises(CatalogCultivationCaseNotFoundError):
        await service.get_cultivation_case(
            TENANT, CatalogCultivationCaseId("ccc_missing"), actor=_reader()
        )

    store.read_error = RuntimeError("postgresql://secret@private/catalog")
    with pytest.raises(TransientError) as caught:
        await service.list_proposals(TENANT, actor=_reader(), limit=10)
    assert "secret" not in str(caught.value)
    assert "postgresql" not in str(caught.value)


@pytest.mark.asyncio
async def test_tenant_and_permissions_fail_before_business_io() -> None:
    store = _Store()
    service = _service(store)
    with pytest.raises(PermissionDenied):
        await service.list_proposals(OTHER_TENANT, actor=_reader(), limit=10)
    with pytest.raises(PermissionDenied):
        await service.apply_cultivation_decision(
            TENANT,
            CatalogProductProposalId("cpr_missing"),
            CatalogApprovalDecisionInput.model_construct(),
            _facts(),
            actor=ProductActor(str(REVIEWER), ProductRole.BOSS, TENANT),
        )
    assert store.enter_count == 0
