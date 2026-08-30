"""Sourcing V2 领域服务的状态、门槛、授权与原子性合同。"""

from __future__ import annotations

import copy
import importlib
import traceback
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import Any, Self

import pytest

from domains.sourcing.errors import (
    MissingEvidenceSnapshotError,
    SourcingPlanStaleError,
    SourcingThresholdNotMetError,
)
from domains.sourcing.permissions import (
    Phase2SourcingAuthorizer,
    SourcingActor,
    SourcingScope,
)
from domains.sourcing.schemas import (
    CandidateSubmission,
    IndicativePriceTier,
    NeedFact,
    OpenSourcingCase,
    PublicSourcingPlanCommand,
    SourcingMatchInference,
    SourcingNeedSnapshot,
    SourcingObservedFact,
    SourcingReviewCommand,
    SourcingSupplierClaim,
    SpecComparisonView,
)
from domains.sourcing.service import (
    CandidateEvidenceSnapshot,
    CandidateEvidenceSnapshotReader,
    SourcingService,
)
from shared.errors import InvalidStateTransition, PermissionDenied, ValidationError
from shared.events.catalog import (
    SourcingCandidatesReady,
    SourcingCandidatesVerified,
    SourcingCaseHandedToCosting,
    SourcingCaseOpened,
)
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    OpportunityId,
    ProductId,
    SourcingCaseId,
    SourcingPlanId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.provenance import ProvenanceSummary, SourceType

NOW = datetime(2026, 8, 30, 10, tzinfo=UTC)
TENANT = TenantId("tenant-sourcing-service")
OTHER_TENANT = TenantId("tenant-other")
SYSTEM = SourcingActor("system-worker", TENANT, SourcingScope.SYSTEM, "system")
BOSS = SourcingActor("emp-boss", TENANT, SourcingScope.TENANT, "boss")
SOURCING = SourcingActor("emp-sourcing", TENANT, SourcingScope.TENANT, "sourcing")

_models = importlib.import_module("domains.sourcing.models")
CaseState = _models.CaseState
LadderCheck = _models.LadderCheck
LadderOutcome = _models.LadderOutcome
MatchLadderRung = _models.MatchLadderRung
PriceRejectionReason = _models.PriceRejectionReason
SourcingSupplyOption = _models.SourcingSupplyOption
SupplyOptionSource = _models.SupplyOptionSource


def _service_type() -> type[Any]:
    try:
        return importlib.import_module(
            "domains.sourcing.service_impl"
        ).SourcingServiceImpl
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：SourcingServiceImpl 尚未实现（{exc}）")


def _provenance() -> ProvenanceSummary:
    return ProvenanceSummary(
        source_type=SourceType.CONVERSATION,
        source_id="msg-need-1",
        extracted_by="human",
        extracted_at=NOW,
        confirmed_by=EmployeeId("emp-boss"),
        confirmed_at=NOW,
    )


def _open_command(*, completeness: int = 3, quantity: int = 5000) -> OpenSourcingCase:
    need_id = ValidatedNeedId("need-service-1")
    return OpenSourcingCase(
        need=SourcingNeedSnapshot(
            need_id=need_id,
            completeness=completeness,
            derivation_version="need-completeness-v1",
            product_category=NeedFact(value="hinges", provenance=_provenance()),
            quantity=NeedFact(value=quantity, provenance=_provenance()),
            snapshot_hash="a" * 64,
        ),
        trigger_key=f"sourcing-case:v2:{TENANT}:{need_id}",
    )


def _check(
    case_id: SourcingCaseId,
    rung: int,
    *,
    outcome: Any = None,
) -> Any:
    return LadderCheck(
        check_id=f"slc-{rung}",
        tenant_id=TENANT,
        case_id=case_id,
        sequence_number=rung,
        rung=MatchLadderRung(rung),
        outcome=outcome or LadderOutcome.NO_QUALIFIED_SUPPLY,
        input_snapshot={"category": "hinges"},
        input_snapshot_hash="b" * 64,
        conclusion="无合格供给",
        match_object_type=None,
        match_object_id=None,
        spec_comparisons=(),
        evidence_refs=(),
        checked_by=EmployeeId("untrusted-request-value"),
        checked_at=NOW,
    )


def _plan(case_id: SourcingCaseId, version: int) -> PublicSourcingPlanCommand:
    return PublicSourcingPlanCommand(
        plan_id=SourcingPlanId(f"spl-{version}"),
        case_id=case_id,
        target_countries=("US",),
        product_category="hinges",
        queries=(f"hinge factory v{version}",),
        max_search_queries=1,
        max_pages_read=3,
        provider="tavily",
        search_depth="basic",
        usage_credits_remaining=100,
        worst_case_credits=1,
        version=version,
        expected_case_version=6,
    )


