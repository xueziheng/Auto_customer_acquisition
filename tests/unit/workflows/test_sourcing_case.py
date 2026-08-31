"""Sourcing Case V2 工作流定义、内部梯子与等待门禁。"""

from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from domains.opportunities.permissions import Actor, OpportunityScope, ScopeLevel
from domains.products.permissions import ProductRole
from domains.products.service import (
    Product,
    ProductActor,
    ProductMatchResult,
    ProductPool,
    ProductSpecComparison,
    ProductSpecFact,
    ProductSpecMatchLevel,
    ProductSpecRequirement,
    QualifiedProductMatch,
)
from domains.sourcing.permissions import SourcingActor, SourcingScope
from domains.sourcing.schemas import (
    NeedFact,
    SourcingNeedSnapshot,
    VerifyPublicCandidateDraftsResult,
)
from domains.sourcing.service import LadderOutcome
from domains.suppliers.service import Supplier, SupplierActor, SupplierRole
from shared.errors import TransientError, ValidationError
from shared.events.catalog import SourcingCandidatesVerified
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    ProductId,
    RunId,
    SourcingCaseId,
    SourcingPlanId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
    SupplierId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.money import Money
from shared.schemas.provenance import ProvenanceSummary, SourceType
from workflows.engine.runner import StepStatus, WorkflowRun
from workflows.sourcing_case.flow import (
    build_sourcing_case_definition,
    build_sourcing_case_handlers,
)

NOW = datetime(2026, 8, 30, 10, tzinfo=UTC)
TENANT = TenantId("tenant-sourcing-flow")
CASE_ID = SourcingCaseId("src-flow")
NEED_ID = ValidatedNeedId("need-flow")
PRODUCT_ACTOR = ProductActor("system:sourcing", ProductRole.SYSTEM, TENANT)
SUPPLIER_ACTOR = SupplierActor("system:sourcing", SupplierRole.SYSTEM, TENANT)
SOURCING_ACTOR = SourcingActor(
    "system:sourcing", TENANT, SourcingScope.SYSTEM, "system"
)
OPPORTUNITY_ACTOR = Actor(
    "system:sourcing", OpportunityScope(level=ScopeLevel.SYSTEM), role="system"
)


def _provenance() -> ProvenanceSummary:
    return ProvenanceSummary(
        source_type=SourceType.CONVERSATION,
        source_id="msg-flow",
        extracted_by="human",
        extracted_at=NOW,
        confirmed_by=EmployeeId("emp-flow"),
        confirmed_at=NOW,
    )


def _need() -> SourcingNeedSnapshot:
    return SourcingNeedSnapshot(
        need_id=NEED_ID,
        completeness=3,
        derivation_version="need-completeness-v1",
        product_category=NeedFact(
            value=" Industrial Hinges ".strip(), provenance=_provenance()
        ),
        application=NeedFact(value="Cabinet doors", provenance=_provenance()),
        material=NeedFact(value="Stainless steel", provenance=_provenance()),
        size_spec=NeedFact(value="4 inch", provenance=_provenance()),
        model=NeedFact(value="HX-4", provenance=_provenance()),
        quantity=NeedFact(value=5000, provenance=_provenance()),
        unit=NeedFact(value="piece", provenance=_provenance()),
        snapshot_hash="a" * 64,
    )


def _run(context: dict[str, Any] | None = None) -> WorkflowRun:
    return WorkflowRun(
        run_id=RunId("run-flow"),
        tenant_id=TENANT,
        workflow_type="sourcing_case",
        workflow_version=2,
        subject_ref=str(CASE_ID),
        current_step="check_ladder",
        status=StepStatus.RUNNING,
        created_at=NOW,
        context=context
        or {
            "case_id": str(CASE_ID),
            "need_id": str(NEED_ID),
            "need_snapshot_hash": "a" * 64,
            "product_category": "industrial hinges",
            "keywords": ["cabinet doors", "stainless steel"],
        },
    )


