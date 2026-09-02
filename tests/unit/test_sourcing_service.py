"""Sourcing V2 领域服务的状态、门槛、授权与原子性合同。"""

from __future__ import annotations

import copy
import importlib
import traceback
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import Any, Self, cast

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
    PublicCandidateDraft,
    PublicCandidateDraftPriceTier,
    PublicCandidateDraftSpec,
    PublicSourcingPlanCommand,
    PublicSourcingQuery,
    SourcingAdmissionEnqueueCommand,
    SourcingAdmissionManualStartCommand,
    SourcingMatchInference,
    SourcingNeedSnapshot,
    SourcingObservedFact,
    SourcingPriorityFactsInput,
    SourcingReviewCommand,
    SourcingSupplierClaim,
    SourcingUncertainReconciliationCommand,
    SpecComparisonView,
    VerifyPublicCandidateDraftsCommand,
)
from domains.sourcing.service import (
    CandidateEvidenceSnapshot,
    CandidateEvidenceSnapshotReader,
    ProviderUsageEvidenceReader,
    ProviderUsageEvidenceSnapshot,
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
    NeedClusterId,
    OpportunityId,
    ProductId,
    RunId,
    SourcingAdmissionId,
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
PRODUCT = SourcingActor("emp-product", TENANT, SourcingScope.TENANT, "product")
FINANCE = SourcingActor("emp-finance", TENANT, SourcingScope.TENANT, "finance")

_models = importlib.import_module("domains.sourcing.models")
ADMISSION_RANKING_VERSION = _models.ADMISSION_RANKING_VERSION
AdmissionBlockedReason = _models.AdmissionBlockedReason
AdmissionState = _models.AdmissionState
CaseState = _models.CaseState
LadderCheck = _models.LadderCheck
LadderOutcome = _models.LadderOutcome
MatchLadderRung = _models.MatchLadderRung
PriceRejectionReason = _models.PriceRejectionReason
PublicPlanStatus = _models.PublicPlanStatus
SourcingReconciliationStatus = _models.SourcingReconciliationStatus
SourcingSearchExecution = _models.SourcingSearchExecution
SourcingSearchExecutionStatus = _models.SourcingSearchExecutionStatus
SourcingStopCode = _models.SourcingStopCode
SourcingStopDetail = _models.SourcingStopDetail
SourcingStopStage = _models.SourcingStopStage
SourcingAdmission = _models.SourcingAdmission
SourcingPrioritySnapshot = _models.SourcingPrioritySnapshot
SourcingSupplyOption = _models.SourcingSupplyOption
SpecComparison = _models.SpecComparison
SpecMatchLevel = _models.SpecMatchLevel
SupplyOptionSource = _models.SupplyOptionSource


def _service_type() -> type[Any]:
    try:
        return importlib.import_module(
            "domains.sourcing.service_impl"
        ).SourcingServiceImpl
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：SourcingServiceImpl 尚未实现（{exc}）")
    raise AssertionError("pytest.fail 必须终止执行")


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
            material=NeedFact(value="required-material", provenance=_provenance()),
            size_spec=NeedFact(value="required-size", provenance=_provenance()),
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


def _qualified_product_check(case_id: SourcingCaseId, *product_ids: ProductId) -> Any:
    check = _check(
        case_id,
        1,
        outcome=LadderOutcome.QUALIFIED_SUPPLY_FOUND,
    )
    frozen_ids = sorted(map(str, product_ids))
    return check.__class__(
        **{
            **check.__dict__,
            "input_snapshot": {
                **check.input_snapshot,
                "qualified_product_ids": frozen_ids,
                "product_spec_evidence": {
                    product_id: {
                        "product_category": "art-product-category",
                    }
                    for product_id in frozen_ids
                },
            },
            "match_object_type": "product",
            "match_object_id": frozen_ids[0],
            "spec_comparisons": tuple(
                SpecComparison(
                    spec_name="product_category",
                    required="hinges",
                    offered="hinges",
                    level=SpecMatchLevel.EXACT,
                    product_id=ProductId(product_id),
                    evidence_ref=ArtifactId("art-product-category"),
                )
                for product_id in frozen_ids
            ),
            "evidence_refs": (ArtifactId("art-product-category"),),
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_shape",
    [
        "missing_mapping",
        "extra_product",
        "missing_product",
        "missing_spec",
        "extra_spec",
        "ref_mismatch",
        "ref_outside_evidence_refs",
    ],
)
async def test_qualified_product_evidence_mapping_is_exact(
    invalid_shape: str,
) -> None:
    """合格 Product、spec、comparison 与 Evidence 集必须形成精确闭包。"""

    service = _service(_Factory())
    case_id = await _opened(service)
    product_id = ProductId("prd-sealed-evidence")
    qualified = _qualified_product_check(case_id, product_id)
    snapshot = copy.deepcopy(qualified.input_snapshot)
    comparisons = qualified.spec_comparisons
    evidence_refs = qualified.evidence_refs
    mapping = snapshot["product_spec_evidence"]
    assert isinstance(mapping, dict)
    if invalid_shape == "missing_mapping":
        del snapshot["product_spec_evidence"]
    elif invalid_shape == "extra_product":
        mapping["prd-extra"] = {"product_category": "art-product-category"}
    elif invalid_shape == "missing_product":
        mapping.clear()
    elif invalid_shape == "missing_spec":
        mapping[str(product_id)] = {}
    elif invalid_shape == "extra_spec":
        mapping[str(product_id)]["material"] = "art-product-category"
    elif invalid_shape == "ref_mismatch":
        mapping[str(product_id)]["product_category"] = "art-forged"
    else:
        mapping[str(product_id)]["product_category"] = "art-forged"
        comparison = comparisons[0]
        comparisons = (
            comparison.__class__(
                **{
                    **comparison.__dict__,
                    "evidence_ref": ArtifactId("art-forged"),
                }
            ),
        )

    invalid = qualified.__class__(
        **{
            **qualified.__dict__,
            "input_snapshot": snapshot,
            "spec_comparisons": comparisons,
            "evidence_refs": evidence_refs,
        }
    )
    with pytest.raises(ValidationError, match="证据映射"):
        await service.record_ladder_check(TENANT, case_id, invalid, actor=SYSTEM)


@pytest.mark.asyncio
async def test_qualified_product_evidence_mapping_rejects_spec_omitted_from_one_product() -> (
    None
):
    """同一必需规格从某个 Product 的 comparison 与 mapping 一起删除也不能绕过闭包。"""

    service = _service(_Factory())
    case_id = await _opened(service)
    first_product = ProductId("prd-complete-specs")
    second_product = ProductId("prd-missing-spec")
    qualified = _qualified_product_check(case_id, first_product, second_product)
    snapshot = copy.deepcopy(qualified.input_snapshot)
    mapping = snapshot["product_spec_evidence"]
    assert isinstance(mapping, dict)
    mapping[str(first_product)]["material"] = "art-material"
    comparisons = (
        *qualified.spec_comparisons,
        SpecComparison(
            spec_name="material",
            required="stainless",
            offered="stainless",
            level=SpecMatchLevel.EXACT,
            product_id=first_product,
            evidence_ref=ArtifactId("art-material"),
        ),
    )
    invalid = qualified.__class__(
        **{
            **qualified.__dict__,
            "input_snapshot": snapshot,
            "spec_comparisons": comparisons,
            "evidence_refs": (
                *qualified.evidence_refs,
                ArtifactId("art-material"),
            ),
        }
    )

    with pytest.raises(ValidationError, match="证据映射"):
        await service.record_ladder_check(TENANT, case_id, invalid, actor=SYSTEM)


@pytest.mark.asyncio
async def test_no_qualified_ladder_cannot_carry_product_evidence_mapping() -> None:
    service = _service(_Factory())
    case_id = await _opened(service)
    check = _check(case_id, 1)
    forged = check.__class__(
        **{
            **check.__dict__,
            "input_snapshot": {
                **check.input_snapshot,
                "product_spec_evidence": {"prd-forged": {"material": "art-forged"}},
            },
        }
    )

    with pytest.raises(ValidationError, match="证据映射"):
        await service.record_ladder_check(TENANT, case_id, forged, actor=SYSTEM)


def _plan(case_id: SourcingCaseId, version: int) -> PublicSourcingPlanCommand:
    return PublicSourcingPlanCommand(
        plan_id=SourcingPlanId(f"spl-{version}"),
        case_id=case_id,
        target_countries=("US",),
        product_category="hinges",
        queries=(
            PublicSourcingQuery(
                query_text=f"hinge factory v{version}", target_country="US"
            ),
        ),
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
            required="hinges" if name == "product_type" else f"required-{name}",
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


def test_candidate_submission_rejects_normalized_duplicate_spec_names() -> None:
    submission = _candidate()
    duplicate = submission.specs[1].model_copy(update={"spec_name": " MATERIAL "})

    with pytest.raises(ValueError, match="specs 规格名不得重复"):
        CandidateSubmission.model_validate(
            {
                **submission.model_dump(mode="python"),
                "specs": (*submission.specs, duplicate),
            }
        )


class _MemoryRepo:
    def __init__(self, state: dict[str, Any], name: str) -> None:
        self.state = state
        self.name = name


class _Cases(_MemoryRepo):
    async def get_or_create(self, tenant_id: TenantId, case: Any) -> tuple[Any, bool]:
        existing = await self.get_by_trigger(tenant_id, case.trigger_key)
        if existing is not None:
            return existing, False
        await self.add(tenant_id, case)
        return copy.deepcopy(case), True

    async def add(self, tenant_id: TenantId, case: Any) -> None:
        self.state[self.name][(tenant_id, case.case_id)] = copy.deepcopy(case)

    async def get(self, tenant_id: TenantId, case_id: SourcingCaseId) -> Any | None:
        return copy.deepcopy(self.state[self.name].get((tenant_id, case_id)))

    async def get_for_update(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> Any | None:
        return await self.get(tenant_id, case_id)

    async def update(
        self,
        tenant_id: TenantId,
        case: Any,
        *,
        clear_recoverable_stop: bool = False,
    ) -> None:
        if self.state.get("fail_case_update"):
            raise RuntimeError("case storage unavailable")
        key = (tenant_id, case.case_id)
        current = self.state[self.name].get(key)
        if current is None or current.version != case.version - 1:
            from domains.sourcing.errors import SourcingCaseConflictError

            raise SourcingCaseConflictError("stale")
        if (
            current.stop_code is not None
            and case.stop_code is None
            and not clear_recoverable_stop
        ):
            case.stop_code = current.stop_code
            case.stop_detail = current.stop_detail
        self.state[self.name][key] = copy.deepcopy(case)

    async def set_recoverable_stop(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        *,
        expected_version: int,
        stop_code: Any,
        stop_detail: Any,
    ) -> None:
        key = (tenant_id, case_id)
        current = self.state[self.name].get(key)
        if (
            current is None
            or current.version != expected_version
            or current.stop_code is not None
        ):
            from domains.sourcing.errors import SourcingCaseConflictError

            raise SourcingCaseConflictError("stale waiting stop")
        current.stop_code = stop_code
        current.stop_detail = stop_detail

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

    async def get_for_update(
        self, tenant_id: TenantId, plan_id: SourcingPlanId
    ) -> Any | None:
        return await self.get(tenant_id, plan_id)

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

    async def get_or_create_public_draft(
        self, tenant_id: TenantId, candidate: Any
    ) -> tuple[Any, bool]:
        for (candidate_tenant, _), current in self.state[self.name].items():
            if (
                candidate_tenant == tenant_id
                and current.public_draft_source_key == candidate.public_draft_source_key
            ):
                return copy.deepcopy(current), False
        await self.add(tenant_id, candidate)
        if self.state.get("fail_candidate_write"):
            raise RuntimeError("candidate storage unavailable")
        return copy.deepcopy(candidate), True

    async def get_by_public_draft_source_key(
        self, tenant_id: TenantId, source_key: str
    ) -> Any | None:
        for (candidate_tenant, _), candidate in self.state[self.name].items():
            if (
                candidate_tenant == tenant_id
                and candidate.public_draft_source_key == source_key
            ):
                return copy.deepcopy(candidate)
        return None

    async def get(
        self, tenant_id: TenantId, candidate_id: SupplierCandidateId
    ) -> Any | None:
        return copy.deepcopy(self.state[self.name].get((tenant_id, candidate_id)))

    async def list_for_case(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        include_rejected: bool,
        limit: int | None = None,
    ) -> list[Any]:
        values = [
            item
            for (candidate_tenant, _), item in self.state[self.name].items()
            if candidate_tenant == tenant_id and item.case_id == case_id
        ]
        values = [
            copy.deepcopy(item)
            for item in values
            if include_rejected or not item.rejected
        ]
        return values[:limit] if limit is not None else values

    async def count_qualified(
        self, tenant_id: TenantId, case_id: SourcingCaseId
    ) -> int:
        return sum(
            item.passes_verification()[0]
            for item in await self.list_for_case(tenant_id, case_id, False)
        )


class _Options(_MemoryRepo):
    async def get_or_create_existing_product(
        self, tenant_id: TenantId, option: Any
    ) -> tuple[Any, bool]:
        for (candidate_tenant, _), current in self.state[self.name].items():
            if (
                candidate_tenant == tenant_id
                and current.case_id == option.case_id
                and current.product_id == option.product_id
                and current.source is SupplyOptionSource.EXISTING_PRODUCT
            ):
                return copy.deepcopy(current), False
        await self.add(tenant_id, option)
        return copy.deepcopy(option), True

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


class _Admissions(_MemoryRepo):
    def __init__(
        self,
        state: dict[str, Any],
        name: str,
        get_or_create_calls: list[int],
    ) -> None:
        super().__init__(state, name)
        self.get_or_create_calls = get_or_create_calls

    async def get_or_create(
        self, tenant_id: TenantId, admission: Any, initial_snapshot: Any | None
    ) -> tuple[Any, Any | None, bool]:
        self.get_or_create_calls[0] += 1
        for (candidate_tenant, _), current in self.state[self.name].items():
            if candidate_tenant == tenant_id and (
                current.case_id == admission.case_id
                or current.need_id == admission.need_id
                or current.admission_id == admission.admission_id
            ):
                snapshot = (
                    None
                    if current.current_snapshot_id is None
                    else self.state["priority_snapshots"][
                        (tenant_id, current.current_snapshot_id)
                    ]
                )
                return copy.deepcopy(current), copy.deepcopy(snapshot), False
        self.state[self.name][(tenant_id, admission.admission_id)] = copy.deepcopy(
            admission
        )
        if initial_snapshot is not None:
            self.state["priority_snapshots"][
                (tenant_id, initial_snapshot.snapshot_id)
            ] = copy.deepcopy(initial_snapshot)
        return copy.deepcopy(admission), copy.deepcopy(initial_snapshot), True

    async def get(
        self, tenant_id: TenantId, admission_id: SourcingAdmissionId
    ) -> Any | None:
        return copy.deepcopy(self.state[self.name].get((tenant_id, admission_id)))

    async def get_with_current_snapshot(
        self, tenant_id: TenantId, admission_id: SourcingAdmissionId
    ) -> tuple[Any, Any | None] | None:
        admission = await self.get(tenant_id, admission_id)
        if admission is None:
            return None
        snapshot = (
            None
            if admission.current_snapshot_id is None
            else copy.deepcopy(
                self.state["priority_snapshots"][
                    (tenant_id, admission.current_snapshot_id)
                ]
            )
        )
        return admission, snapshot

    async def list_cluster_refresh_targets(
        self,
        tenant_id: TenantId,
        cluster_id: NeedClusterId,
        changed_need_id: ValidatedNeedId,
    ) -> list[tuple[Any, Any | None]]:
        pairs = []
        for (candidate_tenant, _), admission in self.state[self.name].items():
            if candidate_tenant != tenant_id or admission.state not in {
                AdmissionState.WAITING,
                AdmissionState.BLOCKED,
            }:
                continue
            snapshot = (
                None
                if admission.current_snapshot_id is None
                else self.state["priority_snapshots"][
                    (tenant_id, admission.current_snapshot_id)
                ]
            )
            if admission.need_id == changed_need_id or (
                snapshot is not None and snapshot.cluster_id == cluster_id
            ):
                pairs.append((copy.deepcopy(admission), copy.deepcopy(snapshot)))
        return sorted(pairs, key=lambda pair: str(pair[0].admission_id))

    async def append_snapshot_if_changed(
        self, tenant_id: TenantId, snapshot: Any
    ) -> tuple[Any, Any, bool]:
        current = await self.get(tenant_id, snapshot.admission_id)
        if current is None:
            raise ValidationError("准入记录不存在")
        canonical = next(
            (
                item
                for (candidate_tenant, _), item in self.state[
                    "priority_snapshots"
                ].items()
                if candidate_tenant == tenant_id
                and item.admission_id == snapshot.admission_id
                and item.facts_hash == snapshot.facts_hash
            ),
            None,
        )
        created = canonical is None
        canonical = snapshot if canonical is None else canonical
        if current.current_snapshot_id == canonical.snapshot_id and not (
            current.state is AdmissionState.BLOCKED
            and current.blocked_reason is AdmissionBlockedReason.PRIORITY_FACTS_INVALID
        ):
            return current, copy.deepcopy(canonical), False
        changed = current.with_current_snapshot(snapshot)
        if canonical.snapshot_id != snapshot.snapshot_id:
            changed = replace(changed, current_snapshot_id=canonical.snapshot_id)
        self.state[self.name][(tenant_id, changed.admission_id)] = copy.deepcopy(
            changed
        )
        if created:
            self.state["priority_snapshots"][(tenant_id, snapshot.snapshot_id)] = (
                copy.deepcopy(snapshot)
            )
        return copy.deepcopy(changed), copy.deepcopy(canonical), created

    async def claim_ordered(
        self,
        tenant_id: TenantId,
        limit: int,
        claim_token: str,
        claim_expires_at: datetime,
        now: datetime,
    ) -> list[Any]:
        candidates = []
        for (candidate_tenant, _), admission in self.state[self.name].items():
            if candidate_tenant != tenant_id:
                continue
            if admission.state is AdmissionState.STARTING:
                admission = admission.release_expired_claim(now=now)
            if admission.state is AdmissionState.WAITING:
                snapshot = self.state["priority_snapshots"][
                    (tenant_id, admission.current_snapshot_id)
                ]
                candidates.append((admission, snapshot))
        candidates.sort(
            key=lambda pair: (
                -pair[1].cluster_member_count,
                pair[1].ready_at,
                str(pair[1].need_id),
                str(pair[1].snapshot_id),
            )
        )
        claimed = []
        for admission, _ in candidates[:limit]:
            changed = admission.claim(
                claim_token, claim_expires_at=claim_expires_at, claimed_at=now
            )
            self.state[self.name][(tenant_id, admission.admission_id)] = copy.deepcopy(
                changed
            )
            claimed.append(copy.deepcopy(changed))
        return claimed

    async def claim_one(
        self,
        tenant_id: TenantId,
        admission_id: SourcingAdmissionId,
        claim_token: str,
        claim_expires_at: datetime,
        now: datetime,
        *,
        requested_by: str,
    ) -> Any | None:
        admission = await self.get(tenant_id, admission_id)
        if admission is None:
            return None
        if admission.state is AdmissionState.STARTING:
            admission = admission.release_expired_claim(now=now)
        if (
            admission.admission_requested_by is not None
            and admission.admission_requested_by != requested_by
        ):
            return copy.deepcopy(admission)
        if admission.state is AdmissionState.WAITING:
            admission = replace(
                admission,
                admission_requested_by=(
                    admission.admission_requested_by or requested_by
                ),
            )
            admission = admission.claim(
                claim_token,
                claim_expires_at=claim_expires_at,
                claimed_at=now,
            )
            self.state[self.name][(tenant_id, admission_id)] = copy.deepcopy(admission)
        return copy.deepcopy(admission)

    async def complete(
        self,
        tenant_id: TenantId,
        admission_id: SourcingAdmissionId,
        claim_token: str,
        workflow_run_id: RunId,
        system_actor_id: str,
        admitted_at: datetime,
    ) -> Any | None:
        admission = await self.get(tenant_id, admission_id)
        if (
            admission is None
            or admission.state is not AdmissionState.STARTING
            or admission.claim_token != claim_token
        ):
            return None
        changed = admission.complete(
            claim_token,
            workflow_run_id=workflow_run_id,
            system_actor_id=system_actor_id,
            admitted_at=admitted_at,
        )
        self.state[self.name][(tenant_id, admission_id)] = copy.deepcopy(changed)
        return changed

    async def release_expired_claims(
        self, tenant_id: TenantId, now: datetime
    ) -> list[Any]:
        released = []
        for (candidate_tenant, admission_id), admission in list(
            self.state[self.name].items()
        ):
            if candidate_tenant != tenant_id:
                continue
            changed = admission.release_expired_claim(now=now)
            if changed != admission:
                self.state[self.name][(tenant_id, admission_id)] = copy.deepcopy(
                    changed
                )
                released.append(copy.deepcopy(changed))
        return released

    async def release(
        self,
        tenant_id: TenantId,
        admission_id: SourcingAdmissionId,
        claim_token: str,
        released_at: datetime,
    ) -> Any | None:
        admission = await self.get(tenant_id, admission_id)
        if (
            admission is None
            or admission.state is not AdmissionState.STARTING
            or admission.claim_token != claim_token
        ):
            return None
        changed = admission._transition(
            AdmissionState.WAITING,
            changed_at=released_at,
            claim_token=None,
            claim_expires_at=None,
        )
        self.state[self.name][(tenant_id, admission_id)] = copy.deepcopy(changed)
        return changed

    async def block(
        self,
        tenant_id: TenantId,
        admission_id: SourcingAdmissionId,
        reason: Any,
        blocked_at: datetime,
        claim_token: str | None = None,
    ) -> Any | None:
        admission = await self.get(tenant_id, admission_id)
        if admission is None or (
            admission.state is AdmissionState.STARTING
            and admission.claim_token != claim_token
        ):
            return None
        if admission.state is not AdmissionState.WAITING and not (
            admission.state is AdmissionState.STARTING
            and admission.claim_token == claim_token
        ):
            return None
        changed = admission.block(
            reason, blocked_at=blocked_at, claim_token=claim_token
        )
        self.state[self.name][(tenant_id, admission_id)] = copy.deepcopy(changed)
        return changed

    async def list_by_state(
        self, tenant_id: TenantId, state: Any, limit: int
    ) -> list[Any]:
        return [
            item[0]
            for item in await self.list_by_state_with_current_snapshot(
                tenant_id, state, limit
            )
        ]

    async def list_by_state_with_current_snapshot(
        self, tenant_id: TenantId, state: Any, limit: int
    ) -> list[tuple[Any, Any | None]]:
        pairs = []
        for (candidate_tenant, admission_id), admission in self.state[
            self.name
        ].items():
            if candidate_tenant != tenant_id or admission.state is not state:
                continue
            snapshot = (
                None
                if admission.current_snapshot_id is None
                else self.state["priority_snapshots"][
                    (tenant_id, admission.current_snapshot_id)
                ]
            )
            pairs.append((copy.deepcopy(admission), copy.deepcopy(snapshot)))
        pairs.sort(
            key=lambda pair: (
                -(pair[1].cluster_member_count if pair[1] is not None else -1),
                pair[0].ready_at,
                str(pair[0].need_id),
                str(pair[0].admission_id),
            )
        )
        return pairs[:limit]


class _SearchExecutions(_MemoryRepo):
    async def get_by_request_key(self, tenant_id: TenantId, request_key: str) -> Any:
        return copy.deepcopy(self.state[self.name].get((tenant_id, request_key)))

    async def list_uncertain_for_case(
        self, tenant_id: TenantId, case_id: SourcingCaseId, limit: int
    ) -> list[Any]:
        return [
            copy.deepcopy(item)
            for (_, _), item in sorted(
                self.state[self.name].items(),
                key=lambda pair: (pair[1].created_at, pair[1].execution_id),
            )
            if item.tenant_id == tenant_id
            and item.case_id == case_id
            and item.provider_status is SourcingSearchExecutionStatus.UNCERTAIN
        ][:limit]


class _Reconciliations(_MemoryRepo):
    async def add(self, tenant_id: TenantId, reconciliation: Any) -> None:
        self.state[self.name][(tenant_id, reconciliation.execution_id)] = copy.deepcopy(
            reconciliation
        )

    async def get_for_execution(self, tenant_id: TenantId, execution_id: str) -> Any:
        return copy.deepcopy(self.state[self.name].get((tenant_id, execution_id)))

    async def get_or_create_canonical(
        self, tenant_id: TenantId, reconciliation: Any
    ) -> Any:
        matches = [
            item
            for (candidate_tenant, _), item in self.state[self.name].items()
            if candidate_tenant == tenant_id
            and (
                item.reconciliation_id == reconciliation.reconciliation_id
                or item.execution_id == reconciliation.execution_id
            )
        ]
        if not matches:
            self.state[self.name][(tenant_id, reconciliation.execution_id)] = (
                copy.deepcopy(reconciliation)
            )
            return copy.deepcopy(reconciliation)
        if len(matches) != 1:
            raise SourcingPlanStaleError("不确定搜索核对事实冲突")
        canonical = matches[0]
        if (
            canonical.reconciliation_id != reconciliation.reconciliation_id
            or canonical.execution_id != reconciliation.execution_id
            or canonical.status != reconciliation.status
            or canonical.reason != reconciliation.reason
            or canonical.provider_usage_artifact_ref
            != reconciliation.provider_usage_artifact_ref
            or canonical.reconciled_by != reconciliation.reconciled_by
        ):
            raise SourcingPlanStaleError("不确定搜索核对事实冲突")
        return copy.deepcopy(canonical)


class _Drafts(_MemoryRepo):
    async def list_exact_for_verification(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        run_id: RunId,
        plan_id: SourcingPlanId,
        plan_hash: str,
    ) -> list[PublicCandidateDraft]:
        values = [
            draft
            for (candidate_tenant, _), draft in self.state[self.name].items()
            if candidate_tenant == tenant_id
            and draft.case_id == case_id
            and draft.run_id == run_id
            and draft.plan_id == plan_id
            and draft.plan_hash == plan_hash
        ]
        return copy.deepcopy(
            sorted(
                values,
                key=lambda item: (
                    item.query_index,
                    item.result_index,
                    item.source_key,
                ),
            )
        )


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
    def __init__(
        self,
        state: dict[str, Any],
        admission_get_or_create_calls: list[int],
        *,
        bus_fails: bool = False,
    ) -> None:
        self.state = state
        self.bus_fails = bus_fails
        self.cases = _Cases(state, "cases")
        self.admissions = _Admissions(
            state, "admissions", admission_get_or_create_calls
        )
        self.checks = _Checks(state, "checks")
        self.plans = _Plans(state, "plans")
        self.candidates = _Candidates(state, "candidates")
        self.options = _Options(state, "options")
        self.reviews = _Reviews(state, "reviews")
        self.search_executions = _SearchExecutions(state, "search_executions")
        self.candidate_drafts = _Drafts(state, "candidate_drafts")
        self.reconciliations = _Reconciliations(state, "reconciliations")
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
        self.admission_get_or_create_calls = [0]
        self.bus_fails = bus_fails
        self.state: dict[str, Any] = {
            "cases": {},
            "admissions": {},
            "priority_snapshots": {},
            "checks": [],
            "plans": {},
            "candidates": {},
            "options": {},
            "reviews": {},
            "search_executions": {},
            "candidate_drafts": {},
            "reconciliations": {},
            "events": [],
            "fail_candidate_write": False,
            "fail_case_update": False,
        }

    def __call__(self, tenant_id: TenantId) -> _MemoryUow:
        self.calls += 1
        return _MemoryUow(
            self.state,
            self.admission_get_or_create_calls,
            bus_fails=self.bus_fails,
        )


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


class _ProviderUsageReader:
    def __init__(
        self,
        projection: ProviderUsageEvidenceSnapshot | None = None,
        failure: BaseException | None = None,
    ) -> None:
        self.calls = 0
        self.failure = failure
        self.projection = projection or ProviderUsageEvidenceSnapshot(
            tenant_id=TENANT,
            artifact_id=ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0Z"),
            provider="tavily",
            content_hash="e" * 64,
            observed_at=NOW,
        )

    async def read_verified(
        self, tenant_id: TenantId, artifact_id: ArtifactId
    ) -> ProviderUsageEvidenceSnapshot:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.projection


def _service(
    factory: _Factory,
    evidence_reader: CandidateEvidenceSnapshotReader | None = None,
    provider_usage_reader: ProviderUsageEvidenceReader | None = None,
) -> Any:
    return _service_type()(
        factory,
        Phase2SourcingAuthorizer(TENANT),
        evidence_reader or _EvidenceReader(),
        provider_usage_evidence_reader=provider_usage_reader,
        now=lambda: NOW,
    )


def test_concrete_service_preserves_the_public_protocol_surface() -> None:
    assert isinstance(_service(_Factory()), SourcingService)


@pytest.mark.asyncio
async def test_admission_case_snapshot_read_is_system_only_and_tenant_bound() -> None:
    """准入 driver 只能经 public service 读取本租户 frozen Case DTO。"""

    factory = _Factory()
    service = _service(factory)
    case_id = await _opened(service)
    canonical_hash = "5f8927d174dab1b6259004d1ced240fa24bbb2873a87932096be614f6f335736"
    stored = factory.state["cases"][(TENANT, case_id)]
    assert stored.need_snapshot is not None
    canonical_snapshot = stored.need_snapshot.model_copy(
        update={"snapshot_hash": canonical_hash}
    )
    factory.state["cases"][(TENANT, case_id)] = replace(
        stored,
        need_snapshot=canonical_snapshot,
        need_snapshot_hash=canonical_hash,
    )
    calls_after_open = factory.calls

    view = await service.get_admission_case_snapshot(TENANT, case_id, actor=SYSTEM)

    assert view is not None
    assert view.case_id == case_id
    assert view.need_snapshot == canonical_snapshot
    assert factory.calls == calls_after_open + 1

    other_system = SourcingActor(
        "system-other", OTHER_TENANT, SourcingScope.SYSTEM, "system"
    )
    with pytest.raises(PermissionDenied, match="Phase 2 寻源授权拒绝"):
        await service.get_admission_case_snapshot(
            OTHER_TENANT, case_id, actor=other_system
        )
    assert factory.calls == calls_after_open + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["persisted_hash_drift", "snapshot_body_drift"])
async def test_admission_case_snapshot_read_rejects_three_way_hash_drift(
    fault: str,
) -> None:
    """持久 hash、内嵌 hash 与正文 canonical hash 任一漂移都必须永久拒绝。"""

    factory = _Factory()
    service = _service(factory)
    case_id = await _opened(service)
    key = (TENANT, case_id)
    stored = factory.state["cases"][key]
    assert stored.need_snapshot is not None
    canonical_hash = "5f8927d174dab1b6259004d1ced240fa24bbb2873a87932096be614f6f335736"
    snapshot = stored.need_snapshot.model_copy(update={"snapshot_hash": canonical_hash})
    if fault == "persisted_hash_drift":
        persisted_hash = "b" * 64
    else:
        persisted_hash = canonical_hash
        snapshot = snapshot.model_copy(
            update={"quantity": snapshot.quantity.model_copy(update={"value": 6000})}
        )
    factory.state["cases"][key] = replace(
        stored,
        need_snapshot=snapshot,
        need_snapshot_hash=persisted_hash,
    )
    calls_before_read = factory.calls

    with pytest.raises(ValidationError, match="^寻源准入 Case 冻结快照完整性无效$"):
        await service.get_admission_case_snapshot(TENANT, case_id, actor=SYSTEM)

    assert factory.calls == calls_before_read + 1


def _priority_facts(
    need_id: ValidatedNeedId,
    *,
    count: int = 1,
    cluster_id: NeedClusterId | None = None,
    observed_at: datetime = NOW - timedelta(minutes=5),
) -> SourcingPriorityFactsInput:
    return SourcingPriorityFactsInput(
        need_id=need_id,
        cluster_id=cluster_id,
        cluster_member_count=count,
        facts_observed_at=observed_at,
    )


async def _enqueue(
    service: Any,
    case_id: SourcingCaseId,
    need_id: ValidatedNeedId,
    *,
    count: int = 1,
    cluster_id: NeedClusterId | None = None,
    ready_at: datetime = NOW - timedelta(hours=2),
) -> SourcingAdmissionId:
    return await service.enqueue_admission(
        TENANT,
        case_id,
        need_id,
        ready_at=ready_at,
        command=SourcingAdmissionEnqueueCommand(
            facts=_priority_facts(need_id, count=count, cluster_id=cluster_id)
        ),
        actor=SYSTEM,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("missing_snapshot", "缺少已验证需求快照"),
        ("snapshot_need_mismatch", "Case、Need 与快照不一致"),
        ("completeness_two", "完整度不足 3"),
        ("non_opened", "OPENED"),
        ("workflow_v1", "V2"),
    ],
)
async def test_admission_enqueue_fails_closed_before_repository_write(
    fault: str, message: str
) -> None:
    """准入目标必须仍是完整、同 Need 的 OPENED V2 Case。"""

    factory = _Factory()
    service = _service(factory)
    case_id = await _opened(service)
    need_id = _open_command().need.need_id
    key = (TENANT, case_id)
    case = factory.state["cases"][key]
    if fault == "missing_snapshot":
        case = replace(case, need_snapshot=None, need_snapshot_hash=None)
    elif fault == "snapshot_need_mismatch":
        assert case.need_snapshot is not None
        case = replace(
            case,
            need_snapshot=case.need_snapshot.model_copy(
                update={"need_id": ValidatedNeedId("need-snapshot-other")}
            ),
        )
    elif fault == "completeness_two":
        assert case.need_snapshot is not None
        case = replace(
            case,
            need_snapshot=case.need_snapshot.model_copy(update={"completeness": 2}),
        )
    elif fault == "non_opened":
        case = replace(case, state=CaseState.DISCOVERING)
    else:
        case = replace(case, workflow_version=1)
    factory.state["cases"][key] = case

    with pytest.raises(ValidationError, match=message):
        await _enqueue(service, case_id, need_id)

    assert factory.admission_get_or_create_calls == [0]
    assert factory.state["admissions"] == {}
    assert factory.state["priority_snapshots"] == {}


@pytest.mark.asyncio
async def test_admission_enqueue_accepts_complete_opened_v2_case() -> None:
    """完整度恰为 3 的目标 Need 可以进入 waiting 并保存首快照。"""

    factory = _Factory()
    service = _service(factory)
    case_id = await _opened(service)

    admission_id = await _enqueue(service, case_id, _open_command().need.need_id)

    assert factory.admission_get_or_create_calls == [1]
    assert (TENANT, admission_id) in factory.state["admissions"]
    assert len(factory.state["priority_snapshots"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [False, True])
async def test_non_opened_case_replay_cannot_change_starting_or_admitted_record(
    terminal: bool,
) -> None:
    """Case 离开 OPENED 后，重放不得补写或改变既有 starting/终态 admission。"""

    factory = _Factory()
    service = _service(factory)
    case_id = await _opened(service)
    need_id = _open_command().need.need_id
    admission_id = await _enqueue(service, case_id, need_id)
    await service.claim_admissions(
        TENANT,
        limit=1,
        claim_token="claim-case-closed",
        claim_expires_at=NOW + timedelta(minutes=5),
        actor=SYSTEM,
    )
    if terminal:
        await service.complete_admission(
            TENANT,
            admission_id,
            claim_token="claim-case-closed",
            workflow_run_id=RunId("run-case-closed"),
            admitted_at=NOW + timedelta(minutes=1),
            actor=SYSTEM,
        )
    key = (TENANT, case_id)
    factory.state["cases"][key] = replace(
        factory.state["cases"][key],
        state=(CaseState.FAILED if terminal else CaseState.DISCOVERING),
    )
    admission_before = copy.deepcopy(factory.state["admissions"])
    snapshots_before = copy.deepcopy(factory.state["priority_snapshots"])
    repository_calls_before = factory.admission_get_or_create_calls[0]

    with pytest.raises(ValidationError, match="OPENED"):
        await _enqueue(service, case_id, need_id)

    assert factory.admission_get_or_create_calls == [repository_calls_before]
    assert factory.state["admissions"] == admission_before
    assert factory.state["priority_snapshots"] == snapshots_before


@pytest.mark.asyncio
async def test_manual_claim_targets_exact_admission_and_replays_same_request() -> None:
    """错误实现若复用全局有序 claim，会启动别的队首 Case 或丢失原请求键。"""

    factory = _Factory()
    service = _service(factory)
    first_case = await _opened(service)
    first_need = _open_command().need.need_id
    first_id = await _enqueue(
        service,
        first_case,
        first_need,
        count=8,
        cluster_id=NeedClusterId("cluster-manual-priority"),
    )
    second_need = ValidatedNeedId("need-manual-target")
    second_command = _open_command().model_copy(
        update={
            "need": _open_command().need.model_copy(update={"need_id": second_need}),
            "trigger_key": f"sourcing-case:v2:{TENANT}:{second_need}",
        }
    )
    second_case = await service.open_case(TENANT, second_command, actor=SYSTEM)
    second_id = await _enqueue(service, second_case, second_need, count=1)
    command = SourcingAdmissionManualStartCommand(request_id="manual-request-1")

    claimed = await service.claim_manual_admission(
        TENANT,
        second_id,
        command,
        claim_expires_at=NOW + timedelta(minutes=5),
        actor=BOSS,
    )
    replayed = await service.claim_manual_admission(
        TENANT,
        second_id,
        command,
        claim_expires_at=NOW + timedelta(minutes=5),
        actor=BOSS,
    )

    assert claimed == replayed
    assert claimed is not None
    assert claimed.admission_id == second_id
    assert claimed.state is AdmissionState.STARTING
    assert claimed.claim_token == "manual-request-1"
    assert claimed.admission_requested_by == BOSS.actor_id
    assert (
        factory.state["admissions"][(TENANT, first_id)].state is AdmissionState.WAITING
    )


@pytest.mark.asyncio
async def test_manual_claim_rejects_other_request_and_role_before_mutation() -> None:
    factory = _Factory()
    service = _service(factory)
    case_id = await _opened(service)
    admission_id = await _enqueue(service, case_id, _open_command().need.need_id)
    await service.claim_manual_admission(
        TENANT,
        admission_id,
        SourcingAdmissionManualStartCommand(request_id="manual-owner"),
        claim_expires_at=NOW + timedelta(minutes=5),
        actor=SOURCING,
    )
    before = copy.deepcopy(factory.state["admissions"])

    with pytest.raises(InvalidStateTransition):
        await service.claim_manual_admission(
            TENANT,
            admission_id,
            SourcingAdmissionManualStartCommand(request_id="manual-other"),
            claim_expires_at=NOW + timedelta(minutes=5),
            actor=BOSS,
        )
    with pytest.raises(PermissionDenied):
        await service.claim_manual_admission(
            TENANT,
            admission_id,
            SourcingAdmissionManualStartCommand(request_id="manual-owner"),
            claim_expires_at=NOW + timedelta(minutes=5),
            actor=PRODUCT,
        )

    assert factory.state["admissions"] == before


@pytest.mark.asyncio
async def test_manual_same_request_cannot_be_replayed_by_different_actor() -> None:
    """request id 不是身份；换员工重放同一 key 不能继承原人工审计意图。"""

    factory = _Factory()
    service = _service(factory)
    case_id = await _opened(service)
    admission_id = await _enqueue(service, case_id, _open_command().need.need_id)
    command = SourcingAdmissionManualStartCommand(request_id="manual-shared-key")
    await service.claim_manual_admission(
        TENANT,
        admission_id,
        command,
        claim_expires_at=NOW + timedelta(minutes=5),
        actor=BOSS,
    )
    before = copy.deepcopy(factory.state["admissions"])

    with pytest.raises(InvalidStateTransition):
        await service.claim_manual_admission(
            TENANT,
            admission_id,
            command,
            claim_expires_at=NOW + timedelta(minutes=5),
            actor=SOURCING,
        )

    assert factory.state["admissions"] == before


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [BOSS, SOURCING])
async def test_tenant_actor_cannot_call_low_level_complete_before_uow(
    actor: SourcingActor,
) -> None:
    """低层 complete 只能由 system 调用，且合同不暴露 admitted_by 注入点。"""

    factory = _Factory()
    service = _service(factory)

    with pytest.raises(PermissionDenied):
        await service.complete_admission(
            TENANT,
            SourcingAdmissionId("sad-direct-complete"),
            claim_token="claim-direct-complete",
            workflow_run_id=RunId("run-direct-complete"),
            admitted_at=NOW,
            actor=actor,
        )

    assert factory.calls == 0


@pytest.mark.asyncio
async def test_admission_enqueue_is_canonical_and_refresh_appends_only_changed_facts() -> (
    None
):
    """重复入口或相同事实不得产生第二条 admission/snapshot。"""

    factory = _Factory()
    service = _service(factory)
    case_id = await _opened(service)
    need_id = _open_command().need.need_id

    first_id = await _enqueue(service, case_id, need_id)
    first_view = await service.get_admission(TENANT, first_id, actor=BOSS)
    replay_id = await _enqueue(service, case_id, need_id)
    replay_view = await service.get_admission(TENANT, replay_id, actor=BOSS)

    assert replay_id == first_id
    assert replay_view == first_view
    assert len(factory.state["admissions"]) == 1
    assert len(factory.state["priority_snapshots"]) == 1

    changed = _priority_facts(
        need_id,
        count=4,
        cluster_id=NeedClusterId("cluster-service-1"),
        observed_at=NOW,
    )
    changed_id = await service.refresh_admission(
        TENANT,
        first_id,
        facts=changed,
        refreshed_at=NOW + timedelta(minutes=1),
        actor=SYSTEM,
    )
    replay_changed_id = await service.refresh_admission(
        TENANT,
        first_id,
        facts=changed,
        refreshed_at=NOW + timedelta(minutes=2),
        actor=SYSTEM,
    )

    assert changed_id == replay_changed_id
    assert len(factory.state["priority_snapshots"]) == 2
    current = await service.get_admission(TENANT, first_id, actor=BOSS)
    assert current is not None
    assert current.snapshot_id == changed_id
    assert current.cluster_member_count == 4


@pytest.mark.asyncio
async def test_admitted_refresh_is_a_no_op() -> None:
    """终态 admitted 的排序快照不可被后续簇变化改写。"""

    factory = _Factory()
    service = _service(factory)
    case_id = await _opened(service)
    need_id = _open_command().need.need_id
    admission_id = await _enqueue(service, case_id, need_id)
    claimed = await service.claim_admissions(
        TENANT,
        limit=2,
        claim_token="claim-1",
        claim_expires_at=NOW + timedelta(minutes=5),
        actor=SYSTEM,
    )
    assert [item.admission_id for item in claimed] == [admission_id]
    await service.complete_admission(
        TENANT,
        admission_id,
        claim_token="claim-1",
        workflow_run_id=RunId("run-admission-1"),
        admitted_at=NOW + timedelta(minutes=1),
        actor=SYSTEM,
    )
    before = await service.get_admission(TENANT, admission_id, actor=BOSS)

    result = await service.refresh_admission(
        TENANT,
        admission_id,
        facts=_priority_facts(
            need_id,
            count=9,
            cluster_id=NeedClusterId("cluster-later"),
            observed_at=NOW + timedelta(minutes=1),
        ),
        refreshed_at=NOW + timedelta(minutes=2),
        actor=SYSTEM,
    )

    assert result is None
    assert await service.get_admission(TENANT, admission_id, actor=BOSS) == before
    assert len(factory.state["priority_snapshots"]) == 1


@pytest.mark.asyncio
async def test_starting_refresh_is_a_strict_no_op() -> None:
    """starting 租约期间的 refresh 不得改状态、token、pointer 或快照集。"""

    factory = _Factory()
    service = _service(factory)
    case_id = await _opened(service)
    need_id = _open_command().need.need_id
    admission_id = await _enqueue(service, case_id, need_id)
    await service.claim_admissions(
        TENANT,
        limit=1,
        claim_token="claim-refresh-noop",
        claim_expires_at=NOW + timedelta(minutes=5),
        actor=SYSTEM,
    )
    admission_before = copy.deepcopy(factory.state["admissions"])
    snapshots_before = copy.deepcopy(factory.state["priority_snapshots"])

    result = await service.refresh_admission(
        TENANT,
        admission_id,
        facts=_priority_facts(
            need_id,
            count=9,
            cluster_id=NeedClusterId("cluster-starting-later"),
            observed_at=NOW + timedelta(minutes=1),
        ),
        refreshed_at=NOW + timedelta(minutes=2),
        actor=SYSTEM,
    )

    assert result is None
    assert factory.state["admissions"] == admission_before
    assert factory.state["priority_snapshots"] == snapshots_before


@pytest.mark.asyncio
async def test_cluster_refresh_updates_all_eligible_targets_without_state_bypass() -> (
    None
):
    """同簇无截断刷新；changed 无快照可恢复，starting/admitted/异簇保持不变。"""

    factory = _Factory()
    service = _service(factory)
    cluster_id = NeedClusterId("cluster-refresh")

    async def open_and_enqueue(
        suffix: str,
        *,
        target_cluster: NeedClusterId | None,
    ) -> tuple[ValidatedNeedId, SourcingAdmissionId]:
        need_id = ValidatedNeedId(f"need-refresh-{suffix}")
        command = _open_command().model_copy(
            update={
                "need": _open_command().need.model_copy(update={"need_id": need_id}),
                "trigger_key": f"sourcing-case:v2:{TENANT}:{need_id}",
            }
        )
        case_id = await service.open_case(TENANT, command, actor=SYSTEM)
        admission_id = await _enqueue(
            service,
            case_id,
            need_id,
            count=2,
            cluster_id=target_cluster,
        )
        return need_id, admission_id

    first_need, first_id = await open_and_enqueue("first", target_cluster=cluster_id)
    second_need, second_id = await open_and_enqueue("second", target_cluster=cluster_id)
    _, other_id = await open_and_enqueue(
        "other", target_cluster=NeedClusterId("cluster-other")
    )
    _, starting_id = await open_and_enqueue("starting", target_cluster=cluster_id)
    _, admitted_id = await open_and_enqueue("admitted", target_cluster=cluster_id)
    _, case_blocked_id = await open_and_enqueue(
        "case-blocked", target_cluster=cluster_id
    )

    starting_key = (TENANT, starting_id)
    admitted_key = (TENANT, admitted_id)
    case_blocked_key = (TENANT, case_blocked_id)
    factory.state["admissions"][starting_key] = factory.state["admissions"][
        starting_key
    ].claim(
        "claim-starting",
        claim_expires_at=NOW + timedelta(minutes=10),
        claimed_at=NOW,
    )
    admitted_starting = factory.state["admissions"][admitted_key].claim(
        "claim-admitted",
        claim_expires_at=NOW + timedelta(minutes=10),
        claimed_at=NOW,
    )
    factory.state["admissions"][admitted_key] = admitted_starting.complete(
        "claim-admitted",
        workflow_run_id=RunId("run-refresh-admitted"),
        system_actor_id="system:sourcing",
        admitted_at=NOW + timedelta(minutes=1),
    )
    factory.state["admissions"][case_blocked_key] = factory.state["admissions"][
        case_blocked_key
    ].block(
        AdmissionBlockedReason.CASE_STATE_MISMATCH,
        blocked_at=NOW,
    )

    changed_need = ValidatedNeedId("need-refresh-changed")
    changed_command = _open_command().model_copy(
        update={
            "need": _open_command().need.model_copy(update={"need_id": changed_need}),
            "trigger_key": f"sourcing-case:v2:{TENANT}:{changed_need}",
        }
    )
    changed_case = await service.open_case(TENANT, changed_command, actor=SYSTEM)
    changed_id = await service.enqueue_admission(
        TENANT,
        changed_case,
        changed_need,
        ready_at=NOW - timedelta(hours=2),
        command=SourcingAdmissionEnqueueCommand(
            facts=None,
            blocked_reason="priority_facts_invalid",
        ),
        actor=SYSTEM,
    )
    before_starting = copy.deepcopy(factory.state["admissions"][starting_key])
    before_admitted = copy.deepcopy(factory.state["admissions"][admitted_key])
    before_other = copy.deepcopy(factory.state["admissions"][(TENANT, other_id)])
    initial_snapshots = len(factory.state["priority_snapshots"])
    current_facts = _priority_facts(
        changed_need,
        count=9,
        cluster_id=cluster_id,
        observed_at=NOW + timedelta(minutes=1),
    )

    refreshed = await service.refresh_cluster_admissions(
        TENANT,
        changed_need,
        facts=current_facts,
        refreshed_at=NOW + timedelta(minutes=2),
        actor=SYSTEM,
    )
    replayed = await service.refresh_cluster_admissions(
        TENANT,
        changed_need,
        facts=current_facts,
        refreshed_at=NOW + timedelta(minutes=3),
        actor=SYSTEM,
    )

    assert refreshed == replayed
    assert len(refreshed) == 4
    assert len(factory.state["priority_snapshots"]) == initial_snapshots + 4
    assert factory.state["admissions"][starting_key] == before_starting
    assert factory.state["admissions"][admitted_key] == before_admitted
    assert factory.state["admissions"][(TENANT, other_id)] == before_other
    for admission_id, need_id in (
        (first_id, first_need),
        (second_id, second_need),
        (changed_id, changed_need),
    ):
        view = await service.get_admission(TENANT, admission_id, actor=BOSS)
        assert view is not None
        assert view.need_id == need_id
        assert view.cluster_id == cluster_id
        assert view.cluster_member_count == 9
        assert view.state == "waiting"
    case_blocked = await service.get_admission(TENANT, case_blocked_id, actor=BOSS)
    assert case_blocked is not None
    assert case_blocked.state == "blocked"
    assert case_blocked.blocked_reason == "case_state_mismatch"
    assert case_blocked.cluster_member_count == 9


@pytest.mark.asyncio
async def test_cluster_block_marks_only_waiting_targets_and_is_idempotent() -> None:
    """永久非法簇事实不能留旧 waiting 排序，也不能覆盖其他阻断或终态。"""

    factory = _Factory()
    service = _service(factory)
    cluster_id = NeedClusterId("cluster-block")

    async def open_and_enqueue(
        suffix: str,
        *,
        target_cluster: NeedClusterId | None,
    ) -> tuple[ValidatedNeedId, SourcingAdmissionId]:
        need_id = ValidatedNeedId(f"need-block-{suffix}")
        command = _open_command().model_copy(
            update={
                "need": _open_command().need.model_copy(update={"need_id": need_id}),
                "trigger_key": f"sourcing-case:v2:{TENANT}:{need_id}",
            }
        )
        case_id = await service.open_case(TENANT, command, actor=SYSTEM)
        return need_id, await _enqueue(
            service,
            case_id,
            need_id,
            count=2,
            cluster_id=target_cluster,
        )

    _, first_id = await open_and_enqueue("first", target_cluster=cluster_id)
    _, second_id = await open_and_enqueue("second", target_cluster=cluster_id)
    _, other_id = await open_and_enqueue(
        "other", target_cluster=NeedClusterId("cluster-other")
    )
    _, starting_id = await open_and_enqueue("starting", target_cluster=cluster_id)
    _, admitted_id = await open_and_enqueue("admitted", target_cluster=cluster_id)
    _, case_blocked_id = await open_and_enqueue(
        "case-blocked", target_cluster=cluster_id
    )
    starting_key = (TENANT, starting_id)
    admitted_key = (TENANT, admitted_id)
    case_blocked_key = (TENANT, case_blocked_id)
    factory.state["admissions"][starting_key] = factory.state["admissions"][
        starting_key
    ].claim(
        "claim-block-starting",
        claim_expires_at=NOW + timedelta(minutes=10),
        claimed_at=NOW,
    )
    admitted_starting = factory.state["admissions"][admitted_key].claim(
        "claim-block-admitted",
        claim_expires_at=NOW + timedelta(minutes=10),
        claimed_at=NOW,
    )
    factory.state["admissions"][admitted_key] = admitted_starting.complete(
        "claim-block-admitted",
        workflow_run_id=RunId("run-block-admitted"),
        system_actor_id="system:sourcing",
        admitted_at=NOW + timedelta(minutes=1),
    )
    factory.state["admissions"][case_blocked_key] = factory.state["admissions"][
        case_blocked_key
    ].block(
        AdmissionBlockedReason.CASE_STATE_MISMATCH,
        blocked_at=NOW,
    )
    changed_need = ValidatedNeedId("need-block-changed")
    changed_command = _open_command().model_copy(
        update={
            "need": _open_command().need.model_copy(update={"need_id": changed_need}),
            "trigger_key": f"sourcing-case:v2:{TENANT}:{changed_need}",
        }
    )
    changed_case = await service.open_case(TENANT, changed_command, actor=SYSTEM)
    changed_id = await service.enqueue_admission(
        TENANT,
        changed_case,
        changed_need,
        ready_at=NOW - timedelta(hours=2),
        command=SourcingAdmissionEnqueueCommand(
            facts=None,
            blocked_reason="priority_facts_invalid",
        ),
        actor=SYSTEM,
    )
    before_immutable = {
        admission_id: copy.deepcopy(factory.state["admissions"][(TENANT, admission_id)])
        for admission_id in (other_id, starting_id, admitted_id, case_blocked_id)
    }
    snapshots_before = copy.deepcopy(factory.state["priority_snapshots"])

    blocked = await service.block_cluster_admissions(
        TENANT,
        cluster_id,
        changed_need,
        blocked_at=NOW - timedelta(minutes=1),
        actor=SYSTEM,
    )
    state_after_first = copy.deepcopy(factory.state["admissions"])
    replayed = await service.block_cluster_admissions(
        TENANT,
        cluster_id,
        changed_need,
        blocked_at=NOW + timedelta(minutes=2),
        actor=SYSTEM,
    )

    assert blocked == replayed == tuple(sorted((first_id, second_id, changed_id)))
    assert factory.state["admissions"] == state_after_first
    assert factory.state["priority_snapshots"] == snapshots_before
    for admission_id in (first_id, second_id, changed_id):
        admission = factory.state["admissions"][(TENANT, admission_id)]
        assert admission.state is AdmissionState.BLOCKED
        assert admission.blocked_reason is AdmissionBlockedReason.PRIORITY_FACTS_INVALID
        assert admission.updated_at == NOW
    for admission_id, before in before_immutable.items():
        assert factory.state["admissions"][(TENANT, admission_id)] == before


@pytest.mark.asyncio
async def test_transient_facts_absence_never_persists_a_blocked_admission() -> None:
    """暂时读不到事实由 application 停止本轮，领域 refresh 不固化 blocked。"""

    factory = _Factory()
    service = _service(factory)
    case_id = await _opened(service)
    admission_id = await _enqueue(service, case_id, _open_command().need.need_id)
    admission_before = copy.deepcopy(factory.state["admissions"])
    snapshots_before = copy.deepcopy(factory.state["priority_snapshots"])
    uow_calls_before = factory.calls

    with pytest.raises(ValidationError, match="排序事实无效"):
        await service.refresh_admission(
            TENANT,
            admission_id,
            facts=cast(Any, None),
            refreshed_at=NOW + timedelta(minutes=1),
            actor=SYSTEM,
        )

    assert factory.calls == uow_calls_before
    assert factory.state["admissions"] == admission_before
    assert factory.state["priority_snapshots"] == snapshots_before


@pytest.mark.asyncio
async def test_invalid_priority_facts_block_only_the_target_admission() -> None:
    """显式永久无效事实只形成目标 Need 的固定 blocked 记录。"""

    factory = _Factory()
    service = _service(factory)
    first_case = await _opened(service)
    first_need = _open_command().need.need_id
    waiting_id = await _enqueue(service, first_case, first_need)
    second_need = ValidatedNeedId("need-service-invalid")
    second_command = _open_command().model_copy(
        update={
            "need": _open_command().need.model_copy(update={"need_id": second_need}),
            "trigger_key": f"sourcing-case:v2:{TENANT}:{second_need}",
        }
    )
    second_case = await service.open_case(TENANT, second_command, actor=SYSTEM)

    blocked_id = await service.enqueue_admission(
        TENANT,
        second_case,
        second_need,
        ready_at=NOW - timedelta(hours=1),
        command=SourcingAdmissionEnqueueCommand(
            facts=None, blocked_reason="priority_facts_invalid"
        ),
        actor=SYSTEM,
    )

    waiting = await service.list_admissions(
        TENANT, state=AdmissionState.WAITING, limit=20, now=NOW, actor=BOSS
    )
    blocked = await service.list_admissions(
        TENANT, state=AdmissionState.BLOCKED, limit=20, now=NOW, actor=BOSS
    )
    assert [item.admission_id for item in waiting] == [waiting_id]
    assert [item.admission_id for item in blocked] == [blocked_id]
    assert blocked[0].blocked_reason == "priority_facts_invalid"
    assert blocked[0].snapshot_id is None


@pytest.mark.asyncio
async def test_priority_invalid_recovers_but_case_mismatch_does_not() -> None:
    """有效 refresh 只恢复 priority_facts_invalid，不能清除 Case 阻断。"""

    factory = _Factory()
    service = _service(factory)
    case_id = await _opened(service)
    need_id = _open_command().need.need_id
    admission_id = await service.enqueue_admission(
        TENANT,
        case_id,
        need_id,
        ready_at=NOW - timedelta(hours=1),
        command=SourcingAdmissionEnqueueCommand(
            facts=None, blocked_reason="priority_facts_invalid"
        ),
        actor=SYSTEM,
    )
    recovered_snapshot = await service.refresh_admission(
        TENANT,
        admission_id,
        facts=_priority_facts(need_id),
        refreshed_at=NOW,
        actor=SYSTEM,
    )
    assert recovered_snapshot is not None
    recovered = await service.get_admission(TENANT, admission_id, actor=BOSS)
    assert recovered is not None and recovered.state == "waiting"

    await service.block_admission(
        TENANT,
        admission_id,
        reason=AdmissionBlockedReason.CASE_STATE_MISMATCH,
        blocked_at=NOW + timedelta(minutes=1),
        actor=SYSTEM,
    )
    refreshed_snapshot = await service.refresh_admission(
        TENANT,
        admission_id,
        facts=_priority_facts(
            need_id,
            count=2,
            cluster_id=NeedClusterId("cluster-case-mismatch"),
            observed_at=NOW + timedelta(minutes=1),
        ),
        refreshed_at=NOW + timedelta(minutes=2),
        actor=SYSTEM,
    )
    blocked = await service.get_admission(TENANT, admission_id, actor=BOSS)
    assert refreshed_snapshot is not None
    assert blocked is not None
    assert blocked.state == "blocked"
    assert blocked.blocked_reason == "case_state_mismatch"
    assert blocked.snapshot_id == refreshed_snapshot


@pytest.mark.asyncio
async def test_priority_invalid_with_current_snapshot_recovers_on_same_hash() -> None:
    """事实恢复到先前值时，snapshot 去重也必须解除 priority-invalid 阻断。"""

    service = _service(_Factory())
    case_id = await _opened(service)
    need_id = _open_command().need.need_id
    admission_id = await _enqueue(service, case_id, need_id)
    before = await service.get_admission(TENANT, admission_id, actor=BOSS)
    assert before is not None and before.snapshot_id is not None
    await service.block_admission(
        TENANT,
        admission_id,
        reason=AdmissionBlockedReason.PRIORITY_FACTS_INVALID,
        blocked_at=NOW + timedelta(minutes=1),
        actor=SYSTEM,
    )

    canonical_snapshot = await service.refresh_admission(
        TENANT,
        admission_id,
        facts=_priority_facts(need_id),
        refreshed_at=NOW + timedelta(minutes=2),
        actor=SYSTEM,
    )

    recovered = await service.get_admission(TENANT, admission_id, actor=BOSS)
    assert canonical_snapshot == before.snapshot_id
    assert recovered is not None and recovered.state == "waiting"
    assert recovered.blocked_reason is None


@pytest.mark.asyncio
async def test_stale_claim_token_cannot_complete_release_or_block() -> None:
    """旧 worker 的三个结束动作都不能覆盖当前 starting 租约。"""

    service = _service(_Factory())
    case_id = await _opened(service)
    admission_id = await _enqueue(service, case_id, _open_command().need.need_id)
    await service.claim_admissions(
        TENANT,
        limit=1,
        claim_token="current-claim",
        claim_expires_at=NOW + timedelta(minutes=5),
        actor=SYSTEM,
    )

    with pytest.raises(InvalidStateTransition, match="claim token"):
        await service.complete_admission(
            TENANT,
            admission_id,
            claim_token="stale-claim",
            workflow_run_id=RunId("run-stale"),
            admitted_at=NOW + timedelta(minutes=1),
            actor=SYSTEM,
        )
    with pytest.raises(InvalidStateTransition, match="claim token"):
        await service.release_admission_claim(
            TENANT,
            admission_id,
            claim_token="stale-claim",
            released_at=NOW + timedelta(minutes=1),
            actor=SYSTEM,
        )
    with pytest.raises(InvalidStateTransition, match="claim token"):
        await service.block_admission(
            TENANT,
            admission_id,
            reason=AdmissionBlockedReason.PRIORITY_FACTS_INVALID,
            blocked_at=NOW + timedelta(minutes=1),
            claim_token="stale-claim",
            actor=SYSTEM,
        )


@pytest.mark.asyncio
async def test_matching_claim_release_and_expiry_return_to_waiting() -> None:
    """已知暂态失败和到期未知执行都只释放匹配 starting 记录。"""

    service = _service(_Factory())
    case_id = await _opened(service)
    admission_id = await _enqueue(service, case_id, _open_command().need.need_id)
    await service.claim_admissions(
        TENANT,
        limit=1,
        claim_token="claim-release",
        claim_expires_at=NOW + timedelta(minutes=5),
        actor=SYSTEM,
    )
    await service.release_admission_claim(
        TENANT,
        admission_id,
        claim_token="claim-release",
        released_at=NOW,
        actor=SYSTEM,
    )
    released = await service.get_admission(TENANT, admission_id, actor=BOSS)
    assert released is not None and released.state == "waiting"

    claimed_again = await service.claim_admissions(
        TENANT,
        limit=1,
        claim_token="claim-expiring",
        claim_expires_at=NOW + timedelta(minutes=5),
        actor=SYSTEM,
    )
    assert [item.admission_id for item in claimed_again] == [admission_id]
    expired = await service.release_expired_admission_claims(
        TENANT, now=NOW + timedelta(minutes=5), actor=SYSTEM
    )
    assert [item.admission_id for item in expired] == [admission_id]
    after_expiry = await service.get_admission(TENANT, admission_id, actor=BOSS)
    assert after_expiry is not None and after_expiry.state == "waiting"


@pytest.mark.asyncio
async def test_admission_list_preserves_repository_order_and_builds_safe_views() -> (
    None
):
    """服务不得重新排序，也不得暴露租约或 Workflow 字段。"""

    factory = _Factory()
    service = _service(factory)
    first_case = await _opened(service)
    first_need = _open_command().need.need_id
    await _enqueue(
        service,
        first_case,
        first_need,
        count=3,
        cluster_id=NeedClusterId("cluster-three"),
    )
    second_need = ValidatedNeedId("need-service-eight")
    second_command = _open_command().model_copy(
        update={
            "need": _open_command().need.model_copy(update={"need_id": second_need}),
            "trigger_key": f"sourcing-case:v2:{TENANT}:{second_need}",
        }
    )
    second_case = await service.open_case(TENANT, second_command, actor=SYSTEM)
    await _enqueue(
        service,
        second_case,
        second_need,
        count=8,
        cluster_id=NeedClusterId("cluster-eight"),
        ready_at=NOW - timedelta(hours=1),
    )

    views = await service.list_admissions(
        TENANT, state=AdmissionState.WAITING, limit=20, now=NOW, actor=BOSS
    )

    assert [item.need_id for item in views] == [second_need, first_need]
    assert views[0].waiting_duration_seconds == 3600
    assert views[0].explanation == (
        "该需求簇当前有 8 条已验证需求；同规模需求按等待时间排序。"
    )
    assert set(views[0].model_dump()) == {
        "admission_id",
        "case_id",
        "need_id",
        "state",
        "blocked_reason",
        "snapshot_id",
        "cluster_id",
        "cluster_member_count",
        "ready_at",
        "facts_observed_at",
        "ranking_version",
        "explanation",
        "waiting_duration_seconds",
        "admitted_at",
        "admitted_by",
        "can_current_user_manual_start",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("actor", "can_manual_start"),
    [
        (BOSS, True),
        (SOURCING, True),
        (PRODUCT, False),
        (FINANCE, False),
    ],
)
async def test_admission_safe_views_apply_role_and_state_manual_start_policy(
    actor: SourcingActor, can_manual_start: bool
) -> None:
    """四类内部读角色都可见；只有 boss/sourcing 的 waiting 可手动启动。"""

    factory = _Factory()
    service = _service(factory)
    case_id = await _opened(service)
    admission_id = await _enqueue(service, case_id, _open_command().need.need_id)

    waiting_detail = await service.get_admission(TENANT, admission_id, actor=actor)
    waiting_list = await service.list_admissions(
        TENANT,
        state=AdmissionState.WAITING,
        limit=10,
        now=NOW,
        actor=actor,
    )
    assert waiting_detail is not None
    assert waiting_detail.can_current_user_manual_start is can_manual_start
    assert [item.admission_id for item in waiting_list] == [admission_id]
    assert waiting_list[0].can_current_user_manual_start is can_manual_start

    await service.block_admission(
        TENANT,
        admission_id,
        reason=AdmissionBlockedReason.CASE_STATE_MISMATCH,
        blocked_at=NOW + timedelta(minutes=1),
        actor=SYSTEM,
    )
    blocked_detail = await service.get_admission(TENANT, admission_id, actor=actor)
    blocked_list = await service.list_admissions(
        TENANT,
        state=AdmissionState.BLOCKED,
        limit=10,
        now=NOW + timedelta(minutes=1),
        actor=actor,
    )
    assert blocked_detail is not None
    assert blocked_detail.can_current_user_manual_start is False
    assert [item.admission_id for item in blocked_list] == [admission_id]
    assert blocked_list[0].can_current_user_manual_start is False


@pytest.mark.asyncio
async def test_admission_authorization_precedes_uow_construction() -> None:
    """拒绝身份时，连事务对象都不能构造。"""

    factory = _Factory()
    service = _service(factory)
    other_system = SourcingActor(
        "other-system", OTHER_TENANT, SourcingScope.SYSTEM, "system"
    )
    with pytest.raises(PermissionDenied):
        await service.enqueue_admission(
            TENANT,
            SourcingCaseId("src-forbidden"),
            ValidatedNeedId("need-forbidden"),
            ready_at=NOW,
            command=SourcingAdmissionEnqueueCommand(
                facts=_priority_facts(ValidatedNeedId("need-forbidden"))
            ),
            actor=other_system,
        )
    assert factory.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        "refresh",
        "refresh_cluster",
        "block_cluster",
        "claim",
        "complete",
        "release_expired",
        "release",
        "block",
        "list",
        "get",
    ],
)
async def test_every_admission_entry_authorizes_before_uow(operation: str) -> None:
    """任一 admission 入口被拒绝时都不能构造事务。"""

    factory = _Factory()
    service = _service(factory)
    other_system = SourcingActor(
        "other-system", OTHER_TENANT, SourcingScope.SYSTEM, "system"
    )
    other_boss = SourcingActor("other-boss", OTHER_TENANT, SourcingScope.TENANT, "boss")
    admission_id = SourcingAdmissionId("sad-forbidden")
    with pytest.raises(PermissionDenied):
        if operation == "refresh":
            await service.refresh_admission(
                TENANT,
                admission_id,
                facts=_priority_facts(ValidatedNeedId("need-forbidden")),
                refreshed_at=NOW,
                actor=other_system,
            )
        elif operation == "refresh_cluster":
            changed_need_id = ValidatedNeedId("need-cluster-forbidden")
            await service.refresh_cluster_admissions(
                TENANT,
                changed_need_id,
                facts=_priority_facts(
                    changed_need_id,
                    cluster_id=NeedClusterId("cluster-forbidden"),
                ),
                refreshed_at=NOW,
                actor=other_system,
            )
        elif operation == "block_cluster":
            await service.block_cluster_admissions(
                TENANT,
                NeedClusterId("cluster-block-forbidden"),
                ValidatedNeedId("need-block-forbidden"),
                blocked_at=NOW,
                actor=other_system,
            )
        elif operation == "claim":
            await service.claim_admissions(
                TENANT,
                limit=1,
                claim_token="claim-forbidden",
                claim_expires_at=NOW + timedelta(minutes=1),
                actor=other_system,
            )
        elif operation == "complete":
            await service.complete_admission(
                TENANT,
                admission_id,
                claim_token="claim-forbidden",
                workflow_run_id=RunId("run-forbidden"),
                admitted_at=NOW,
                actor=other_system,
            )
        elif operation == "release_expired":
            await service.release_expired_admission_claims(
                TENANT, now=NOW, actor=other_system
            )
        elif operation == "release":
            await service.release_admission_claim(
                TENANT,
                admission_id,
                claim_token="claim-forbidden",
                released_at=NOW,
                actor=other_system,
            )
        elif operation == "block":
            await service.block_admission(
                TENANT,
                admission_id,
                reason=AdmissionBlockedReason.CASE_STATE_MISMATCH,
                blocked_at=NOW,
                actor=other_system,
            )
        elif operation == "list":
            await service.list_admissions(
                TENANT,
                state=AdmissionState.WAITING,
                limit=1,
                now=NOW,
                actor=other_boss,
            )
        else:
            await service.get_admission(TENANT, admission_id, actor=other_boss)
    assert factory.calls == 0