def _candidate(
    *, moq: int = 500, url: str = "https://factory.example/hinge"
) -> CandidateSubmission:
    specs = tuple(
        SpecComparisonView(
            spec_name=name,
            required=f"required-{name}",
            offered=f"offered-{name}",
            level="exact",
        )
        for name in ("product_type", "material", "size", "model")
    )
    artifact = ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0X")
    facts = {
        name: SourcingObservedFact(
            value=f"offered-{name}",
            provenance=ProvenanceSummary(
                source_type=SourceType.WEB_PAGE,
                source_id=f"page-{name}",
                extracted_by="human",
                extracted_at=NOW,
                confirmed_by=EmployeeId("emp-sourcing"),
                confirmed_at=NOW,
            ),
            evidence_ref=artifact,
        )
        for name in ("product_type", "material", "size", "model")
    }
    facts.update(
        {
            "moq": SourcingObservedFact(
                value=moq,
                provenance=_provenance(),
                evidence_ref=artifact,
            ),
            "price_unit": SourcingObservedFact(
                value="piece",
                provenance=_provenance(),
                evidence_ref=artifact,
            ),
            "currency": SourcingObservedFact(
                value="USD",
                provenance=_provenance(),
                evidence_ref=artifact,
            ),
        }
    )
    return CandidateSubmission(
        supplier_name="Factory A",
        product_title="Stainless hinge",
        source_platform="official_site",
        specs=specs,
        observed_facts=facts,
        match_inferences={
            "fit": SourcingMatchInference(
                value="四项均匹配",
                based_on=(artifact,),
                inferred_by="agent",
                inferred_at=NOW,
            )
        },
        indicative_price_tiers=(
            IndicativePriceTier(
                minimum_quantity=1000,
                amount=Decimal("1.25"),
                currency="USD",
                unit="piece",
                provenance=_provenance(),
                evidence_ref=artifact,
            ),
        ),
        moq=moq,
        price_unit="piece",
        currency="USD",
        evidence_url=url,
        evidence_hash="c" * 64,
        evidence_artifact_ref=str(artifact),
    )


class _MemoryRepo:
    def __init__(self, state: dict[str, Any], name: str) -> None:
        self.state = state
        self.name = name