def _product(
    product_id: str, pool: ProductPool, *, customizable: bool = False
) -> Product:
    return Product(
        product_id=ProductId(product_id),
        tenant_id=TENANT,
        pool=pool,
        name_zh="工业铰链",
        name_en="Industrial hinge",
        category="industrial hinges",
        created_at=NOW,
        internal_cost=Money(Decimal("1.25"), "USD"),
        internal_cost_basis="EXW",
        internal_cost_unit="piece",
        internal_cost_source_ref=ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0X"),
        moq=1000,
        customizable=customizable,
        match_specs={
            "application": ProductSpecFact(
                "cabinet doors", ArtifactId("art_application")
            ),
            "material": ProductSpecFact("stainless steel", ArtifactId("art_material")),
            "model": ProductSpecFact("hx-4", ArtifactId("art_model")),
            "moq": ProductSpecFact("1000", ArtifactId("art_moq")),
            "product_category": ProductSpecFact(
                "industrial hinges", ArtifactId("art_product_category")
            ),
            "size_spec": ProductSpecFact("4 inch", ArtifactId("art_size_spec")),
            "unit": ProductSpecFact("piece", ArtifactId("art_unit")),
        },
    )


def _qualified(product: Product) -> QualifiedProductMatch:
    return QualifiedProductMatch(
        product=product,
        spec_comparisons=tuple(
            ProductSpecComparison(
                spec_name=name,
                required=required,
                offered=offered,
                level=ProductSpecMatchLevel.EXACT,
                evidence_ref=ArtifactId(f"art_{name}"),
            )
            for name, required, offered in (
                ("application", "cabinet doors", "cabinet doors"),
                ("material", "stainless steel", "stainless steel"),
                ("model", "hx-4", "hx-4"),
                ("moq", "5000", "1000"),
                ("product_category", "industrial hinges", "industrial hinges"),
                ("size_spec", "4 inch", "4 inch"),
                ("unit", "piece", "piece"),
            )
        ),
    )