@pytest.mark.asyncio
async def test_admission_rejects_mismatch_and_naive_time_before_write() -> None:
    """Case/Need 与 UTC 边界错误不能留下任何 admission。"""

    factory = _Factory()
    service = _service(factory)
    case_id = await _opened(service)
    calls_after_case = factory.calls
    with pytest.raises(ValidationError, match="Need"):
        await service.enqueue_admission(
            TENANT,
            case_id,
            _open_command().need.need_id,
            ready_at=NOW,
            command=SourcingAdmissionEnqueueCommand(
                facts=_priority_facts(ValidatedNeedId("need-other"))
            ),
            actor=SYSTEM,
        )
    with pytest.raises(ValidationError, match="UTC"):
        await service.enqueue_admission(
            TENANT,
            case_id,
            _open_command().need.need_id,
            ready_at=NOW.replace(tzinfo=None),
            command=SourcingAdmissionEnqueueCommand(
                facts=_priority_facts(_open_command().need.need_id)
            ),
            actor=SYSTEM,
        )
    assert factory.calls == calls_after_case
    assert factory.state["admissions"] == {}


async def _opened(service: Any) -> SourcingCaseId:
    return await service.open_case(TENANT, _open_command(), actor=SYSTEM)


async def _discovering(service: Any) -> SourcingCaseId:
    case_id = await _opened(service)
    for rung in range(1, 6):
        await service.record_ladder_check(
            TENANT, case_id, _check(case_id, rung), actor=SYSTEM
        )
    return case_id