class _Cases(_MemoryRepo):
    async def get_or_create(
        self, tenant_id: TenantId, case: Any
    ) -> tuple[Any, bool]:
        existing = await self.get_by_trigger(tenant_id, case.trigger_key)
        if existing is not None:
            return existing, False
        await self.add(tenant_id, case)
        return copy.deepcopy(case), True

    async def add(self, tenant_id: TenantId, case: Any) -> None:
        self.state[self.name][(tenant_id, case.case_id)] = copy.deepcopy(case)

    async def get(self, tenant_id: TenantId, case_id: SourcingCaseId) -> Any | None:
        return copy.deepcopy(self.state[self.name].get((tenant_id, case_id)))

    async def update(self, tenant_id: TenantId, case: Any) -> None:
        key = (tenant_id, case.case_id)
        current = self.state[self.name].get(key)
        if current is None or current.version != case.version - 1:
            from domains.sourcing.errors import SourcingCaseConflictError

            raise SourcingCaseConflictError("stale")
        self.state[self.name][key] = copy.deepcopy(case)

    async def find_active_for_need(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> Any | None:
        for (candidate_tenant, _), case in self.state[self.name].items():
            if (
                candidate_tenant == tenant_id
                and case.need_id == need_id
                and case.state not in {CaseState.FAILED, CaseState.HANDED_TO_COSTING}
            ):
                return copy.deepcopy(case)
        return None

    async def get_by_trigger(self, tenant_id: TenantId, trigger_key: str) -> Any | None:
        for (candidate_tenant, _), case in self.state[self.name].items():
            if candidate_tenant == tenant_id and case.trigger_key == trigger_key:
                return copy.deepcopy(case)
        return None


class _Checks(_MemoryRepo):
    async def add(self, tenant_id: TenantId, check: Any) -> None:
        self.state[self.name].append(copy.deepcopy(check))

    async def list_for_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> list[Any]:
        return [
            copy.deepcopy(item)
            for item in self.state[self.name]
            if item.tenant_id == tenant_id and item.case_id == case_id
        ]


class _Plans(_MemoryRepo):
    async def add(self, tenant_id: TenantId, plan: Any) -> None:
        self.state[self.name][(tenant_id, plan.plan_id)] = copy.deepcopy(plan)

    async def get(self, tenant_id: TenantId, plan_id: SourcingPlanId) -> Any | None:
        return copy.deepcopy(self.state[self.name].get((tenant_id, plan_id)))

    async def update(self, tenant_id: TenantId, plan: Any) -> None:
        self.state[self.name][(tenant_id, plan.plan_id)] = copy.deepcopy(plan)

    async def get_active_for_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> Any | None:
        values = [
            plan
            for (candidate_tenant, _), plan in self.state[self.name].items()
            if candidate_tenant == tenant_id
            and plan.case_id == case_id
            and plan.status.value in {"pending_confirmation", "authorized", "running"}
        ]
        return (
            copy.deepcopy(max(values, key=lambda item: item.version))
            if values
            else None
        )


class _Candidates(_MemoryRepo):
    async def add(self, tenant_id: TenantId, candidate: Any) -> None:
        self.state[self.name][(tenant_id, candidate.candidate_id)] = copy.deepcopy(
            candidate
        )

    async def get(
        self, tenant_id: TenantId, candidate_id: SupplierCandidateId
    ) -> Any | None:
        return copy.deepcopy(self.state[self.name].get((tenant_id, candidate_id)))

    async def list_for_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId, include_rejected: bool
    ) -> list[Any]:
        values = [
            item
            for (candidate_tenant, _), item in self.state[self.name].items()
            if candidate_tenant == tenant_id and item.case_id == case_id
        ]
        return [
            copy.deepcopy(item)
            for item in values
            if include_rejected or not item.rejected
        ]

    async def count_qualified(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> int:
        return sum(
            item.passes_verification()[0]
            for item in await self.list_for_case(tenant_id, case_id, False)
        )


class _Options(_MemoryRepo):
    async def get_or_create_supplier_candidate(
        self, tenant_id: TenantId, option: Any
    ) -> tuple[Any, bool]:
        for (candidate_tenant, _), current in self.state[self.name].items():
            if (
                candidate_tenant == tenant_id
                and current.case_id == option.case_id
                and current.supplier_candidate_id == option.supplier_candidate_id
            ):
                return copy.deepcopy(current), False
        await self.add(tenant_id, option)
        return copy.deepcopy(option), True

    async def add(self, tenant_id: TenantId, option: Any) -> None:
        self.state[self.name][(tenant_id, option.option_id)] = copy.deepcopy(option)

    async def get(
        self, tenant_id: TenantId, option_id: SourcingSupplyOptionId
    ) -> Any | None:
        return copy.deepcopy(self.state[self.name].get((tenant_id, option_id)))

    async def list_for_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> list[Any]:
        return [
            copy.deepcopy(item)
            for (candidate_tenant, _), item in self.state[self.name].items()
            if candidate_tenant == tenant_id and item.case_id == case_id
        ]


class _Reviews(_MemoryRepo):
    async def add(self, tenant_id: TenantId, review: Any) -> None:
        self.state[self.name][(tenant_id, review.review_id)] = copy.deepcopy(review)

    async def get(self, tenant_id: TenantId, review_id: Any) -> Any | None:
        return copy.deepcopy(self.state[self.name].get((tenant_id, review_id)))

    async def get_for_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> Any | None:
        for (candidate_tenant, _), review in self.state[self.name].items():
            if candidate_tenant == tenant_id and review.case_id == case_id:
                return copy.deepcopy(review)
        return None

    async def update(self, tenant_id: TenantId, review: Any) -> None:
        self.state[self.name][(tenant_id, review.review_id)] = copy.deepcopy(review)


class _Handoffs:
    def __init__(self, uow: _MemoryUow) -> None:
        self.uow = uow

    async def get_snapshot(
        self, tenant_id: TenantId, case_id: SourcingCaseId, review_id: Any
    ) -> Any | None:
        case = await self.uow.cases.get(tenant_id, case_id)
        review = await self.uow.reviews.get(tenant_id, review_id)
        if (
            case is None
            or review is None
            or case.state is not CaseState.HANDED_TO_COSTING
            or case.version != review.expected_case_version + 1
        ):
            return None
        return {
            "case_id": case_id,
            "review_id": review_id,
            "opportunity_id": case.opportunity_id,
        }


class _Bus:
    def __init__(self, state: dict[str, Any], *, fail: bool = False) -> None:
        self.state = state
        self.fail = fail

    async def publish(self, event: Any) -> None:
        if self.fail:
            raise RuntimeError("outbox unavailable")
        self.state["events"].append(event)


class _MemoryUow:
    def __init__(self, state: dict[str, Any], *, bus_fails: bool = False) -> None:
        self.state = state
        self.bus_fails = bus_fails
        self.cases = _Cases(state, "cases")
        self.checks = _Checks(state, "checks")
        self.plans = _Plans(state, "plans")
        self.candidates = _Candidates(state, "candidates")
        self.options = _Options(state, "options")
        self.reviews = _Reviews(state, "reviews")
        self.handoffs = _Handoffs(self)
        self.bus = _Bus(state, fail=bus_fails)

    async def __aenter__(self) -> Self:
        self._before = copy.deepcopy(self.state)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is not None:
            self.state.clear()
            self.state.update(self._before)


class _Factory:
    def __init__(self, *, bus_fails: bool = False) -> None:
        self.calls = 0
        self.bus_fails = bus_fails
        self.state: dict[str, Any] = {
            "cases": {},
            "checks": [],
            "plans": {},
            "candidates": {},
            "options": {},
            "reviews": {},
            "events": [],
        }

    def __call__(self, tenant_id: TenantId) -> _MemoryUow:
        self.calls += 1
        return _MemoryUow(self.state, bus_fails=self.bus_fails)


class _EvidenceReader:
    def __init__(
        self,
        *,
        projection: CandidateEvidenceSnapshot | None = None,
        failure: BaseException | None = None,
    ) -> None:
        self.calls = 0
        self.failure = failure
        self.projection = projection or CandidateEvidenceSnapshot(
            tenant_id=TENANT,
            artifact_id=ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0X"),
            canonical_url="https://factory.example/hinge",
            content_hash="c" * 64,
            observed_at=NOW - timedelta(hours=2),
        )

    async def read_verified(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> CandidateEvidenceSnapshot:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.projection


def _service(
    factory: _Factory,
    evidence_reader: CandidateEvidenceSnapshotReader | None = None,
) -> Any:
    return _service_type()(
        factory,
        Phase2SourcingAuthorizer(TENANT),
        evidence_reader or _EvidenceReader(),
        now=lambda: NOW,
    )


def test_concrete_service_preserves_the_public_protocol_surface() -> None:
    assert isinstance(_service(_Factory()), SourcingService)


async def _opened(service: Any) -> SourcingCaseId:
    return await service.open_case(TENANT, _open_command(), actor=SYSTEM)


async def _discovering(service: Any) -> SourcingCaseId:
    case_id = await _opened(service)
    for rung in range(1, 6):
        await service.record_ladder_check(
            TENANT, case_id, _check(case_id, rung), actor=SYSTEM
        )
    return case_id


@pytest.mark.asyncio
async def test_open_case_requires_level_three_and_is_idempotent_per_tenant() -> None:
    factory = _Factory()
    service = _service(factory)
    with pytest.raises(SourcingThresholdNotMetError):
        await service.open_case(TENANT, _open_command(completeness=2), actor=SYSTEM)
    first = await service.open_case(TENANT, _open_command(), actor=SYSTEM)
    second = await service.open_case(TENANT, _open_command(), actor=SYSTEM)
    assert first == second
    assert len(factory.state["cases"]) == 1
    assert isinstance(factory.state["events"][0], SourcingCaseOpened)


@pytest.mark.asyncio
async def test_open_case_requires_service_derived_exact_trigger_key() -> None:
    factory = _Factory()
    service = _service(factory)
    command = _open_command().model_copy(update={"trigger_key": "caller:key"})
    with pytest.raises(ValidationError):
        await service.open_case(TENANT, command, actor=SYSTEM)
    assert factory.calls == 0


@pytest.mark.asyncio
async def test_open_case_remains_idempotent_after_existing_case_is_terminal() -> None:
    factory = _Factory()
    service = _service(factory)
    first = await service.open_case(TENANT, _open_command(), actor=SYSTEM)
    stored = factory.state["cases"][(TENANT, first)]
    stored.state = CaseState.FAILED
    stored.failed_reason = "no_qualified_candidate"
    second = await service.open_case(TENANT, _open_command(), actor=SYSTEM)
    assert second == first
    assert len(factory.state["events"]) == 1


@pytest.mark.asyncio
async def test_duplicate_open_keeps_original_snapshot_when_need_has_later_updates() -> (
    None
):
    factory = _Factory()
    service = _service(factory)
    first = await service.open_case(TENANT, _open_command(quantity=5000), actor=SYSTEM)
    updated = _open_command(quantity=6000).model_copy(
        update={
            "need": _open_command(quantity=6000).need.model_copy(
                update={"snapshot_hash": "f" * 64}
            )
        }
    )
    second = await service.open_case(TENANT, updated, actor=SYSTEM)
    assert second == first
    assert factory.state["cases"][(TENANT, first)].need_snapshot.quantity.value == 5000


@pytest.mark.asyncio
async def test_every_public_write_is_authorizer_first() -> None:
    factory = _Factory()
    reader = _EvidenceReader()
    service = _service(factory, reader)
    case_id = SourcingCaseId("src-never-read")
    calls = [
        lambda: service.open_case(OTHER_TENANT, _open_command(), actor=SYSTEM),
        lambda: service.record_ladder_check(
            OTHER_TENANT, case_id, _check(case_id, 1), actor=SYSTEM
        ),
        lambda: service.save_public_plan(
            OTHER_TENANT, case_id, _plan(case_id, 1), actor=BOSS
        ),
        lambda: service.confirm_public_plan(
            OTHER_TENANT, SourcingPlanId("spl-1"), "a" * 64, actor=BOSS
        ),
        lambda: service.submit_candidate(
            OTHER_TENANT, case_id, _candidate(), actor=SOURCING
        ),
        lambda: service.mark_candidates_verified(
            OTHER_TENANT, case_id, (SupplierCandidateId("spc-1"),), actor=SYSTEM
        ),
        lambda: service.register_supplier_candidate_option(
            OTHER_TENANT,
            case_id,
            SupplierCandidateId("spc-1"),
            ProductId("prd-1"),
            actor=SYSTEM,
        ),
        lambda: service.mark_candidates_ready(
            OTHER_TENANT, case_id, (SourcingSupplyOptionId("sop-1"),), (), actor=SYSTEM
        ),
        lambda: service.review(
            OTHER_TENANT,
            case_id,
            SourcingReviewCommand(
                primary_option_id=SourcingSupplyOptionId("sop-1"),
                alternate_option_ids=(),
                reason="ok",
                expected_case_version=4,
            ),
            actor=SOURCING,
        ),
        lambda: service.hand_to_costing(
            OTHER_TENANT, case_id, OpportunityId("opp-1"), actor=SYSTEM
        ),
    ]
    for call in calls:
        with pytest.raises(PermissionDenied):
            await call()
    assert factory.calls == 0
    assert reader.calls == 0


@pytest.mark.asyncio
async def test_ladder_checks_are_contiguous_and_plan_hash_is_exact() -> None:
    service = _service(_Factory())
    case_id = await _opened(service)
    with pytest.raises(ValidationError):
        await service.record_ladder_check(
            TENANT, case_id, _check(case_id, 2), actor=SYSTEM
        )
    await service.record_ladder_check(TENANT, case_id, _check(case_id, 1), actor=SYSTEM)
    with pytest.raises(ValidationError):
        await service.record_ladder_check(
            TENANT, case_id, _check(case_id, 1), actor=SYSTEM
        )
    with pytest.raises(ValidationError):
        await service.save_public_plan(TENANT, case_id, _plan(case_id, 1), actor=BOSS)
    for rung in range(2, 6):
        await service.record_ladder_check(
            TENANT, case_id, _check(case_id, rung), actor=SYSTEM
        )
    plan = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    with pytest.raises(SourcingPlanStaleError):
        await service.confirm_public_plan(TENANT, plan.plan_id, "d" * 64, actor=BOSS)
    confirmed = await service.confirm_public_plan(
        TENANT, plan.plan_id, plan.plan_hash, actor=BOSS
    )
    assert confirmed.authorized_plan_hash == plan.plan_hash


@pytest.mark.asyncio
async def test_qualified_ladder_outcome_stops_later_rungs_and_public_search() -> None:
    service = _service(_Factory())
    case_id = await _opened(service)
    await service.record_ladder_check(
        TENANT,
        case_id,
        _check(
            case_id,
            1,
            outcome=LadderOutcome.QUALIFIED_SUPPLY_FOUND,
        ),
        actor=SYSTEM,
    )
    with pytest.raises(ValidationError, match="已找到合格供给"):
        await service.record_ladder_check(
            TENANT, case_id, _check(case_id, 2), actor=SYSTEM
        )
    with pytest.raises(ValidationError, match="无合格供给"):
        await service.save_public_plan(
            TENANT, case_id, _plan(case_id, 1), actor=BOSS
        )


@pytest.mark.asyncio
async def test_scope_change_creates_a_new_pending_plan_version() -> None:
    service = _service(_Factory())
    case_id = await _discovering(service)
    first = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    second = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 2), actor=BOSS
    )
    assert second.version == first.version + 1
    assert second.plan_id != first.plan_id
    assert second.plan_hash != first.plan_hash
    assert second.authorized_plan_hash is None