class _NeedReader:
    calls = 0

    async def read(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> SourcingNeedSnapshot:
        self.calls += 1
        assert tenant_id == TENANT and need_id == NEED_ID
        return _need()


class _Products:
    def __init__(self, result: ProductMatchResult) -> None:
        self.result = result
        self.calls = 0

    async def search_for_matching(
        self, tenant_id, category, keywords, required_specs, *, actor
    ):
        self.calls += 1
        assert (tenant_id, category, keywords, actor) == (
            TENANT,
            "industrial hinges",
            ["4 inch", "cabinet doors", "stainless steel"],
            PRODUCT_ACTOR,
        )
        assert required_specs == (
            ProductSpecRequirement("product_category", "industrial hinges"),
            ProductSpecRequirement("application", "cabinet doors"),
            ProductSpecRequirement("material", "stainless steel"),
            ProductSpecRequirement("size_spec", "4 inch"),
            ProductSpecRequirement("model", "hx-4"),
            ProductSpecRequirement("moq", "5000"),
            ProductSpecRequirement("unit", "piece"),
        )
        return self.result


class _Suppliers:
    def __init__(self, suppliers: list[Supplier] | None = None) -> None:
        self.suppliers = suppliers or []
        self.calls = 0

    async def search_by_capability(self, tenant_id, tags, *, actor):
        self.calls += 1
        assert tenant_id == TENANT and actor == SUPPLIER_ACTOR
        assert tags == [
            "4 inch",
            "cabinet doors",
            "industrial hinges",
            "stainless steel",
        ]
        return self.suppliers


class _Opportunities:
    async def get_by_need(self, tenant_id, need_id, *, actor):
        assert (tenant_id, need_id, actor) == (TENANT, NEED_ID, OPPORTUNITY_ACTOR)


class _Sourcing:
    def __init__(self) -> None:
        self.checks: list[Any] = []
        self.options: dict[ProductId, SourcingSupplyOptionId] = {}
        self.ready_calls: list[
            tuple[tuple[SourcingSupplyOptionId, ...], tuple[object, ...]]
        ] = []
        self.record_error: Exception | None = None
        self.option_error: Exception | None = None
        self.verification_error: Exception | None = None
        self.verification_result = VerifyPublicCandidateDraftsResult(
            calibration_draft_ids=("scd-calibration",),
            converted_candidate_ids=(),
            rejected_candidate_ids=(),
            qualified_candidate_ids=(),
            verified_event=None,
        )
        self.verification_calls: list[tuple[Any, ...]] = []

    async def record_ladder_check(self, tenant_id, case_id, check, *, actor):
        assert (tenant_id, case_id, actor) == (TENANT, CASE_ID, SOURCING_ACTOR)
        if self.record_error is not None:
            error, self.record_error = self.record_error, None
            raise error
        existing = next((item for item in self.checks if item.rung == check.rung), None)
        if existing is not None:
            if existing != check:
                raise ValidationError("匹配梯子同级事实冲突")
            return
        self.checks.append(check)

    async def register_existing_product_option(
        self, tenant_id, case_id, product_id, *, actor
    ):
        assert (tenant_id, case_id, actor) == (TENANT, CASE_ID, SOURCING_ACTOR)
        if self.option_error is not None:
            error, self.option_error = self.option_error, None
            raise error
        self.options.setdefault(
            product_id, SourcingSupplyOptionId(f"sop-{product_id!s}")
        )
        return self.options[product_id]

    async def mark_candidates_ready(
        self, tenant_id, case_id, option_ids, candidate_ids, *, actor, **kwargs
    ):
        assert (tenant_id, case_id, actor) == (TENANT, CASE_ID, SOURCING_ACTOR)
        self.ready_calls.append((option_ids, candidate_ids))

    async def verify_public_candidate_drafts(
        self, tenant_id, case_id, command, *, actor
    ):
        self.verification_calls.append((tenant_id, case_id, command, actor))
        if self.verification_error is not None:
            raise self.verification_error
        return self.verification_result


def _handlers(products: _Products, suppliers: _Suppliers, sourcing: _Sourcing):
    return build_sourcing_case_handlers(
        need_reader=_NeedReader(),
        products=products,
        suppliers=suppliers,
        sourcing=sourcing,
        product_actor=PRODUCT_ACTOR,
        supplier_actor=SUPPLIER_ACTOR,
        sourcing_actor=SOURCING_ACTOR,
        opportunities=_Opportunities(),
        opportunity_actor=OPPORTUNITY_ACTOR,
    )


def test_definition_has_exact_v2_steps_transitions_and_wait_contracts() -> None:
    definition = build_sourcing_case_definition()

    assert (definition.workflow_type, definition.version) == ("sourcing_case", 2)
    assert [step.step_name for step in definition.steps] == [
        "check_ladder",
        "await_public_plan",
        "public_search",
        "verify_candidates",
        "prepare_candidates",
        "await_product_cards",
        "await_review",
        "handoff_costing",
    ]
    assert definition.transitions == {
        "check_ladder": ("await_public_plan", "prepare_candidates"),
        "await_public_plan": ("public_search",),
        "public_search": ("verify_candidates",),
        "verify_candidates": ("prepare_candidates",),
        "prepare_candidates": ("await_product_cards",),
        "await_product_cards": ("await_review",),
        "await_review": ("handoff_costing",),
        "handoff_costing": (),
    }
    by_name = {step.step_name: step for step in definition.steps}
    assert (
        by_name["await_public_plan"].wait_event_type,
        by_name["await_public_plan"].run_on_entry,
    ) == (
        "SourcingPlanConfirmed",
        True,
    )
    assert (
        by_name["public_search"].wait_event_type,
        by_name["public_search"].run_on_entry,
        by_name["public_search"].max_retries,
    ) == ("SourcingSearchRetryRequested", True, 0)
    assert (
        by_name["await_product_cards"].wait_event_type,
        by_name["await_product_cards"].run_on_entry,
    ) == (
        "SourcingProductCardsPrepared",
        True,
    )
    assert (
        by_name["await_review"].wait_event_type,
        by_name["await_review"].run_on_entry,
    ) == (
        "SourcingReviewSubmitted",
        True,
    )
    assert (
        by_name["handoff_costing"].wait_event_type,
        by_name["handoff_costing"].run_on_entry,
    ) == (
        "SourcingHandoffRetryRequested",
        True,
    )


def _verification_run() -> WorkflowRun:
    return _run(
        {
            **_run().context,
            "sourcing_plan_id": "spl-flow",
            "sourcing_plan_hash": "b" * 64,
            "supplier_candidate_draft_ids": ["scd-first", "scd-second"],
        }
    )


@pytest.mark.asyncio
async def test_verify_candidates_step_advances_with_exact_sealed_generation() -> None:
    sourcing = _Sourcing()
    candidate_ids = tuple(
        sorted(
            (
                SupplierCandidateId("spc-second"),
                SupplierCandidateId("spc-first"),
            ),
            key=str,
        )
    )
    event = SourcingCandidatesVerified(
        tenant_id=TENANT,
        occurred_at=NOW,
        case_id=CASE_ID,
        candidate_ids=candidate_ids,
        case_version=8,
        candidate_set_hash="c" * 64,
    )
    sourcing.verification_result = VerifyPublicCandidateDraftsResult(
        calibration_draft_ids=(),
        converted_candidate_ids=candidate_ids,
        rejected_candidate_ids=(),
        qualified_candidate_ids=candidate_ids,
        verified_event=event,
    )
    step = _handlers(
        _Products(ProductMatchResult((), ())), _Suppliers(), sourcing
    )["sourcing_case.v2.verify_candidates"]

    result = await step.execute(_verification_run())

    assert result == (
        "advance",
        "prepare_candidates",
        {
            "internal_product_ids": [],
            "supplier_candidate_ids": list(map(str, candidate_ids)),
            "candidate_case_version": 8,
            "candidate_set_hash": "c" * 64,
        },
    )
    command = sourcing.verification_calls[0][2]
    assert command.run_id == RunId("run-flow")
    assert command.plan_id == SourcingPlanId("spl-flow")
    assert command.draft_ids == ("scd-first", "scd-second")


@pytest.mark.asyncio
async def test_supplier_generation_runs_verify_prepare_and_wait_without_domain_writes() -> None:
    """供应商路径的 prepare 若误走内部产品分支，真实步骤序列会在建卡前失败。"""

    sourcing = _Sourcing()
    candidate_ids = (
        SupplierCandidateId("spc-first"),
        SupplierCandidateId("spc-second"),
    )
    sourcing.verification_result = VerifyPublicCandidateDraftsResult(
        calibration_draft_ids=(),
        converted_candidate_ids=candidate_ids,
        rejected_candidate_ids=(),
        qualified_candidate_ids=candidate_ids,
        verified_event=SourcingCandidatesVerified(
            tenant_id=TENANT,
            occurred_at=NOW,
            case_id=CASE_ID,
            candidate_ids=candidate_ids,
            case_version=8,
            candidate_set_hash="c" * 64,
        ),
    )
    handlers = _handlers(
        _Products(ProductMatchResult((), ())), _Suppliers(), sourcing
    )

    verify_result = await handlers["sourcing_case.v2.verify_candidates"].execute(
        _verification_run()
    )
    assert verify_result[0:2] == ("advance", "prepare_candidates")
    prepare_run = replace(
        _verification_run(),
        current_step="prepare_candidates",
        context={**_verification_run().context, **verify_result[2]},
    )
    prepare_result = await handlers["sourcing_case.v2.prepare_candidates"].execute(
        prepare_run
    )

    assert prepare_result == (
        "advance",
        "await_product_cards",
        {
            "option_ids": [],
            "supplier_candidate_ids": ["spc-first", "spc-second"],
            "candidate_case_version": 8,
            "candidate_set_hash": "c" * 64,
        },
    )
    waiting_run = replace(
        prepare_run,
        current_step="await_product_cards",
        context={**prepare_run.context, **prepare_result[2]},
    )
    assert await handlers["sourcing_case.v2.await_product_cards"].execute(
        waiting_run
    ) == ("wait", None, {})
    assert sourcing.options == {}
    assert sourcing.ready_calls == []


@pytest.mark.asyncio
async def test_verify_candidates_step_waits_safely_when_no_candidate_qualifies() -> None:
    sourcing = _Sourcing()
    sourcing.verification_result = VerifyPublicCandidateDraftsResult(
        calibration_draft_ids=("scd-calibration",),
        converted_candidate_ids=(SupplierCandidateId("spc-rejected"),),
        rejected_candidate_ids=(SupplierCandidateId("spc-rejected"),),
        qualified_candidate_ids=(),
        verified_event=None,
    )
    step = _handlers(
        _Products(ProductMatchResult((), ())), _Suppliers(), sourcing
    )["sourcing_case.v2.verify_candidates"]

    result = await step.execute(_verification_run())

    assert result == (
        "wait",
        None,
        {
            "sourcing_stop_reason": "no_qualified_candidate",
            "calibration_draft_ids": ["scd-calibration"],
            "converted_supplier_candidate_ids": ["spc-rejected"],
            "rejected_supplier_candidate_ids": ["spc-rejected"],
            "qualified_supplier_candidate_count": 0,
        },
    )


@pytest.mark.asyncio
async def test_await_product_cards_advances_only_on_exact_generation_payload() -> None:
    """产品卡唤醒如与封存候选或 generation 不同，必须继续等待。"""

    candidate_ids = ["spc-first", "spc-second"]
    waiting = _run(
        {
            **_run().context,
            "internal_product_ids": [],
            "supplier_candidate_ids": candidate_ids,
            "candidate_case_version": 8,
            "candidate_set_hash": "c" * 64,
        }
    )
    waiting.current_step = "await_product_cards"
    step = _handlers(
        _Products(ProductMatchResult((), ())), _Suppliers(), _Sourcing()
    )["sourcing_case.v2.await_product_cards"]

    assert await step.execute(waiting) == ("wait", None, {})

    waiting.context["event"] = {
        "event_type": "SourcingProductCardsPrepared",
        "payload": {
            "case_id": str(CASE_ID),
            "candidate_ids": candidate_ids,
            "product_ids": ["prd-first", "prd-second"],
            "option_ids": ["sop-first", "sop-second"],
            "case_version": 8,
            "candidate_set_hash": "c" * 64,
        },
    }
    assert await step.execute(waiting) == (
        "advance",
        "await_review",
        {
            "product_ids": ["prd-first", "prd-second"],
            "option_ids": ["sop-first", "sop-second"],
        },
    )

    waiting.context["event"]["payload"]["case_version"] = 9
    with pytest.raises(ValidationError, match="generation"):
        await step.execute(waiting)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw_error", "error_type", "expected"),
    [
        (
            TransientError(
                "postgres://user:secret@db/private",
                context={"token": "raw-secret"},
            ),
            TransientError,
            "公开候选草稿核验暂不可用",
        ),
        (
            RuntimeError("provider token=raw-secret"),
            ValidationError,
            "公开候选草稿核验失败",
        ),
    ],
)
async def test_verify_candidates_dependency_errors_are_fixed_and_fully_detached(
    raw_error: Exception,
    error_type: type[Exception],
    expected: str,
) -> None:
    sourcing = _Sourcing()
    sourcing.verification_error = raw_error
    step = _handlers(
        _Products(ProductMatchResult((), ())), _Suppliers(), sourcing
    )["sourcing_case.v2.verify_candidates"]

    with pytest.raises(error_type, match=f"^{expected}$") as caught:
        await step.execute(_verification_run())

    assert getattr(caught.value, "context", {}) == {}
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("matches", "expected_rung", "expected_ids"),
    [
        ((_product("prd-exact", ProductPool.FORMAL),), 1, ["prd-exact"]),
        (
            (_product("prd-custom", ProductPool.FORMAL, customizable=True),),
            2,
            ["prd-custom"],
        ),
        ((_product("prd-candidate", ProductPool.CANDIDATE),), 3, ["prd-candidate"]),
    ],
)
async def test_first_qualified_product_rung_short_circuits_supplier_search(
    matches: tuple[Product, ...], expected_rung: int, expected_ids: list[str]
) -> None:
    products = _Products(
        ProductMatchResult(tuple(_qualified(item) for item in matches), ())
    )
    suppliers = _Suppliers()
    sourcing = _Sourcing()
    step = _handlers(products, suppliers, sourcing)["sourcing_case.v2.check_ladder"]

    result = await step.execute(_run())

    assert result == (
        "advance",
        "prepare_candidates",
        {"internal_product_ids": expected_ids, "supplier_candidate_ids": []},
    )
    assert [item.rung.value for item in sourcing.checks] == list(
        range(1, expected_rung + 1)
    )
    assert sourcing.checks[-1].outcome is LadderOutcome.QUALIFIED_SUPPLY_FOUND
    assert [
        (item.spec_name, item.level.value)
        for item in sourcing.checks[-1].spec_comparisons
    ] == [
        ("application", "exact"),
        ("material", "exact"),
        ("model", "exact"),
        ("moq", "exact"),
        ("product_category", "exact"),
        ("size_spec", "exact"),
        ("unit", "exact"),
    ]
    assert {
        "art_application",
        "art_material",
        "art_model",
        "art_moq",
        "art_product_category",
        "art_size_spec",
        "art_unit",
    } <= set(sourcing.checks[-1].evidence_refs)
    assert sourcing.checks[-1].input_snapshot["product_spec_evidence"] == {
        expected_ids[0]: {
            "application": "art_application",
            "material": "art_material",
            "model": "art_model",
            "moq": "art_moq",
            "product_category": "art_product_category",
            "size_spec": "art_size_spec",
            "unit": "art_unit",
        }
    }
    assert all(
        item.product_id == ProductId(expected_ids[0])
        and item.evidence_ref
        == ArtifactId(
            sourcing.checks[-1].input_snapshot["product_spec_evidence"][
                expected_ids[0]
            ][item.spec_name]
        )
        for item in sourcing.checks[-1].spec_comparisons
    )
    assert suppliers.calls == 0