async def _verifying_case_with_frozen_specs(
    service: Any, *, model: str | None
) -> SourcingCaseId:
    """为直提候选核验冻结明确的客户规格，而非相信候选自报 required。"""

    need = _open_command().need.model_copy(
        update={
            "material": NeedFact(value="steel", provenance=_provenance()),
            "size_spec": NeedFact(value="4 inch", provenance=_provenance()),
            "model": (
                None
                if model is None
                else NeedFact(value=model, provenance=_provenance())
            ),
        }
    )
    case_id = await service.open_case(
        TENANT,
        _open_command().model_copy(update={"need": need}),
        actor=SYSTEM,
    )
    for rung in range(1, 6):
        await service.record_ladder_check(
            TENANT, case_id, _check(case_id, rung), actor=SYSTEM
        )
    plan = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    await service.confirm_public_plan(TENANT, plan.plan_id, plan.plan_hash, actor=BOSS)
    return case_id


def _candidate_with_frozen_required_values() -> CandidateSubmission:
    expected = {
        "product_type": "hinges",
        "material": "steel",
        "size": "4 inch",
        "model": "HX-4",
    }
    submission = _candidate()
    return submission.model_copy(
        update={
            "specs": tuple(
                item.model_copy(update={"required": expected[item.spec_name]})
                for item in submission.specs
            )
        }
    )