@pytest.mark.asyncio
async def test_superseded_public_plan_cannot_be_confirmed() -> None:
    service = _service(_Factory())
    case_id = await _discovering(service)
    first = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    await service.save_public_plan(TENANT, case_id, _plan(case_id, 2), actor=BOSS)
    with pytest.raises(SourcingPlanStaleError):
        await service.confirm_public_plan(
            TENANT, first.plan_id, first.plan_hash, actor=BOSS
        )


@pytest.mark.asyncio
async def test_candidate_checks_snapshot_moq_tier_and_persists_fourth_as_rejected() -> (
    None
):
    factory = _Factory()
    service = _service(factory)
    case_id = await _discovering(service)
    plan = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    await service.confirm_public_plan(TENANT, plan.plan_id, plan.plan_hash, actor=BOSS)
    with pytest.raises(MissingEvidenceSnapshotError):
        await service.submit_candidate(
            TENANT,
            case_id,
            _candidate(url="https://user:secret@factory.example"),
            actor=SOURCING,
        )
    rejected_id = await service.submit_candidate(
        TENANT, case_id, _candidate(moq=6000), actor=SOURCING
    )
    assert factory.state["candidates"][(TENANT, rejected_id)].rejected is True
    for _ in range(3):
        candidate_id = await service.submit_candidate(
            TENANT, case_id, _candidate(), actor=SOURCING
        )
        assert factory.state["candidates"][(TENANT, candidate_id)].rejected is False
    fourth_id = await service.submit_candidate(
        TENANT, case_id, _candidate(), actor=SOURCING
    )
    assert factory.state["candidates"][(TENANT, fourth_id)].rejected is True
    assert len(factory.state["candidates"]) == 5


