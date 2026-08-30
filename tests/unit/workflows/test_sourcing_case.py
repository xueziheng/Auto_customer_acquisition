"""Sourcing Case V2 工作流定义、内部梯子与等待门禁。"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

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
from domains.sourcing.schemas import NeedFact, SourcingNeedSnapshot
from domains.sourcing.service import LadderOutcome
from domains.suppliers.service import Supplier, SupplierActor, SupplierRole
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    ProductId,
    RunId,
    SourcingCaseId,
    SourcingSupplyOptionId,
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
            "moq": ProductSpecFact("1000", ArtifactId("art_moq")),
            "product_category": ProductSpecFact(
                "industrial hinges", ArtifactId("art_category")
            ),
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
                ("moq", "5000", "1000"),
                ("product_category", "industrial hinges", "industrial hinges"),
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
            ["cabinet doors", "stainless steel"],
            PRODUCT_ACTOR,
        )
        assert required_specs == (
            ProductSpecRequirement("product_category", "industrial hinges"),
            ProductSpecRequirement("application", "cabinet doors"),
            ProductSpecRequirement("material", "stainless steel"),
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
        assert tags == ["cabinet doors", "industrial hinges", "stainless steel"]
        return self.suppliers


class _Sourcing:
    def __init__(self) -> None:
        self.checks: list[Any] = []
        self.options: dict[ProductId, SourcingSupplyOptionId] = {}
        self.ready_calls: list[
            tuple[tuple[SourcingSupplyOptionId, ...], tuple[object, ...]]
        ] = []
        self.record_error: Exception | None = None
        self.option_error: Exception | None = None

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


def _handlers(products: _Products, suppliers: _Suppliers, sourcing: _Sourcing):
    return build_sourcing_case_handlers(
        need_reader=_NeedReader(),
        products=products,
        suppliers=suppliers,
        sourcing=sourcing,
        product_actor=PRODUCT_ACTOR,
        supplier_actor=SUPPLIER_ACTOR,
        sourcing_actor=SOURCING_ACTOR,
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
        ("moq", "exact"),
        ("product_category", "exact"),
        ("unit", "exact"),
    ]
    assert {
        "art_application",
        "art_material",
        "art_moq",
        "art_product_category",
        "art_unit",
    } <= set(sourcing.checks[-1].evidence_refs)
    assert sourcing.checks[-1].input_snapshot["product_spec_evidence"] == {
        expected_ids[0]: {
            "application": "art_application",
            "material": "art_material",
            "moq": "art_moq",
            "product_category": "art_product_category",
            "unit": "art_unit",
        }
    }
    assert suppliers.calls == 0


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