@pytest.mark.asyncio
async def test_every_qualified_product_gets_a_complete_spec_evidence_mapping() -> None:
    matches = (
        _product("prd-a", ProductPool.FORMAL),
        _product("prd-b", ProductPool.FORMAL),
    )
    sourcing = _Sourcing()
    step = _handlers(
        _Products(ProductMatchResult(tuple(_qualified(item) for item in matches), ())),
        _Suppliers(),
        sourcing,
    )["sourcing_case.v2.check_ladder"]

    await step.execute(_run())

    check = sourcing.checks[-1]
    mapping = check.input_snapshot["product_spec_evidence"]
    assert isinstance(mapping, dict)
    assert set(mapping) == {"prd-a", "prd-b"}
    assert all(
        set(product_mapping)
        == {
            "application",
            "material",
            "model",
            "moq",
            "product_category",
            "size_spec",
            "unit",
        }
        for product_mapping in mapping.values()
    )
    assert len(check.spec_comparisons) == 14
    assert {
        str(item.product_id) for item in check.spec_comparisons
    } == {"prd-a", "prd-b"}


@pytest.mark.asyncio
@pytest.mark.parametrize("model_failure", ["missing", "unknown", "different"])
async def test_model_failure_cannot_qualify_an_early_ladder_rung(
    model_failure: str,
) -> None:
    product = _product("prd-model-failure", ProductPool.FORMAL)
    comparisons = list(_qualified(product).spec_comparisons)
    model_index = next(
        index
        for index, comparison in enumerate(comparisons)
        if comparison.spec_name == "model"
    )
    if model_failure == "missing":
        comparisons.pop(model_index)
    else:
        comparisons[model_index] = ProductSpecComparison(
            spec_name="model",
            required="hx-4",
            offered=None if model_failure == "unknown" else "hx-5",
            level=(
                ProductSpecMatchLevel.UNKNOWN
                if model_failure == "unknown"
                else ProductSpecMatchLevel.DIFFERENT
            ),
            evidence_ref=None if model_failure == "unknown" else ArtifactId("art_model"),
        )
    sourcing = _Sourcing()
    step = _handlers(
        _Products(
            ProductMatchResult(
                (QualifiedProductMatch(product, tuple(comparisons)),), ()
            )
        ),
        _Suppliers(),
        sourcing,
    )["sourcing_case.v2.check_ladder"]

    with pytest.raises(ValidationError, match="规格证明"):
        await step.execute(_run())

    assert sourcing.checks == []