@pytest.mark.asyncio
async def test_candidate_evidence_is_trusted_and_all_field_refs_are_bound() -> None:
    factory = _Factory()
    reader = _EvidenceReader()
    service = _service(factory, reader)
    case_id = await _discovering(service)
    plan = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    await service.confirm_public_plan(TENANT, plan.plan_id, plan.plan_hash, actor=BOSS)
    candidate_id = await service.submit_candidate(
        TENANT, case_id, _candidate(), actor=SOURCING
    )
    stored = factory.state["candidates"][(TENANT, candidate_id)]
    assert stored.evidence.observed_at == NOW - timedelta(hours=2)

    wrong_artifact = ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0Y")
    submission = _candidate()
    wrong_fact = next(iter(submission.observed_facts.values())).model_copy(
        update={"evidence_ref": wrong_artifact}
    )
    facts = dict(submission.observed_facts)
    facts["material"] = wrong_fact
    with pytest.raises(MissingEvidenceSnapshotError):
        await service.submit_candidate(
            TENANT,
            case_id,
            submission.model_copy(update={"observed_facts": facts}),
            actor=SOURCING,
        )
    wrong_claim = SourcingSupplierClaim(
        value="offered-material",
        provenance=_provenance(),
        evidence_ref=wrong_artifact,
    )
    with pytest.raises(MissingEvidenceSnapshotError):
        await service.submit_candidate(
            TENANT,
            case_id,
            submission.model_copy(
                update={"supplier_claims": {"material": wrong_claim}}
            ),
            actor=SOURCING,
        )
    wrong_inference = next(iter(submission.match_inferences.values())).model_copy(
        update={"based_on": (wrong_artifact,)}
    )
    with pytest.raises(MissingEvidenceSnapshotError):
        await service.submit_candidate(
            TENANT,
            case_id,
            submission.model_copy(
                update={"match_inferences": {"fit": wrong_inference}}
            ),
            actor=SOURCING,
        )