@pytest.mark.asyncio
async def test_submit_candidate_rejects_model_omitted_from_frozen_need() -> None:
    factory = _Factory()
    service = _service(factory)
    case_id = await _verifying_case_with_frozen_specs(service, model="HX-4")
    valid = _candidate_with_frozen_required_values()
    submission = valid.model_copy(
        update={
            "specs": tuple(item for item in valid.specs if item.spec_name != "model")
        }
    )

    candidate_id = await service.submit_candidate(
        TENANT, case_id, submission, actor=SYSTEM
    )

    stored = factory.state["candidates"][(TENANT, candidate_id)]
    assert stored.rejected is True
    assert "verification_incomplete" in {
        item.value for item in stored.rejection_reasons
    }
    assert stored.passes_verification()[0] is False
    assert "model" in stored.passes_verification()[1]


@pytest.mark.asyncio
async def test_submit_candidate_rejects_required_value_drift_from_frozen_need() -> None:
    factory = _Factory()
    service = _service(factory)
    case_id = await _verifying_case_with_frozen_specs(service, model="HX-4")
    valid = _candidate_with_frozen_required_values()
    drifted = valid.model_copy(
        update={
            "specs": tuple(
                item.model_copy(update={"required": "HX-5"})
                if item.spec_name == "model"
                else item
                for item in valid.specs
            )
        }
    )

    with pytest.raises(ValidationError, match="冻结需求"):
        await service.submit_candidate(TENANT, case_id, drifted, actor=SYSTEM)

    assert factory.state["candidates"] == {}