@pytest.mark.asyncio
async def test_qualified_match_cannot_rewrite_trusted_need_requirement() -> None:
    """产品服务伪造 required=carbon 的 exact 结果不能覆盖 Need 的 stainless。"""

    product = _product("prd-drift", ProductPool.FORMAL)
    comparisons = list(_qualified(product).spec_comparisons)
    comparisons[1] = ProductSpecComparison(
        spec_name="material",
        required="carbon steel",
        offered="carbon steel",
        level=ProductSpecMatchLevel.EXACT,
        evidence_ref=ArtifactId("art_material"),
    )
    products = _Products(
        ProductMatchResult(
            (QualifiedProductMatch(product, tuple(comparisons)),), ()
        )
    )
    sourcing = _Sourcing()
    step = _handlers(products, _Suppliers(), sourcing)[
        "sourcing_case.v2.check_ladder"
    ]

    with pytest.raises(ValidationError, match="规格证明"):
        await step.execute(_run())

    assert sourcing.checks == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tamper", ["offered", "evidence_ref"])
async def test_qualified_match_must_equal_persisted_product_fact(
    tamper: str,
) -> None:
    """Adapter 不能替换 Product 持久事实的 offered 或 Evidence。"""

    product = _product("prd-product-fact", ProductPool.FORMAL)
    comparisons = list(_qualified(product).spec_comparisons)
    comparisons[1] = replace(
        comparisons[1],
        **(
            {"offered": "carbon steel"}
            if tamper == "offered"
            else {"evidence_ref": ArtifactId("art_forged")}
        ),
    )
    products = _Products(
        ProductMatchResult(
            (QualifiedProductMatch(product, tuple(comparisons)),), ()
        )
    )
    sourcing = _Sourcing()
    step = _handlers(products, _Suppliers(), sourcing)[
        "sourcing_case.v2.check_ladder"
    ]

    with pytest.raises(ValidationError, match="规格证明"):
        await step.execute(_run())

    assert sourcing.checks == []