@pytest.mark.asyncio
async def test_candidate_evidence_reader_failure_or_mismatch_fails_closed() -> None:
    factory = _Factory()
    service = _service(factory, _EvidenceReader(failure=RuntimeError("secret")))
    case_id = await _discovering(service)
    plan = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    await service.confirm_public_plan(TENANT, plan.plan_id, plan.plan_hash, actor=BOSS)
    with pytest.raises(MissingEvidenceSnapshotError) as captured:
        await service.submit_candidate(TENANT, case_id, _candidate(), actor=SOURCING)
    rendered = "".join(traceback.format_exception(captured.value))
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "secret" not in rendered

    mismatch = CandidateEvidenceSnapshot(
        tenant_id=TENANT,
        artifact_id=ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0X"),
        canonical_url="https://factory.example/hinge",
        content_hash="d" * 64,
        observed_at=NOW,
    )
    service = _service(factory, _EvidenceReader(projection=mismatch))
    with pytest.raises(MissingEvidenceSnapshotError):
        await service.submit_candidate(TENANT, case_id, _candidate(), actor=SOURCING)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    ["spec_value_mismatch", "missing_moq_evidence", "wrong_tier_ref"],
)
async def test_candidate_decision_fields_must_match_trusted_evidence(
    mutation: str,
) -> None:
    factory = _Factory()
    service = _service(factory)
    case_id = await _discovering(service)
    plan = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    await service.confirm_public_plan(TENANT, plan.plan_id, plan.plan_hash, actor=BOSS)
    submission = _candidate()
    if mutation == "spec_value_mismatch":
        submission = submission.model_copy(
            update={
                "specs": tuple(
                    item.model_copy(update={"offered": "malicious-material"})
                    if item.spec_name == "material"
                    else item
                    for item in submission.specs
                )
            }
        )
    elif mutation == "missing_moq_evidence":
        facts = dict(submission.observed_facts)
        facts.pop("moq")
        submission = submission.model_copy(update={"observed_facts": facts})
    else:
        wrong_ref = ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0Y")
        tier = submission.indicative_price_tiers[0].model_copy(
            update={"evidence_ref": wrong_ref}
        )
        submission = submission.model_copy(
            update={"indicative_price_tiers": (tier,)}
        )

    if mutation == "wrong_tier_ref":
        with pytest.raises(MissingEvidenceSnapshotError):
            await service.submit_candidate(
                TENANT, case_id, submission, actor=SOURCING
            )
        return
    candidate_id = await service.submit_candidate(
        TENANT, case_id, submission, actor=SOURCING
    )
    stored = factory.state["candidates"][(TENANT, candidate_id)]
    assert stored.rejected is True
    assert PriceRejectionReason.VERIFICATION_INCOMPLETE in stored.rejection_reasons


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("canonical_url", "observed_at"),
    [
        ("https://user:secret@factory.example/hinge", NOW),
        ("https://factory.example/hinge", NOW.replace(tzinfo=None)),
    ],
)
async def test_candidate_evidence_reader_rejects_unsafe_or_naive_projection(
    canonical_url: str,
    observed_at: datetime,
) -> None:
    factory = _Factory()
    projection = CandidateEvidenceSnapshot(
        tenant_id=TENANT,
        artifact_id=ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0X"),
        canonical_url=canonical_url,
        content_hash="c" * 64,
        observed_at=observed_at,
    )
    service = _service(factory, _EvidenceReader(projection=projection))
    case_id = await _discovering(service)
    plan = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    await service.confirm_public_plan(TENANT, plan.plan_id, plan.plan_hash, actor=BOSS)
    uow_calls_before_submit = factory.calls
    with pytest.raises(MissingEvidenceSnapshotError):
        await service.submit_candidate(
            TENANT,
            case_id,
            _candidate(url=canonical_url),
            actor=SOURCING,
        )
    assert factory.calls == uow_calls_before_submit
    assert factory.state["candidates"] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "replacement",
    [
        SpecComparisonView(
            spec_name="material",
            required="required-material",
            offered="offered-material",
            level="different",
            substitutable=False,
        ),
        SpecComparisonView(
            spec_name="material",
            required="required-material",
            offered="offered-material",
            level="different",
            substitutable=True,
            substitution_impact="客户需接受替代材质",
            needs_customer_confirmation=True,
        ),
    ],
)
async def test_incompatible_or_unconfirmed_spec_cannot_qualify(
    replacement: SpecComparisonView,
) -> None:
    factory = _Factory()
    service = _service(factory)
    case_id = await _discovering(service)
    plan = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    await service.confirm_public_plan(TENANT, plan.plan_id, plan.plan_hash, actor=BOSS)
    submission = _candidate()
    submission = submission.model_copy(
        update={
            "specs": tuple(
                replacement if item.spec_name == "material" else item
                for item in submission.specs
            )
        }
    )
    candidate_id = await service.submit_candidate(
        TENANT, case_id, submission, actor=SOURCING
    )
    candidate = factory.state["candidates"][(TENANT, candidate_id)]
    assert candidate.rejected is True
    assert "verification_incomplete" in {
        reason.value for reason in candidate.rejection_reasons
    }