@pytest.mark.asyncio
async def test_submit_candidate_rejects_material_and_size_not_declared_by_frozen_need() -> (
    None
):
    """完整度 3 不能让候选输入把未验证规格伪造成客户需求。"""

    factory = _Factory()
    service = _service(factory)
    incomplete_need = _open_command().need.model_copy(
        update={"material": None, "size_spec": None}
    )
    case_id = await service.open_case(
        TENANT,
        _open_command().model_copy(update={"need": incomplete_need}),
        actor=SYSTEM,
    )
    for rung in range(1, 6):
        await service.record_ladder_check(
            TENANT, case_id, _check(case_id, rung), actor=SYSTEM
        )
    plan = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    await service.confirm_public_plan(TENANT, plan.plan_id, plan.plan_hash, actor=BOSS)

    case = factory.state["cases"][(TENANT, case_id)]
    assert case.need_snapshot is not None
    assert case.need_snapshot.completeness == 3
    assert case.need_snapshot.material is None
    assert case.need_snapshot.size_spec is None

    with pytest.raises(ValidationError, match="冻结需求未声明"):
        await service.submit_candidate(TENANT, case_id, _candidate(), actor=SYSTEM)

    assert factory.state["candidates"] == {}


@pytest.mark.asyncio
async def test_submit_candidate_does_not_require_model_absent_from_frozen_need() -> (
    None
):
    factory = _Factory()
    service = _service(factory)
    case_id = await _verifying_case_with_frozen_specs(service, model=None)
    valid = _candidate_with_frozen_required_values()
    no_model = valid.model_copy(
        update={
            "specs": tuple(item for item in valid.specs if item.spec_name != "model")
        }
    )

    candidate_id = await service.submit_candidate(
        TENANT, case_id, no_model, actor=SYSTEM
    )

    stored = factory.state["candidates"][(TENANT, candidate_id)]
    assert stored.rejected is False
    assert stored.passes_verification() == (True, [])