@pytest.mark.asyncio
async def test_qualified_match_rejects_duplicate_returned_comparison() -> None:
    """重复 comparison 不能用第二份 Evidence 覆盖第一份。"""

    product = _product("prd-duplicate", ProductPool.FORMAL)
    comparisons = _qualified(product).spec_comparisons
    products = _Products(
        ProductMatchResult(
            (
                QualifiedProductMatch(
                    product,
                    tuple(
                        sorted(
                            (*comparisons, comparisons[1]),
                            key=lambda item: item.spec_name,
                        )
                    ),
                ),
            ),
            (),
        )
    )
    sourcing = _Sourcing()
    step = _handlers(products, _Suppliers(), sourcing)[
        "sourcing_case.v2.check_ladder"
    ]

    with pytest.raises(ValidationError, match="规格证明"):
        await step.execute(_run())

    assert sourcing.checks == []


@pytest.mark.asyncio
async def test_trusted_need_requirement_map_rejects_normalized_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Need 规格名规范化后重复时，调用产品服务前即失败关闭。"""

    monkeypatch.setattr(
        "workflows.sourcing_case.steps._required_specs",
        lambda _snapshot: (
            ProductSpecRequirement(" Material ", "stainless steel"),
            ProductSpecRequirement("material", "stainless steel"),
        ),
    )
    products = _Products(ProductMatchResult((), ()))
    step = _handlers(products, _Suppliers(), _Sourcing())[
        "sourcing_case.v2.check_ladder"
    ]

    with pytest.raises(ValidationError, match="可信寻源需求规格"):
        await step.execute(_run())

    assert products.calls == 0


@pytest.mark.asyncio
async def test_supplier_capability_is_only_a_lead_after_all_five_unqualified_checks() -> (
    None
):
    products = _Products(ProductMatchResult((), ()))
    supplier = Supplier(
        supplier_id=SupplierId("sup-lead"),
        tenant_id=TENANT,
        name="Factory Lead",
        created_at=NOW,
        capability_tags=["industrial hinges", "custom"],
    )
    suppliers = _Suppliers([supplier])
    sourcing = _Sourcing()
    step = _handlers(products, suppliers, sourcing)["sourcing_case.v2.check_ladder"]

    result = await step.execute(_run())

    assert result == (
        "advance",
        "await_public_plan",
        {"internal_product_ids": [], "supplier_candidate_ids": []},
    )
    assert products.calls == suppliers.calls == 1
    assert [item.rung.value for item in sourcing.checks] == [1, 2, 3, 4, 5]
    assert all(
        item.outcome is LadderOutcome.NO_QUALIFIED_SUPPLY for item in sourcing.checks
    )
    assert sourcing.checks[3].match_object_id == "sup-lead"
    assert sourcing.checks[4].match_object_id == "sup-lead"


@pytest.mark.asyncio
async def test_partial_ladder_retry_replays_same_fact_and_continues_without_drift() -> (
    None
):
    products = _Products(
        ProductMatchResult(
            (
                _qualified(
                    _product("prd-custom", ProductPool.FORMAL, customizable=True)
                ),
            ),
            (),
        )
    )
    sourcing = _Sourcing()
    suppliers = _Suppliers()
    step = _handlers(products, suppliers, sourcing)["sourcing_case.v2.check_ladder"]
    original_record = sourcing.record_ladder_check
    failed = False

    async def fail_after_first_commit(*args, **kwargs):
        nonlocal failed
        await original_record(*args, **kwargs)
        if not failed:
            failed = True
            raise RuntimeError("database packet included raw-provider-token")

    sourcing.record_ladder_check = fail_after_first_commit  # type: ignore[method-assign]
    with pytest.raises(ValidationError, match="寻源内部匹配记录失败") as caught:
        await step.execute(_run())
    assert "raw-provider-token" not in str(caught.value)

    sourcing.record_ladder_check = original_record  # type: ignore[method-assign]
    result = await step.execute(_run())

    assert result[0:2] == ("advance", "prepare_candidates")
    assert [item.rung.value for item in sourcing.checks] == [1, 2]


@pytest.mark.asyncio
async def test_prepare_internal_options_is_idempotent_and_skips_product_card_wait() -> (
    None
):
    sourcing = _Sourcing()
    handlers = _handlers(_Products(ProductMatchResult((), ())), _Suppliers(), sourcing)
    run = _run(
        {
            **_run().context,
            "internal_product_ids": ["prd-a", "prd-b"],
            "supplier_candidate_ids": [],
        }
    )

    first = await handlers["sourcing_case.v2.prepare_candidates"].execute(run)
    second = await handlers["sourcing_case.v2.prepare_candidates"].execute(run)

    assert (
        first
        == second
        == (
            "advance",
            "await_product_cards",
            {"option_ids": ["sop-prd-a", "sop-prd-b"], "supplier_candidate_ids": []},
        )
    )
    assert len(sourcing.options) == 2
    assert sourcing.ready_calls == [
        (
            (SourcingSupplyOptionId("sop-prd-a"), SourcingSupplyOptionId("sop-prd-b")),
            (),
        ),
        (
            (SourcingSupplyOptionId("sop-prd-a"), SourcingSupplyOptionId("sop-prd-b")),
            (),
        ),
    ]
    wait_result = await handlers["sourcing_case.v2.await_product_cards"].execute(
        _run({**run.context, **first[2]})
    )
    assert wait_result == ("advance", "await_review", {})


def test_handler_map_covers_every_definition_handler_ref() -> None:
    handlers = _handlers(
        _Products(ProductMatchResult((), ())), _Suppliers(), _Sourcing()
    )
    definition = build_sourcing_case_definition()

    assert set(handlers) == {step.handler_ref for step in definition.steps}


def test_handler_builder_uses_public_product_actor_type() -> None:
    from domains.products.service import ProductActor as PublicProductActor

    annotation = (
        inspect.signature(build_sourcing_case_handlers)
        .parameters["product_actor"]
        .annotation
    )
    assert annotation == "ProductActor" or annotation is PublicProductActor


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["ladder_record", "option_registration"])
async def test_committed_partial_work_stays_retryable_without_leaking_dependency_error(
    failure_stage: str,
) -> None:
    products = _Products(
        ProductMatchResult((_qualified(_product("prd-exact", ProductPool.FORMAL)),), ())
    )
    sourcing = _Sourcing()
    raw_error = TransientError(
        "postgres://user:secret@db/private",
        context={"token": "raw-secret"},
    )
    handlers = _handlers(products, _Suppliers(), sourcing)
    if failure_stage == "ladder_record":
        sourcing.record_error = raw_error
        operation = handlers["sourcing_case.v2.check_ladder"].execute(_run())
        expected = "寻源内部匹配记录暂不可用"
    else:
        sourcing.option_error = raw_error
        operation = handlers["sourcing_case.v2.prepare_candidates"].execute(
            _run(
                {
                    **_run().context,
                    "internal_product_ids": ["prd-exact"],
                    "supplier_candidate_ids": [],
                }
            )
        )
        expected = "寻源内部产品准备暂不可用"

    with pytest.raises(TransientError, match=f"^{expected}$") as caught:
        await operation

    assert caught.value.context == {}
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "raw-secret" not in str(caught.value)