@pytest.mark.asyncio
async def test_substitution_with_customer_conversation_provenance_can_qualify() -> None:
    factory = _Factory()
    service = _service(factory)
    case_id = await _discovering(service)
    plan = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    await service.confirm_public_plan(TENANT, plan.plan_id, plan.plan_hash, actor=BOSS)
    confirmed_material = SpecComparisonView(
        spec_name="material",
        required="required-material",
        offered="offered-material",
        level="different",
        substitutable=True,
        substitution_impact="客户接受替代材质",
        needs_customer_confirmation=True,
        customer_confirmation=_provenance(),
    )
    submission = _candidate()
    submission = submission.model_copy(
        update={
            "specs": tuple(
                confirmed_material if item.spec_name == "material" else item
                for item in submission.specs
            )
        }
    )
    candidate_id = await service.submit_candidate(
        TENANT, case_id, submission, actor=SOURCING
    )
    assert factory.state["candidates"][(TENANT, candidate_id)].rejected is False


@pytest.mark.asyncio
async def test_mark_ready_publishes_exact_ids_and_rejects_empty_options() -> None:
    factory = _Factory()
    service = _service(factory)
    case_id = await _discovering(service)
    plan = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    await service.confirm_public_plan(TENANT, plan.plan_id, plan.plan_hash, actor=BOSS)
    with pytest.raises(ValidationError):
        await service.mark_candidates_ready(TENANT, case_id, (), (), actor=SYSTEM)
    option = SourcingSupplyOption(
        option_id=SourcingSupplyOptionId("sop-internal"),
        tenant_id=TENANT,
        case_id=case_id,
        source=SupplyOptionSource.EXISTING_PRODUCT,
        product_id=ProductId("prd-internal"),
        supplier_candidate_id=None,
        is_qualified=True,
        created_at=NOW,
    )
    _MemoryUow(factory.state).options.state["options"][(TENANT, option.option_id)] = (
        option
    )
    await service.mark_candidates_ready(
        TENANT, case_id, (option.option_id,), (), actor=SYSTEM
    )
    event = factory.state["events"][-1]
    assert isinstance(event, SourcingCandidatesReady)
    assert event.option_ids == (option.option_id,)
    assert event.candidate_ids == ()


@pytest.mark.asyncio
async def test_verified_candidates_require_cards_and_complete_frozen_ready_sets() -> None:
    factory = _Factory()
    service = _service(factory)
    case_id = await _discovering(service)
    plan = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    await service.confirm_public_plan(TENANT, plan.plan_id, plan.plan_hash, actor=BOSS)
    candidate_id = await service.submit_candidate(
        TENANT, case_id, _candidate(), actor=SOURCING
    )
    with pytest.raises(ValidationError):
        await service.mark_candidates_ready(
            TENANT, case_id, (), (candidate_id,), actor=SYSTEM
        )
    verified = await service.mark_candidates_verified(
        TENANT, case_id, (candidate_id,), actor=SYSTEM
    )
    assert isinstance(verified, SourcingCandidatesVerified)
    assert verified.candidate_ids == (candidate_id,)
    assert verified.case_version == factory.state["cases"][(TENANT, case_id)].version
    assert len(verified.candidate_set_hash) == 64
    assert factory.state["cases"][(TENANT, case_id)].state is CaseState.VERIFYING
    event_count = len(factory.state["events"])
    repeated_generation = await service.mark_candidates_verified(
        TENANT, case_id, (candidate_id,), actor=SYSTEM
    )
    assert repeated_generation == verified
    assert len(factory.state["events"]) == event_count
    with pytest.raises(InvalidStateTransition):
        await service.submit_candidate(
            TENANT, case_id, _candidate(), actor=SOURCING
        )

    product_id = ProductId("prd-card")
    option_id = await service.register_supplier_candidate_option(
        TENANT,
        case_id,
        candidate_id,
        product_id,
        expected_case_version=verified.case_version,
        expected_candidate_set_hash=verified.candidate_set_hash,
        actor=SYSTEM,
    )
    repeated = await service.register_supplier_candidate_option(
        TENANT,
        case_id,
        candidate_id,
        product_id,
        expected_case_version=verified.case_version,
        expected_candidate_set_hash=verified.candidate_set_hash,
        actor=SYSTEM,
    )
    assert repeated == option_id
    extra_option = SourcingSupplyOption(
        option_id=SourcingSupplyOptionId("sop-extra"),
        tenant_id=TENANT,
        case_id=case_id,
        source=SupplyOptionSource.EXISTING_PRODUCT,
        product_id=ProductId("prd-existing"),
        supplier_candidate_id=None,
        is_qualified=True,
        created_at=NOW,
    )
    factory.state["options"][(TENANT, extra_option.option_id)] = extra_option
    with pytest.raises(ValidationError):
        await service.mark_candidates_ready(
            TENANT, case_id, (option_id,), (candidate_id,), actor=SYSTEM
        )
    with pytest.raises(ValidationError):
        await service.mark_candidates_ready(
            TENANT,
            case_id,
            (option_id, extra_option.option_id),
            (),
            actor=SYSTEM,
        )
    await service.mark_candidates_ready(
        TENANT,
        case_id,
        (option_id, extra_option.option_id),
        (candidate_id,),
        expected_case_version=verified.case_version,
        expected_candidate_set_hash=verified.candidate_set_hash,
        actor=SYSTEM,
    )
    ready = factory.state["events"][-1]
    assert isinstance(ready, SourcingCandidatesReady)
    assert set(ready.option_ids) == {option_id, extra_option.option_id}
    assert ready.candidate_ids == (candidate_id,)
    assert await service.register_supplier_candidate_option(
        TENANT,
        case_id,
        candidate_id,
        product_id,
        expected_case_version=verified.case_version,
        expected_candidate_set_hash=verified.candidate_set_hash,
        actor=SYSTEM,
    ) == option_id