async def _running_public_case(
    service: Any, factory: _Factory
) -> tuple[SourcingCaseId, Any, RunId]:
    command = _open_command()
    need = command.need.model_copy(
        update={
            "material": NeedFact(value="steel", provenance=_provenance()),
            "size_spec": NeedFact(value="4 inch", provenance=_provenance()),
            "model": NeedFact(value="HX-4", provenance=_provenance()),
        }
    )
    case_id = await service.open_case(
        TENANT, command.model_copy(update={"need": need}), actor=SYSTEM
    )
    for rung in range(1, 6):
        await service.record_ladder_check(
            TENANT, case_id, _check(case_id, rung), actor=SYSTEM
        )
    plan = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    await service.confirm_public_plan(TENANT, plan.plan_id, plan.plan_hash, actor=BOSS)
    await service.authorize_public_plan_run(
        TENANT, case_id, plan.plan_id, plan.plan_hash, actor=BOSS
    )
    run_id = RunId("run-public-verification")
    draft = PublicCandidateDraft(
        draft_id="scd-public-qualified",
        tenant_id=TENANT,
        case_id=case_id,
        run_id=run_id,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        query_index=0,
        result_index=0,
        source_key="d" * 64,
        supplier_name="Factory A",
        product_title="Stainless hinge HX-4",
        specs=(
            PublicCandidateDraftSpec(
                spec_name="product_type", required="hinges", observed="hinges"
            ),
            PublicCandidateDraftSpec(
                spec_name="material", required="steel", observed="steel"
            ),
            PublicCandidateDraftSpec(
                spec_name="size", required="4 inch", observed="4 inch"
            ),
            PublicCandidateDraftSpec(
                spec_name="model", required="HX-4", observed="HX-4"
            ),
        ),
        moq=500,
        indicative_price_tiers=(
            PublicCandidateDraftPriceTier(
                minimum_quantity=1000,
                amount=Decimal("1.25"),
                currency="USD",
                unit="piece",
            ),
        ),
        rejection_codes=(),
        evidence_url="https://factory.example/hinge",
        evidence_observed_at=NOW - timedelta(hours=2),
        evidence_hash="c" * 64,
        evidence_artifact_ref=ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0X"),
        created_at=NOW - timedelta(hours=2),
    )
    factory.state["candidate_drafts"][(TENANT, draft.draft_id)] = draft
    return case_id, plan, run_id


@pytest.mark.asyncio
async def test_public_draft_verification_converts_four_specs_and_seals_once() -> None:
    factory = _Factory()
    reader = _EvidenceReader()
    service = _service(factory, reader)
    case_id, plan, run_id = await _running_public_case(service, factory)

    result = await service.verify_public_candidate_drafts(
        TENANT,
        case_id,
        VerifyPublicCandidateDraftsCommand(
            run_id=run_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            draft_ids=("scd-public-qualified",),
        ),
        actor=SYSTEM,
    )

    assert result.calibration_draft_ids == ()
    assert result.rejected_candidate_ids == ()
    assert result.converted_candidate_ids == result.qualified_candidate_ids
    assert result.verified_event is not None
    assert result.verified_event.candidate_ids == result.qualified_candidate_ids
    candidate = factory.state["candidates"][(TENANT, result.qualified_candidate_ids[0])]
    assert [item.spec_name for item in candidate.verified_specs] == [
        "product_type",
        "material",
        "size",
        "model",
    ]
    assert candidate.public_draft_source_key == "d" * 64
    assert candidate.supplier_claims == {}
    assert set(candidate.observed_facts) == {
        "supplier_name",
        "product_title",
        "product_type",
        "material",
        "size",
        "model",
        "moq",
        "price_unit",
        "currency",
    }
    assert all(
        fact.evidence_ref == ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0X")
        and fact.provenance.source_type is SourceType.WEB_PAGE
        and fact.provenance.source_id == "art_01K39P9M5D6K4A91YEQ80EJZ0X"
        and fact.provenance.confirmed_by is None
        for fact in candidate.observed_facts.values()
    )
    assert candidate.observed_facts["supplier_name"].value == "Factory A"
    assert candidate.observed_facts["product_title"].value == "Stainless hinge HX-4"
    assert reader.calls == 1


def _stored_public_draft(factory: _Factory) -> PublicCandidateDraft:
    return next(iter(factory.state["candidate_drafts"].values()))


def _replace_public_drafts(factory: _Factory, *drafts: PublicCandidateDraft) -> None:
    factory.state["candidate_drafts"] = {
        (TENANT, draft.draft_id): draft for draft in drafts
    }


def _verification_command(plan: Any, run_id: RunId, *draft_ids: str) -> Any:
    return VerifyPublicCandidateDraftsCommand(
        run_id=run_id,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        draft_ids=tuple(draft_ids),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_model", "unequal_material", "moq"])
async def test_complete_public_draft_failures_persist_rejected_without_seal(
    failure: str,
) -> None:
    factory = _Factory()
    service = _service(factory)
    case_id, plan, run_id = await _running_public_case(service, factory)
    draft = _stored_public_draft(factory)
    if failure == "missing_model":
        specs = tuple(
            item.model_copy(update={"observed": None})
            if item.spec_name == "model"
            else item
            for item in draft.specs
        )
        draft = draft.model_copy(update={"specs": specs})
    elif failure == "unequal_material":
        specs = tuple(
            item.model_copy(update={"observed": "aluminium"})
            if item.spec_name == "material"
            else item
            for item in draft.specs
        )
        draft = draft.model_copy(update={"specs": specs})
    else:
        draft = draft.model_copy(update={"moq": 6000})
    _replace_public_drafts(factory, draft)

    result = await service.verify_public_candidate_drafts(
        TENANT,
        case_id,
        _verification_command(plan, run_id, draft.draft_id),
        actor=SYSTEM,
    )

    assert result.verified_event is None
    assert result.qualified_candidate_ids == ()
    assert result.rejected_candidate_ids == result.converted_candidate_ids
    candidate = next(iter(factory.state["candidates"].values()))
    assert candidate.rejected is True
    assert candidate.supplier_claims == {}
    if failure == "missing_model":
        assert "model" not in candidate.observed_facts
    if failure == "unequal_material":
        material = next(
            item for item in candidate.verified_specs if item.spec_name == "material"
        )
        assert material.level is SpecMatchLevel.DIFFERENT
        assert material.substitutable is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "draft_only",
    ["identity", "price", "mixed_currency", "mixed_unit", "duplicate_tier"],
)
async def test_incomplete_or_ambiguous_public_price_stays_draft_only(
    draft_only: str,
) -> None:
    factory = _Factory()
    reader = _EvidenceReader()
    service = _service(factory, reader)
    case_id, plan, run_id = await _running_public_case(service, factory)
    draft = _stored_public_draft(factory)
    if draft_only == "identity":
        draft = draft.model_copy(
            update={
                "supplier_name": None,
                "rejection_codes": ("supplier_identity_missing",),
            }
        )
    elif draft_only == "price":
        draft = draft.model_copy(
            update={
                "indicative_price_tiers": (),
                "rejection_codes": ("quantity_tier_missing",),
            }
        )
    else:
        first = draft.indicative_price_tiers[0]
        second = first.model_copy(
            update={
                "minimum_quantity": 2000
                if draft_only != "duplicate_tier"
                else first.minimum_quantity,
                "currency": "EUR" if draft_only == "mixed_currency" else "USD",
                "unit": "set" if draft_only == "mixed_unit" else "piece",
            }
        )
        draft = draft.model_copy(update={"indicative_price_tiers": (first, second)})
    _replace_public_drafts(factory, draft)

    result = await service.verify_public_candidate_drafts(
        TENANT,
        case_id,
        _verification_command(plan, run_id, draft.draft_id),
        actor=SYSTEM,
    )

    assert result.calibration_draft_ids == (draft.draft_id,)
    assert result.converted_candidate_ids == ()
    assert result.verified_event is None
    assert factory.state["candidates"] == {}
    assert reader.calls == 0


@pytest.mark.asyncio
async def test_fourth_complete_candidate_is_persisted_rejected_and_not_sealed() -> None:
    factory = _Factory()
    service = _service(factory)
    case_id, plan, run_id = await _running_public_case(service, factory)
    first = _stored_public_draft(factory)
    drafts = tuple(
        first.model_copy(
            update={
                "draft_id": f"scd-public-{index}",
                "result_index": index,
                "source_key": f"{index + 1:064x}",
                "supplier_name": f"Factory {index}",
            }
        )
        for index in range(4)
    )
    _replace_public_drafts(factory, *drafts)

    result = await service.verify_public_candidate_drafts(
        TENANT,
        case_id,
        _verification_command(plan, run_id, *(draft.draft_id for draft in drafts)),
        actor=SYSTEM,
    )

    assert len(result.converted_candidate_ids) == 4
    assert len(result.qualified_candidate_ids) == 3
    assert len(result.rejected_candidate_ids) == 1
    rejected = factory.state["candidates"][(TENANT, result.rejected_candidate_ids[0])]
    assert rejected.rejection_reasons == [PriceRejectionReason.QUALIFIED_LIMIT_REACHED]


@pytest.mark.asyncio
async def test_verification_requires_exact_canonical_draft_order_before_artifact_reads() -> (
    None
):
    factory = _Factory()
    reader = _EvidenceReader()
    service = _service(factory, reader)
    case_id, plan, run_id = await _running_public_case(service, factory)
    first = _stored_public_draft(factory)
    second = first.model_copy(
        update={
            "draft_id": "scd-public-second",
            "result_index": 1,
            "source_key": "e" * 64,
        }
    )
    _replace_public_drafts(factory, first, second)

    with pytest.raises(ValidationError, match="集合或顺序"):
        await service.verify_public_candidate_drafts(
            TENANT,
            case_id,
            _verification_command(plan, run_id, second.draft_id, first.draft_id),
            actor=SYSTEM,
        )

    assert factory.state["candidates"] == {}
    assert reader.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "binding_failure",
    [
        "missing",
        "extra",
        "reordered",
        "foreign_tenant",
        "foreign_case",
        "foreign_run",
        "foreign_plan",
        "foreign_hash",
    ],
)
async def test_public_verification_rejects_exact_draft_set_and_binding_drift_before_writes(
    binding_failure: str,
) -> None:
    factory = _Factory()
    reader = _EvidenceReader()
    service = _service(factory, reader)
    case_id, plan, run_id = await _running_public_case(service, factory)
    first = _stored_public_draft(factory)
    second = first.model_copy(
        update={
            "draft_id": "scd-public-second",
            "result_index": 1,
            "source_key": "e" * 64,
        }
    )
    if binding_failure in {"missing", "extra", "reordered"}:
        _replace_public_drafts(factory, first, second)
        draft_ids = {
            "missing": (first.draft_id,),
            "extra": (first.draft_id, second.draft_id, "scd-missing"),
            "reordered": (second.draft_id, first.draft_id),
        }[binding_failure]
    else:
        updates: dict[str, object] = {
            "foreign_tenant": {"tenant_id": OTHER_TENANT},
            "foreign_case": {"case_id": SourcingCaseId("src-foreign")},
            "foreign_run": {"run_id": RunId("run-foreign")},
            "foreign_plan": {"plan_id": SourcingPlanId("spl-foreign")},
            "foreign_hash": {"plan_hash": "f" * 64},
        }[binding_failure]
        _replace_public_drafts(factory, first.model_copy(update=updates))
        draft_ids = (first.draft_id,)

    with pytest.raises(ValidationError, match="集合或顺序|绑定漂移") as caught:
        await service.verify_public_candidate_drafts(
            TENANT,
            case_id,
            _verification_command(plan, run_id, *draft_ids),
            actor=SYSTEM,
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert factory.state["candidates"] == {}
    assert reader.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "artifact_failure",
    ["url", "hash", "tenant", "time", "artifact", "reader"],
)
async def test_artifact_drift_or_reader_error_rolls_back_without_error_leak(
    artifact_failure: str,
) -> None:
    factory = _Factory()
    projection = _EvidenceReader().projection
    if artifact_failure == "url":
        projection = replace(projection, canonical_url="https://other.example/hinge")
    elif artifact_failure == "hash":
        projection = replace(projection, content_hash="f" * 64)
    elif artifact_failure == "tenant":
        projection = replace(projection, tenant_id=OTHER_TENANT)
    elif artifact_failure == "time":
        projection = replace(projection, observed_at=NOW - timedelta(hours=1))
    elif artifact_failure == "artifact":
        projection = replace(
            projection,
            artifact_id=ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0Y"),
        )
    reader = _EvidenceReader(
        projection=projection,
        failure=(
            RuntimeError("provider secret raw error")
            if artifact_failure == "reader"
            else None
        ),
    )
    service = _service(factory, reader)
    case_id, plan, run_id = await _running_public_case(service, factory)
    draft = _stored_public_draft(factory)

    with pytest.raises(
        MissingEvidenceSnapshotError,
        match="^公开候选草稿 Artifact 元数据不一致$",
    ) as caught:
        await service.verify_public_candidate_drafts(
            TENANT,
            case_id,
            _verification_command(plan, run_id, draft.draft_id),
            actor=SYSTEM,
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "secret" not in str(caught.value)
    assert factory.state["candidates"] == {}


@pytest.mark.asyncio
async def test_exact_public_draft_replay_returns_original_generation_without_duplicate() -> (
    None
):
    factory = _Factory()
    service = _service(factory)
    case_id, plan, run_id = await _running_public_case(service, factory)
    draft = _stored_public_draft(factory)
    command = _verification_command(plan, run_id, draft.draft_id)

    first = await service.verify_public_candidate_drafts(
        TENANT, case_id, command, actor=SYSTEM
    )
    second = await service.verify_public_candidate_drafts(
        TENANT, case_id, command, actor=SYSTEM
    )

    assert second == first
    assert len(factory.state["candidates"]) == 1
    assert (
        len(
            [
                event
                for event in factory.state["events"]
                if isinstance(event, SourcingCandidatesVerified)
            ]
        )
        == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "drift",
    [
        "supplier_name",
        "product_title",
        "spec",
        "spec_required",
        "spec_name",
        "moq",
        "price_amount",
        "price_minimum",
        "price_currency",
        "price_unit",
        "evidence_url",
        "evidence_hash",
        "evidence_time",
        "evidence_artifact",
        "reader_failure",
        "created_at",
        "conversion_state",
    ],
)
async def test_sealed_public_draft_replay_revalidates_full_proposal_and_artifact(
    drift: str,
) -> None:
    factory = _Factory()
    reader = _EvidenceReader()
    service = _service(factory, reader)
    case_id, plan, run_id = await _running_public_case(service, factory)
    draft = _stored_public_draft(factory)
    command = _verification_command(plan, run_id, draft.draft_id)
    first = await service.verify_public_candidate_drafts(
        TENANT, case_id, command, actor=SYSTEM
    )
    case_before = copy.deepcopy(factory.state["cases"][(TENANT, case_id)])
    candidates_before = copy.deepcopy(factory.state["candidates"])
    events_before = copy.deepcopy(factory.state["events"])
    if drift == "supplier_name":
        changed = draft.model_copy(update={"supplier_name": "Factory B"})
    elif drift == "product_title":
        changed = draft.model_copy(update={"product_title": "Changed hinge"})
    elif drift == "spec":
        changed = draft.model_copy(
            update={
                "specs": tuple(
                    item.model_copy(update={"observed": "brass"})
                    if item.spec_name == "material"
                    else item
                    for item in draft.specs
                )
            }
        )
    elif drift == "spec_required":
        changed = draft.model_copy(
            update={
                "specs": tuple(
                    item.model_copy(update={"required": "brass"})
                    if item.spec_name == "material"
                    else item
                    for item in draft.specs
                )
            }
        )
    elif drift == "spec_name":
        changed = draft.model_copy(
            update={
                "specs": tuple(
                    item.model_copy(update={"spec_name": "grade"})
                    if item.spec_name == "material"
                    else item
                    for item in draft.specs
                )
            }
        )
    elif drift == "moq":
        changed = draft.model_copy(update={"moq": 501})
    elif drift.startswith("price_"):
        tier_updates = {
            "price_amount": {"amount": Decimal("1.26")},
            "price_minimum": {"minimum_quantity": 999},
            "price_currency": {"currency": "EUR"},
            "price_unit": {"unit": "set"},
        }[drift]
        changed = draft.model_copy(
            update={
                "indicative_price_tiers": (
                    draft.indicative_price_tiers[0].model_copy(update=tier_updates),
                )
            }
        )
    elif drift == "evidence_url":
        changed = draft.model_copy(
            update={"evidence_url": "https://other.example/hinge"}
        )
    elif drift == "evidence_hash":
        changed = draft.model_copy(update={"evidence_hash": "e" * 64})
    elif drift == "evidence_time":
        changed = draft.model_copy(
            update={
                "evidence_observed_at": draft.evidence_observed_at
                + timedelta(seconds=1)
            }
        )
    elif drift == "evidence_artifact":
        changed = draft.model_copy(
            update={
                "evidence_artifact_ref": ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0Y")
            }
        )
    elif drift == "reader_failure":
        reader.failure = RuntimeError("provider secret raw error")
        changed = draft
    elif drift == "created_at":
        changed = draft.model_copy(
            update={"created_at": draft.created_at + timedelta(seconds=1)}
        )
    else:
        changed = draft.model_copy(
            update={
                "supplier_name": None,
                "rejection_codes": ("supplier_identity_missing",),
            }
        )
    _replace_public_drafts(factory, changed)

    with pytest.raises(
        (ValidationError, MissingEvidenceSnapshotError), match="公开候选"
    ) as caught:
        await service.verify_public_candidate_drafts(
            TENANT, case_id, command, actor=SYSTEM
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert factory.state["cases"][(TENANT, case_id)] == case_before
    assert factory.state["candidates"] == candidates_before
    assert factory.state["events"] == events_before
    assert first.verified_event is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("sealed", [False, True])
async def test_descending_public_price_tiers_are_canonical_before_persistence_and_replay(
    sealed: bool,
) -> None:
    factory = _Factory()
    service = _service(factory)
    case_id, plan, run_id = await _running_public_case(service, factory)
    draft = _stored_public_draft(factory)
    tiers = (
        draft.indicative_price_tiers[0].model_copy(
            update={"minimum_quantity": 2000, "amount": Decimal("1.00")}
        ),
        draft.indicative_price_tiers[0],
    )
    draft = draft.model_copy(
        update={
            "moq": 500 if sealed else 6000,
            "indicative_price_tiers": tiers,
        }
    )
    _replace_public_drafts(factory, draft)
    command = _verification_command(plan, run_id, draft.draft_id)

    first = await service.verify_public_candidate_drafts(
        TENANT, case_id, command, actor=SYSTEM
    )
    second = await service.verify_public_candidate_drafts(
        TENANT, case_id, command, actor=SYSTEM
    )

    candidate = next(iter(factory.state["candidates"].values()))
    assert [tier.minimum_quantity for tier in candidate.indicative_price_tiers] == [
        1000,
        2000,
    ]
    assert second == first
    assert (first.verified_event is not None) is sealed


@pytest.mark.asyncio
async def test_public_draft_source_collision_with_changed_content_fails_closed() -> (
    None
):
    factory = _Factory()
    service = _service(factory)
    case_id, plan, run_id = await _running_public_case(service, factory)
    draft = _stored_public_draft(factory).model_copy(update={"moq": 6000})
    _replace_public_drafts(factory, draft)
    command = _verification_command(plan, run_id, draft.draft_id)
    first = await service.verify_public_candidate_drafts(
        TENANT, case_id, command, actor=SYSTEM
    )
    assert first.verified_event is None
    assert len(factory.state["candidates"]) == 1
    _replace_public_drafts(
        factory,
        draft.model_copy(update={"product_title": "Changed hinge identity"}),
    )

    with pytest.raises(ValidationError, match="source key 已绑定不同候选内容"):
        await service.verify_public_candidate_drafts(
            TENANT, case_id, command, actor=SYSTEM
        )

    assert len(factory.state["candidates"]) == 1
    assert not any(
        isinstance(event, SourcingCandidatesVerified)
        for event in factory.state["events"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["candidate", "case", "outbox"])
async def test_public_verification_failure_rolls_back_candidate_seal_and_event(
    failure_stage: str,
) -> None:
    factory = _Factory()
    service = _service(factory)
    case_id, plan, run_id = await _running_public_case(service, factory)
    draft = _stored_public_draft(factory)
    case_before = copy.deepcopy(factory.state["cases"][(TENANT, case_id)])
    events_before = copy.deepcopy(factory.state["events"])
    if failure_stage == "candidate":
        factory.state["fail_candidate_write"] = True
        expected = "candidate storage unavailable"
    elif failure_stage == "case":
        factory.state["fail_case_update"] = True
        expected = "case storage unavailable"
    else:
        factory.bus_fails = True
        expected = "outbox unavailable"

    with pytest.raises(RuntimeError, match=f"^{expected}$") as caught:
        await service.verify_public_candidate_drafts(
            TENANT,
            case_id,
            _verification_command(plan, run_id, draft.draft_id),
            actor=SYSTEM,
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert factory.state["cases"][(TENANT, case_id)] == case_before
    assert factory.state["candidates"] == {}
    assert factory.state["events"] == events_before


async def _internal_option(
    service: Any, case_id: SourcingCaseId, product_id: ProductId
) -> SourcingSupplyOptionId:
    qualified = _qualified_product_check(case_id, product_id)
    await service.record_ladder_check(TENANT, case_id, qualified, actor=SYSTEM)
    return await service.register_existing_product_option(
        TENANT, case_id, product_id, actor=SYSTEM
    )


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
        lambda: service.verify_public_candidate_drafts(
            OTHER_TENANT,
            case_id,
            VerifyPublicCandidateDraftsCommand(
                run_id=RunId("run-never-read"),
                plan_id=SourcingPlanId("spl-never-read"),
                plan_hash="a" * 64,
                draft_ids=("scd-never-read",),
            ),
            actor=SYSTEM,
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
        lambda: service.register_existing_product_option(
            OTHER_TENANT,
            case_id,
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
            OTHER_TENANT,
            case_id,
            OpportunityId("opp-1"),
            expected_need_id=ValidatedNeedId("need-service-1"),
            actor=SYSTEM,
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
    await service.record_ladder_check(TENANT, case_id, _check(case_id, 1), actor=SYSTEM)
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
async def test_ladder_exact_replay_is_noop_but_same_rung_drift_conflicts() -> None:
    factory = _Factory()
    service = _service(factory)
    case_id = await _opened(service)
    first = _check(case_id, 1)

    await service.record_ladder_check(TENANT, case_id, first, actor=SYSTEM)
    case_version = factory.state["cases"][(TENANT, case_id)].version
    await service.record_ladder_check(TENANT, case_id, first, actor=SYSTEM)

    assert len(factory.state["checks"]) == 1
    assert factory.state["cases"][(TENANT, case_id)].version == case_version
    with pytest.raises(ValidationError, match="同级事实冲突"):
        await service.record_ladder_check(
            TENANT,
            case_id,
            first.__class__(**{**first.__dict__, "conclusion": "changed"}),
            actor=SYSTEM,
        )


@pytest.mark.asyncio
async def test_existing_product_option_is_canonical_and_moves_internal_path_to_verifying() -> (
    None
):
    factory = _Factory()
    service = _service(factory)
    case_id = await _opened(service)
    product_id = ProductId("prd-existing")
    qualified = _qualified_product_check(case_id, product_id)
    await service.record_ladder_check(TENANT, case_id, qualified, actor=SYSTEM)

    first = await service.register_existing_product_option(
        TENANT, case_id, product_id, actor=SYSTEM
    )
    second = await service.register_existing_product_option(
        TENANT, case_id, product_id, actor=SYSTEM
    )

    assert first == second
    assert len(factory.state["options"]) == 1
    assert factory.state["cases"][(TENANT, case_id)].state is CaseState.VERIFYING
    await service.mark_candidates_ready(TENANT, case_id, (first,), (), actor=SYSTEM)
    assert factory.state["cases"][(TENANT, case_id)].state is CaseState.CANDIDATES_READY


@pytest.mark.asyncio
async def test_qualified_product_ladder_requires_nonempty_exact_comparisons() -> None:
    service = _service(_Factory())
    case_id = await _opened(service)
    product_id = ProductId("prd-evidence-bound")
    qualified = _check(
        case_id,
        1,
        outcome=LadderOutcome.QUALIFIED_SUPPLY_FOUND,
    )
    qualified = qualified.__class__(
        **{
            **qualified.__dict__,
            "input_snapshot": {
                **qualified.input_snapshot,
                "qualified_product_ids": [str(product_id)],
            },
            "match_object_type": "product",
            "match_object_id": str(product_id),
        }
    )

    with pytest.raises(ValidationError, match="逐项 exact"):
        await service.record_ladder_check(TENANT, case_id, qualified, actor=SYSTEM)

    different = qualified.__class__(
        **{
            **qualified.__dict__,
            "spec_comparisons": (
                SpecComparison(
                    spec_name="material",
                    required="304 stainless steel",
                    offered="201 stainless steel",
                    level=SpecMatchLevel.DIFFERENT,
                ),
            ),
            "evidence_refs": (ArtifactId("art-spec-evidence"),),
        }
    )
    with pytest.raises(ValidationError, match="逐项 exact"):
        await service.record_ladder_check(TENANT, case_id, different, actor=SYSTEM)


@pytest.mark.asyncio
async def test_ready_freezes_exact_internal_products_and_forbids_late_option_creation() -> (
    None
):
    factory = _Factory()
    service = _service(factory)
    case_id = await _opened(service)
    first_product = ProductId("prd-first")
    second_product = ProductId("prd-second")
    qualified = _qualified_product_check(case_id, first_product, second_product)
    await service.record_ladder_check(TENANT, case_id, qualified, actor=SYSTEM)
    first_option = await service.register_existing_product_option(
        TENANT, case_id, first_product, actor=SYSTEM
    )

    with pytest.raises(ValidationError, match="现有产品.*冻结集合"):
        await service.mark_candidates_ready(
            TENANT, case_id, (first_option,), (), actor=SYSTEM
        )

    second_option = await service.register_existing_product_option(
        TENANT, case_id, second_product, actor=SYSTEM
    )
    await service.mark_candidates_ready(
        TENANT, case_id, (first_option, second_option), (), actor=SYSTEM
    )
    assert (
        await service.register_existing_product_option(
            TENANT, case_id, first_product, actor=SYSTEM
        )
        == first_option
    )

    del factory.state["options"][(TENANT, second_option)]
    with pytest.raises(InvalidStateTransition, match="就绪后禁止新增"):
        await service.register_existing_product_option(
            TENANT, case_id, second_product, actor=SYSTEM
        )


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
        await service.save_public_plan(TENANT, case_id, _plan(case_id, 1), actor=BOSS)


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
        submission = submission.model_copy(update={"indicative_price_tiers": (tier,)})

    if mutation == "wrong_tier_ref":
        with pytest.raises(MissingEvidenceSnapshotError):
            await service.submit_candidate(TENANT, case_id, submission, actor=SOURCING)
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
async def test_submit_candidate_fails_closed_for_validation_bypassed_duplicate_specs() -> (
    None
):
    factory = _Factory()
    service = _service(factory)
    case_id = await _discovering(service)
    plan = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    await service.confirm_public_plan(TENANT, plan.plan_id, plan.plan_hash, actor=BOSS)
    submission = _candidate()
    incompatible = SpecComparisonView(
        spec_name=" MATERIAL ",
        required="required-material",
        offered="unverified-substitute",
        level="different",
        substitutable=False,
    )
    bypassed = submission.model_copy(
        update={"specs": (incompatible, *submission.specs)}
    )

    candidate_id = await service.submit_candidate(
        TENANT, case_id, bypassed, actor=SOURCING
    )

    stored = factory.state["candidates"][(TENANT, candidate_id)]
    assert stored.rejected is True
    passed, missing = stored.passes_verification()
    assert passed is False
    assert "duplicate_spec:material" in missing
    assert "incompatible_spec:material" in missing


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
    case_id = await _opened(service)
    with pytest.raises(ValidationError):
        await service.mark_candidates_ready(TENANT, case_id, (), (), actor=SYSTEM)
    option_id = await _internal_option(service, case_id, ProductId("prd-internal"))
    await service.mark_candidates_ready(TENANT, case_id, (option_id,), (), actor=SYSTEM)
    event = factory.state["events"][-1]
    assert isinstance(event, SourcingCandidatesReady)
    assert event.option_ids == (option_id,)
    assert event.candidate_ids == ()


@pytest.mark.asyncio
async def test_verified_candidates_require_cards_and_complete_frozen_ready_sets() -> (
    None
):
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
        await service.submit_candidate(TENANT, case_id, _candidate(), actor=SOURCING)

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
    del factory.state["options"][(TENANT, extra_option.option_id)]
    await service.mark_candidates_ready(
        TENANT,
        case_id,
        (option_id,),
        (candidate_id,),
        expected_case_version=verified.case_version,
        expected_candidate_set_hash=verified.candidate_set_hash,
        actor=SYSTEM,
    )
    ready = factory.state["events"][-1]
    assert isinstance(ready, SourcingCandidatesReady)
    assert ready.option_ids == (option_id,)
    assert ready.candidate_ids == (candidate_id,)
    assert (
        await service.register_supplier_candidate_option(
            TENANT,
            case_id,
            candidate_id,
            product_id,
            expected_case_version=verified.case_version,
            expected_candidate_set_hash=verified.candidate_set_hash,
            actor=SYSTEM,
        )
        == option_id
    )


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
async def test_candidate_product_inputs_rebuild_exact_sealed_generation() -> None:
    """从可变 Case 读到非封存候选或重算 generation 会使产品卡混代。"""

    service = _service(_Factory())
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

    projection = await service.get_candidate_product_inputs(
        TENANT,
        case_id,
        verified.candidate_ids,
        expected_case_version=verified.case_version,
        expected_candidate_set_hash=verified.candidate_set_hash,
        actor=SYSTEM,
    )

    assert projection.tenant_id == TENANT
    assert projection.case_id == case_id
    assert projection.candidate_ids == verified.candidate_ids
    assert projection.case_version == verified.case_version
    assert projection.candidate_set_hash == verified.candidate_set_hash
    assert len(projection.commands) == 1
    command = projection.commands[0]
    assert command.sourcing_case_id == case_id
    assert command.supplier_candidate_id == candidate_id
    assert command.name_zh == "Stainless hinge"
    assert command.name_en == "Stainless hinge"
    assert command.category == "hinges"
    assert command.moq == 500
    assert command.evidence_refs == (ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0X"),)
    assert command.indicative_prices[0].unit_amount == Decimal("1.25")

    with pytest.raises(ValidationError, match="generation"):
        await service.get_candidate_product_inputs(
            TENANT,
            case_id,
            verified.candidate_ids,
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
    case_id = await _opened(service)
    option_id = await _internal_option(service, case_id, ProductId("prd-main"))
    await service.mark_candidates_ready(TENANT, case_id, (option_id,), (), actor=SYSTEM)
    case = next(iter(factory.state["cases"].values()))
    review = await service.review(
        TENANT,
        case_id,
        SourcingReviewCommand(
            primary_option_id=option_id,
            alternate_option_ids=(),
            reason="证据最完整",
            expected_case_version=case.version,
        ),
        actor=SOURCING,
    )
    with pytest.raises(ValidationError):
        await service.hand_to_costing(
            TENANT,
            case_id,
            OpportunityId("opp-real"),
            expected_need_id=ValidatedNeedId("need-service-1"),
            actor=SYSTEM,
        )
    await service.confirm_review(TENANT, review.review_id, actor=BOSS)
    review_version = review.expected_case_version
    await service.record_waiting_stop(
        TENANT,
        case_id,
        "opportunity_required",
        actor=SYSTEM,
    )
    waiting = next(iter(factory.state["cases"].values()))
    assert waiting.version == review_version
    assert waiting.stop_code is SourcingStopCode.OPPORTUNITY_REQUIRED
    assert waiting.stop_detail == SourcingStopDetail(SourcingStopStage.COST_HANDOFF)
    snapshot = await service.hand_to_costing(
        TENANT,
        case_id,
        OpportunityId("opp-real"),
        expected_need_id=ValidatedNeedId("need-service-1"),
        actor=SYSTEM,
    )
    handed = next(iter(factory.state["cases"].values()))
    assert handed.state is CaseState.HANDED_TO_COSTING
    assert handed.version == review.expected_case_version + 1
    assert handed.stop_code is None
    assert handed.stop_detail is None
    assert snapshot["opportunity_id"] == OpportunityId("opp-real")
    assert isinstance(factory.state["events"][-1], SourcingCaseHandedToCosting)
    with pytest.raises(InvalidStateTransition):
        await service.hand_to_costing(
            TENANT,
            case_id,
            OpportunityId("opp-real"),
            expected_need_id=ValidatedNeedId("need-service-1"),
            actor=SYSTEM,
        )


@pytest.mark.asyncio
async def test_review_exact_replay_returns_original_fact_but_changed_command_conflicts() -> (
    None
):
    """同 Case 的重试请求不得重写人工选择事实。"""

    factory = _Factory()
    service = _service(factory)
    case_id = await _opened(service)
    option_id = await _internal_option(service, case_id, ProductId("prd-main"))
    await service.mark_candidates_ready(TENANT, case_id, (option_id,), (), actor=SYSTEM)
    case = factory.state["cases"][(TENANT, case_id)]
    command = SourcingReviewCommand(
        primary_option_id=option_id,
        alternate_option_ids=(),
        reason="证据最完整",
        expected_case_version=case.version,
    )

    first = await service.review(TENANT, case_id, command, actor=SOURCING)
    replayed = await service.review(TENANT, case_id, command, actor=SOURCING)

    assert replayed == first
    assert len(factory.state["reviews"]) == 1
    with pytest.raises(ValidationError, match="已有审核事实"):
        await service.review(
            TENANT,
            case_id,
            command.model_copy(update={"reason": "改选其他理由"}),
            actor=SOURCING,
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
    case_id = await _opened(ready_service)
    option_id = await _internal_option(
        ready_service, case_id, ProductId("prd-rollback")
    )
    ready_factory.bus_fails = True
    with pytest.raises(RuntimeError, match="outbox unavailable"):
        await ready_service.mark_candidates_ready(
            TENANT, case_id, (option_id,), (), actor=SYSTEM
        )
    current = ready_factory.state["cases"][(TENANT, case_id)]
    assert current.state is CaseState.VERIFYING


@pytest.mark.asyncio
async def test_confirmed_plan_can_be_replaced_before_run_but_running_plan_cannot() -> (
    None
):
    factory = _Factory()
    service = _service(factory)
    case_id = await _discovering(service)
    first = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    await service.confirm_public_plan(
        TENANT, first.plan_id, first.plan_hash, actor=BOSS
    )
    replacement_command = _plan(case_id, 2).model_copy(
        update={"expected_case_version": 7}
    )

    replacement = await service.save_public_plan(
        TENANT, case_id, replacement_command, actor=SOURCING
    )

    with pytest.raises(SourcingPlanStaleError):
        await service.get_public_plan_run_view(
            TENANT, case_id, first.plan_id, first.plan_hash, actor=BOSS
        )
    with pytest.raises(SourcingPlanStaleError):
        await service.get_public_plan_run_view(
            TENANT, case_id, replacement.plan_id, replacement.plan_hash, actor=BOSS
        )
    confirmed = await service.confirm_public_plan(
        TENANT, replacement.plan_id, replacement.plan_hash, actor=BOSS
    )
    running = await service.authorize_public_plan_run(
        TENANT, case_id, confirmed.plan_id, confirmed.plan_hash, actor=BOSS
    )
    replay = await service.authorize_public_plan_run(
        TENANT, case_id, confirmed.plan_id, confirmed.plan_hash, actor=BOSS
    )
    assert running.status is PublicPlanStatus.RUNNING
    assert replay == running
    blocked = _plan(case_id, 3).model_copy(update={"expected_case_version": 8})
    with pytest.raises(InvalidStateTransition):
        await service.save_public_plan(TENANT, case_id, blocked, actor=BOSS)


@pytest.mark.asyncio
async def test_confirmation_rejects_wrong_expected_case_before_mutation() -> None:
    factory = _Factory()
    service = _service(factory)
    case_id = await _discovering(service)
    pending = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    case_before = factory.state["cases"][(TENANT, case_id)]

    with pytest.raises(ValidationError, match="Case 不一致"):
        await service.confirm_public_plan(
            TENANT,
            pending.plan_id,
            pending.plan_hash,
            actor=BOSS,
            expected_case_id=SourcingCaseId("src-other"),
        )

    assert (
        factory.state["plans"][(TENANT, pending.plan_id)].status
        is PublicPlanStatus.PENDING_CONFIRMATION
    )
    assert factory.state["cases"][(TENANT, case_id)] == case_before


@pytest.mark.asyncio
async def test_plan_run_and_reconciliation_are_boss_only_before_repository_read() -> (
    None
):
    factory = _Factory()
    service = _service(factory, provider_usage_reader=_ProviderUsageReader())
    calls = [
        lambda: service.get_public_plan_run_view(
            TENANT,
            SourcingCaseId("src-never"),
            SourcingPlanId("spl-never"),
            "a" * 64,
            actor=SOURCING,
        ),
        lambda: service.authorize_public_plan_run(
            TENANT,
            SourcingCaseId("src-never"),
            SourcingPlanId("spl-never"),
            "a" * 64,
            actor=SOURCING,
        ),
        lambda: service.get_uncertain_search_execution(
            TENANT,
            SourcingCaseId("src-never"),
            RunId("run-never"),
            "b" * 64,
            actor=SOURCING,
        ),
    ]
    for call in calls:
        with pytest.raises(PermissionDenied):
            await call()
    assert factory.calls == 0


@pytest.mark.asyncio
async def test_confirmed_consumed_reconciliation_is_evidence_bound_and_exact_replay() -> (
    None
):
    factory = _Factory()
    usage_reader = _ProviderUsageReader()
    service = _service(factory, provider_usage_reader=usage_reader)
    case_id = await _discovering(service)
    pending = await service.save_public_plan(
        TENANT, case_id, _plan(case_id, 1), actor=BOSS
    )
    confirmed = await service.confirm_public_plan(
        TENANT, pending.plan_id, pending.plan_hash, actor=BOSS
    )
    await service.authorize_public_plan_run(
        TENANT, case_id, confirmed.plan_id, confirmed.plan_hash, actor=BOSS
    )
    execution = SourcingSearchExecution(
        execution_id="sse-execution",
        tenant_id=TENANT,
        case_id=case_id,
        plan_id=confirmed.plan_id,
        run_id="run-sourcing",
        plan_hash=confirmed.plan_hash,
        query_index=0,
        request_key="b" * 64,
        query_hash="c" * 64,
        locator_results=(),
        provider_status=SourcingSearchExecutionStatus.UNCERTAIN,
        created_at=NOW,
    )
    factory.state["search_executions"][(TENANT, execution.request_key)] = execution
    command = SourcingUncertainReconciliationCommand(
        reconciliation_id="src-reconciliation",
        run_id=RunId(execution.run_id),
        request_key=execution.request_key,
        resolution="count_as_consumed",
        reason="已在提供商用量页人工核对",
        provider_usage_artifact_ref=usage_reader.projection.artifact_id,
    )

    first = await service.record_confirmed_consumed_reconciliation(
        TENANT, case_id, command, actor=BOSS
    )
    second = await service.record_confirmed_consumed_reconciliation(
        TENANT, case_id, command, actor=BOSS
    )

    assert first == second
    assert first.status is SourcingReconciliationStatus.CONFIRMED_CONSUMED
    assert len(factory.state["reconciliations"]) == 1
    assert usage_reader.calls == 2
    with pytest.raises(SourcingPlanStaleError):
        await service.record_confirmed_consumed_reconciliation(
            TENANT,
            case_id,
            command.model_copy(update={"reason": "不同核对事实"}),
            actor=BOSS,
        )

    second_execution = replace(
        execution,
        execution_id="sse-execution-second",
        request_key="c" * 64,
    )
    factory.state["search_executions"][(TENANT, second_execution.request_key)] = (
        second_execution
    )
    with pytest.raises(SourcingPlanStaleError, match="核对事实冲突") as conflict:
        await service.record_confirmed_consumed_reconciliation(
            TENANT,
            case_id,
            command.model_copy(update={"request_key": second_execution.request_key}),
            actor=BOSS,
        )
    assert conflict.value.__cause__ is None
    assert conflict.value.__context__ is None
    assert len(factory.state["reconciliations"]) == 1


@pytest.mark.asyncio
async def test_reconciliation_rejects_untrusted_or_cross_tenant_usage_evidence() -> (
    None
):
    for reader in (
        _ProviderUsageReader(failure=RuntimeError("secret-provider-payload")),
        _ProviderUsageReader(
            ProviderUsageEvidenceSnapshot(
                tenant_id=OTHER_TENANT,
                artifact_id=ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0Z"),
                provider="tavily",
                content_hash="e" * 64,
                observed_at=NOW,
            )
        ),
    ):
        factory = _Factory()
        service = _service(factory, provider_usage_reader=reader)
        case_id = await _discovering(service)
        pending = await service.save_public_plan(
            TENANT, case_id, _plan(case_id, 1), actor=BOSS
        )
        confirmed = await service.confirm_public_plan(
            TENANT, pending.plan_id, pending.plan_hash, actor=BOSS
        )
        await service.authorize_public_plan_run(
            TENANT, case_id, confirmed.plan_id, confirmed.plan_hash, actor=BOSS
        )
        execution = SourcingSearchExecution(
            execution_id="sse-execution",
            tenant_id=TENANT,
            case_id=case_id,
            plan_id=confirmed.plan_id,
            run_id="run-sourcing",
            plan_hash=confirmed.plan_hash,
            query_index=0,
            request_key="b" * 64,
            query_hash="c" * 64,
            locator_results=(),
            provider_status=SourcingSearchExecutionStatus.UNCERTAIN,
            created_at=NOW,
        )
        factory.state["search_executions"][(TENANT, execution.request_key)] = execution
        command = SourcingUncertainReconciliationCommand(
            reconciliation_id="src-reconciliation",
            run_id=RunId(execution.run_id),
            request_key=execution.request_key,
            resolution="count_as_consumed",
            reason="已在提供商用量页人工核对",
            provider_usage_artifact_ref=ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0Z"),
        )
        with pytest.raises(ValidationError) as failure:
            await service.record_confirmed_consumed_reconciliation(
                TENANT, case_id, command, actor=BOSS
            )
        assert "secret" not in str(failure.value)
        assert factory.state["reconciliations"] == {}