@pytest.mark.asyncio
async def test_candidate_projection_rejects_stale_generation() -> None:
    factory = _Factory()
    service = _service(factory)
    case_id = await _discovering(service)
    plan = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    await service.confirm_public_plan(TENANT, plan.plan_id, plan.plan_hash, actor=BOSS)
    candidate_id = await service.submit_candidate(
        TENANT, case_id, _candidate(), actor=SOURCING
    )
    verified = await service.mark_candidates_verified(
        TENANT, case_id, (candidate_id,), actor=SYSTEM
    )
    with pytest.raises(ValidationError):
        await service.register_supplier_candidate_option(
            TENANT,
            case_id,
            candidate_id,
            ProductId("prd-stale"),
            expected_case_version=verified.case_version - 1,
            expected_candidate_set_hash="0" * 64,
            actor=SYSTEM,
        )


@pytest.mark.asyncio
async def test_review_and_handoff_require_qualified_selection_and_confirmed_review() -> (
    None
):
    factory = _Factory()
    service = _service(factory)
    case_id = await _discovering(service)
    plan = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    await service.confirm_public_plan(TENANT, plan.plan_id, plan.plan_hash, actor=BOSS)
    option = SourcingSupplyOption(
        option_id=SourcingSupplyOptionId("sop-main"),
        tenant_id=TENANT,
        case_id=case_id,
        source=SupplyOptionSource.EXISTING_PRODUCT,
        product_id=ProductId("prd-main"),
        supplier_candidate_id=None,
        is_qualified=True,
        created_at=NOW,
    )
    factory.state["options"][(TENANT, option.option_id)] = option
    await service.mark_candidates_ready(
        TENANT, case_id, (option.option_id,), (), actor=SYSTEM
    )
    case = next(iter(factory.state["cases"].values()))
    review = await service.review(
        TENANT,
        case_id,
        SourcingReviewCommand(
            primary_option_id=option.option_id,
            alternate_option_ids=(),
            reason="证据最完整",
            expected_case_version=case.version,
        ),
        actor=SOURCING,
    )
    with pytest.raises(ValidationError):
        await service.hand_to_costing(
            TENANT, case_id, OpportunityId("opp-real"), actor=SYSTEM
        )
    await service.confirm_review(TENANT, review.review_id, actor=BOSS)
    snapshot = await service.hand_to_costing(
        TENANT, case_id, OpportunityId("opp-real"), actor=SYSTEM
    )
    handed = next(iter(factory.state["cases"].values()))
    assert handed.state is CaseState.HANDED_TO_COSTING
    assert handed.version == review.expected_case_version + 1
    assert snapshot["opportunity_id"] == OpportunityId("opp-real")
    assert isinstance(factory.state["events"][-1], SourcingCaseHandedToCosting)
    with pytest.raises(InvalidStateTransition):
        await service.hand_to_costing(
            TENANT, case_id, OpportunityId("opp-real"), actor=SYSTEM
        )


@pytest.mark.asyncio
async def test_outbox_failure_rolls_back_case_creation_and_ready_transition() -> None:
    factory = _Factory(bus_fails=True)
    service = _service(factory)
    with pytest.raises(RuntimeError, match="outbox unavailable"):
        await service.open_case(TENANT, _open_command(), actor=SYSTEM)
    assert factory.state["cases"] == {}
    assert factory.state["events"] == []

    ready_factory = _Factory()
    ready_service = _service(ready_factory)
    case_id = await _discovering(ready_service)
    plan = await ready_service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    await ready_service.confirm_public_plan(
        TENANT, plan.plan_id, plan.plan_hash, actor=BOSS
    )
    option = SourcingSupplyOption(
        option_id=SourcingSupplyOptionId("sop-rollback"),
        tenant_id=TENANT,
        case_id=case_id,
        source=SupplyOptionSource.EXISTING_PRODUCT,
        product_id=ProductId("prd-rollback"),
        supplier_candidate_id=None,
        is_qualified=True,
        created_at=NOW,
    )
    ready_factory.state["options"][(TENANT, option.option_id)] = option
    ready_factory.bus_fails = True
    with pytest.raises(RuntimeError, match="outbox unavailable"):
        await ready_service.mark_candidates_ready(
            TENANT, case_id, (option.option_id,), (), actor=SYSTEM
        )
    current = ready_factory.state["cases"][(TENANT, case_id)]
    assert current.state is CaseState.VERIFYING
