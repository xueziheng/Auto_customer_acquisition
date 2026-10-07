"""Sourcing verified generation 到产品卡与工作流唤醒的投影。"""

from __future__ import annotations

import importlib
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError as PydanticValidationError

from domains.products.permissions import ProductActor, ProductRole
from domains.products.schemas import CandidateProductCreate
from domains.sourcing.permissions import SourcingActor, SourcingScope
from domains.sourcing.schemas import (
    CaseView,
    SourcingCandidateProductInput,
    SourcingCandidateProductInputs,
    SourcingCandidateProductPriceInput,
    SourcingReviewCommand,
)
from domains.sourcing.service import SourcingReview
from shared.errors import TransientError, ValidationError
from shared.events.catalog import (
    SourcingCandidatesReady,
    SourcingCandidatesVerified,
)
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    ProductId,
    RunId,
    SourcingCaseId,
    SourcingReviewId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
    TenantId,
)
from workflows.engine.runner import StepStatus, WorkflowRun
from workflows.sourcing_case.application import (
    SourcingCaseApplication,
    SourcingPlanDeliveryError,
)

TENANT = TenantId("tenant-projection")
CASE_ID = SourcingCaseId("src-projection")
CANDIDATE_ID = SupplierCandidateId("spc-projection")
NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)
VERSION = 7
GENERATION_HASH = "a" * 64
SOURCING_ACTOR = SourcingActor(
    "system:product-projector", TENANT, SourcingScope.SYSTEM, "system"
)
PRODUCT_ACTOR = ProductActor("system:product-projector", ProductRole.SYSTEM, TENANT)


def _symbol(module_name: str, symbol_name: str):
    module = importlib.import_module(module_name)
    if not hasattr(module, symbol_name):
        pytest.fail(f"{module_name}.{symbol_name} 尚未实现")
    return getattr(module, symbol_name)


def test_sourcing_candidate_product_projector_contract_exists() -> None:
    """缺少投影器时，封存的候选 generation 无法生成产品卡。"""

    try:
        module = importlib.import_module("apps.scheduler_worker.sourcing_projections")
    except ModuleNotFoundError:
        pytest.fail("SourcingCandidateProductProjector 尚未实现")
    assert hasattr(module, "SourcingCandidateProductProjector")


def test_product_subscription_uses_verified_fact_not_final_ready_fact() -> None:
    """Ready 是最终完整集事实，如被订阅会形成建卡循环。"""

    products_events = importlib.import_module("domains.products.events")
    sourcing_events = importlib.import_module("domains.sourcing.events")

    assert products_events.SUBSCRIBES == (SourcingCandidatesVerified,)
    assert SourcingCandidatesVerified in sourcing_events.PUBLISHES
    assert SourcingCandidatesReady in sourcing_events.PUBLISHES


def test_candidate_product_input_projection_is_strict_and_generation_bound() -> None:
    """对任一命令的候选或 Case 绑定放宽都必须被严格投影拦截。"""

    Command = _symbol("domains.sourcing.schemas", "SourcingCandidateProductInput")
    Price = _symbol("domains.sourcing.schemas", "SourcingCandidateProductPriceInput")
    Projection = _symbol("domains.sourcing.schemas", "SourcingCandidateProductInputs")
    command = Command(
        sourcing_case_id=CASE_ID,
        supplier_candidate_id=CANDIDATE_ID,
        name_zh="Industrial hinge",
        name_en="Industrial hinge",
        category="industrial hinges",
        spec_summary="Material and size match the verified need",
        moq=100,
        evidence_refs=(ArtifactId("art-product"),),
        indicative_prices=(
            Price(
                minimum_quantity=100,
                unit_amount=Decimal("1.25"),
                currency="USD",
                unit="piece",
                evidence_ref=ArtifactId("art-product"),
            ),
        ),
    )
    projection = Projection(
        tenant_id=TENANT,
        case_id=CASE_ID,
        candidate_ids=(CANDIDATE_ID,),
        case_version=7,
        candidate_set_hash="a" * 64,
        commands=(command,),
    )

    assert projection.commands == (command,)
    with pytest.raises(PydanticValidationError, match="NUMERIC"):
        Price(
            minimum_quantity=100,
            unit_amount=Decimal("10000000000000000.0000000000001"),
            currency="USD",
            unit="piece",
            evidence_ref=ArtifactId("art-product"),
        )
    with pytest.raises(PydanticValidationError):
        Projection(
            tenant_id=TENANT,
            case_id=SourcingCaseId("src-other"),
            candidate_ids=(CANDIDATE_ID,),
            case_version=7,
            candidate_set_hash="a" * 64,
            commands=(command,),
        )


def _projection(candidate_count: int = 3) -> SourcingCandidateProductInputs:
    candidate_ids = tuple(
        SupplierCandidateId(f"spc-candidate-{index}")
        for index in range(1, candidate_count + 1)
    )
    evidence = ArtifactId("art-product")
    commands = tuple(
        SourcingCandidateProductInput(
            sourcing_case_id=CASE_ID,
            supplier_candidate_id=candidate_id,
            name_zh=f"Product {index}",
            name_en=f"Product {index}",
            category="industrial hinges",
            spec_summary="Material and size match the verified need",
            moq=100,
            evidence_refs=(evidence,),
            indicative_prices=(
                SourcingCandidateProductPriceInput(
                    minimum_quantity=100,
                    unit_amount=Decimal("1.25"),
                    currency="USD",
                    unit="piece",
                    evidence_ref=evidence,
                ),
            ),
        )
        for index, candidate_id in enumerate(candidate_ids, start=1)
    )
    return SourcingCandidateProductInputs(
        tenant_id=TENANT,
        case_id=CASE_ID,
        candidate_ids=candidate_ids,
        case_version=VERSION,
        candidate_set_hash=GENERATION_HASH,
        commands=commands,
    )


def _event(candidate_count: int = 3) -> SourcingCandidatesVerified:
    projection = _projection(candidate_count)
    return SourcingCandidatesVerified(
        tenant_id=TENANT,
        occurred_at=NOW,
        case_id=CASE_ID,
        candidate_ids=projection.candidate_ids,
        case_version=VERSION,
        candidate_set_hash=GENERATION_HASH,
    )


class _Sourcing:
    def __init__(self, projection: SourcingCandidateProductInputs) -> None:
        self.projection = projection
        self.read_calls: list[tuple[Any, ...]] = []
        self.option_calls: list[tuple[Any, ...]] = []
        self.ready_calls: list[tuple[Any, ...]] = []

    async def get_candidate_product_inputs(
        self,
        tenant_id,
        case_id,
        candidate_ids,
        *,
        expected_case_version,
        expected_candidate_set_hash,
        actor,
    ):
        self.read_calls.append(
            (
                tenant_id,
                case_id,
                candidate_ids,
                expected_case_version,
                expected_candidate_set_hash,
                actor,
            )
        )
        return self.projection

    async def register_supplier_candidate_option(
        self,
        tenant_id,
        case_id,
        candidate_id,
        product_id,
        *,
        expected_case_version,
        expected_candidate_set_hash,
        actor,
    ):
        self.option_calls.append(
            (
                tenant_id,
                case_id,
                candidate_id,
                product_id,
                expected_case_version,
                expected_candidate_set_hash,
                actor,
            )
        )
        return SourcingSupplyOptionId(f"sop-{str(candidate_id).removeprefix('spc-')}")

    async def mark_candidates_ready(
        self,
        tenant_id,
        case_id,
        option_ids,
        candidate_ids,
        *,
        expected_case_version,
        expected_candidate_set_hash,
        actor,
    ):
        self.ready_calls.append(
            (
                tenant_id,
                case_id,
                option_ids,
                candidate_ids,
                expected_case_version,
                expected_candidate_set_hash,
                actor,
            )
        )


class _Products:
    def __init__(
        self,
        *,
        fail_once_for: SupplierCandidateId | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.fail_once_for = fail_once_for
        self.failure = failure
        self.failed = False
        self.created_source_keys: set[tuple[str, str]] = set()
        self.calls: list[CandidateProductCreate] = []

    async def create_candidate_from_sourcing(
        self, tenant_id, command, *, actor
    ) -> ProductId:
        assert tenant_id == TENANT and actor == PRODUCT_ACTOR
        assert isinstance(command, CandidateProductCreate)
        self.calls.append(command)
        if command.supplier_candidate_id == self.fail_once_for and not self.failed:
            self.failed = True
            raise self.failure or TransientError("postgres://user:secret@db/private")
        self.created_source_keys.add(
            (str(command.sourcing_case_id), str(command.supplier_candidate_id))
        )
        return ProductId(
            f"prd-{str(command.supplier_candidate_id).removeprefix('spc-')}"
        )


class _Engine:
    def __init__(self) -> None:
        self.run = WorkflowRun(
            run_id=RunId("run-product-projection"),
            tenant_id=TENANT,
            workflow_type="sourcing_case",
            workflow_version=2,
            subject_ref=str(CASE_ID),
            current_step="await_product_cards",
            status=StepStatus.RUNNING,
            created_at=NOW,
            context={
                "case_id": str(CASE_ID),
                "supplier_candidate_ids": [
                    "spc-candidate-1",
                    "spc-candidate-2",
                    "spc-candidate-3",
                ],
                "candidate_case_version": VERSION,
                "candidate_set_hash": GENERATION_HASH,
            },
        )
        self.product_cards_prepared_events = 0
        self.payloads: list[dict[str, Any]] = []
        self.prepared_run_ids: set[RunId] = set()
        self.historical_prepared = False
        self.query_error_once: Exception | None = None
        self.queries: list[tuple[Any, ...]] = []

    async def find_active_run(self, tenant_id, workflow_type, subject_ref):
        if (
            tenant_id == TENANT
            and workflow_type == "sourcing_case"
            and subject_ref == str(CASE_ID)
            and self.run.status in {StepStatus.RUNNING, StepStatus.WAITING_EVENT}
        ):
            return self.run.run_id
        return None

    async def get_run(self, tenant_id, run_id):
        if tenant_id == TENANT and run_id == self.run.run_id:
            return self.run
        return None

    async def has_delivered_event(
        self,
        tenant_id,
        workflow_type,
        subject_ref,
        event_type,
        payload,
        *,
        workflow_version=None,
        required_context=None,
        run_id=None,
    ):
        if self.query_error_once is not None:
            error, self.query_error_once = self.query_error_once, None
            raise error
        self.queries.append(
            (
                tenant_id,
                workflow_type,
                subject_ref,
                event_type,
                payload,
                workflow_version,
                required_context,
                run_id,
            )
        )
        if event_type != "SourcingProductCardsPrepared":
            return False
        if run_id is None and self.historical_prepared:
            return True
        return run_id in self.prepared_run_ids

    async def deliver_event(self, tenant_id, run_id, event_type, payload):
        assert tenant_id == TENANT and run_id == self.run.run_id
        assert event_type == "SourcingProductCardsPrepared"
        self.product_cards_prepared_events += 1
        self.payloads.append(dict(payload))
        self.prepared_run_ids.add(run_id)
        self.run = replace(self.run, current_step="await_review")
        return True


def _projector(
    *, products: _Products | None = None
) -> tuple[Any, _Sourcing, _Products, _Engine]:
    Projector = _symbol(
        "apps.scheduler_worker.sourcing_projections",
        "SourcingCandidateProductProjector",
    )
    sourcing = _Sourcing(_projection())
    products = products or _Products()
    engine = _Engine()
    return (
        Projector(
            sourcing=sourcing,
            products=products,
            engine=engine,
            tenant_id=TENANT,
            sourcing_actor=SOURCING_ACTOR,
            product_actor=PRODUCT_ACTOR,
        ),
        sourcing,
        products,
        engine,
    )


@pytest.mark.asyncio
async def test_candidates_verified_projection_is_idempotent_and_generation_exact() -> (
    None
):
    """重投或 generation 参数丢失不得重建卡、重发唤醒或混用候选。"""

    projector, sourcing, products, engine = _projector()
    event = _event()

    await projector.handle(event)
    await projector.handle(event)

    assert products.created_source_keys == {
        (str(CASE_ID), "spc-candidate-1"),
        (str(CASE_ID), "spc-candidate-2"),
        (str(CASE_ID), "spc-candidate-3"),
    }
    assert engine.product_cards_prepared_events == 1
    assert all(
        call[4:6] == (VERSION, GENERATION_HASH) for call in sourcing.option_calls
    )
    assert all(call[4:6] == (VERSION, GENERATION_HASH) for call in sourcing.ready_calls)
    assert engine.payloads == [
        {
            "case_id": str(CASE_ID),
            "candidate_ids": [
                "spc-candidate-1",
                "spc-candidate-2",
                "spc-candidate-3",
            ],
            "product_ids": [
                "prd-candidate-1",
                "prd-candidate-2",
                "prd-candidate-3",
            ],
            "option_ids": [
                "sop-candidate-1",
                "sop-candidate-2",
                "sop-candidate-3",
            ],
            "case_version": VERSION,
            "candidate_set_hash": GENERATION_HASH,
        }
    ]
    assert all(
        query[0:4]
        == (
            TENANT,
            "sourcing_case",
            str(CASE_ID),
            "SourcingProductCardsPrepared",
        )
        and query[5] == 2
        and query[6]
        == {
            "case_id": str(CASE_ID),
            "supplier_candidate_ids": [
                "spc-candidate-1",
                "spc-candidate-2",
                "spc-candidate-3",
            ],
            "candidate_case_version": VERSION,
            "candidate_set_hash": GENERATION_HASH,
        }
        and query[7] == RunId("run-product-projection")
        for query in engine.queries
    )


@pytest.mark.asyncio
async def test_projection_recovers_after_partial_product_failure_without_raw_error() -> (
    None
):
    """已成功的 source key 必须可复用，且下层敏感错误不得跨编排边界。"""

    products = _Products(fail_once_for=SupplierCandidateId("spc-candidate-2"))
    projector, sourcing, products, engine = _projector(products=products)

    with pytest.raises(TransientError, match="候选产品卡创建暂不可用") as caught:
        await projector.handle(_event())

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert engine.product_cards_prepared_events == 0
    assert sourcing.ready_calls == []

    await projector.handle(_event())

    assert products.created_source_keys == {
        (str(CASE_ID), "spc-candidate-1"),
        (str(CASE_ID), "spc-candidate-2"),
        (str(CASE_ID), "spc-candidate-3"),
    }
    assert engine.product_cards_prepared_events == 1


@pytest.mark.asyncio
async def test_unknown_product_storage_failure_is_detached_transient() -> None:
    """Unknown adapter failures must redeliver instead of dead-lettering Verified."""

    products = _Products(
        fail_once_for=SupplierCandidateId("spc-candidate-2"),
        failure=RuntimeError("postgres://user:secret@db/private"),
    )
    projector, sourcing, products, engine = _projector(products=products)

    with pytest.raises(TransientError, match="^候选产品卡创建暂不可用$") as caught:
        await projector.handle(_event())

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "secret" not in str(caught.value)
    assert sourcing.ready_calls == []
    assert engine.product_cards_prepared_events == 0

    await projector.handle(_event())
    assert len(products.created_source_keys) == 3
    assert engine.product_cards_prepared_events == 1


@pytest.mark.asyncio
async def test_ready_then_engine_failure_is_detached_transient_and_retry_delivers_once() -> (
    None
):
    """Ready 已提交后 Engine 原始失败若被永久化，会不可恢复地丢失工作流唤醒。"""

    projector, sourcing, _products, engine = _projector()
    engine.query_error_once = RuntimeError(
        "postgres://user:secret@db/private token=raw-secret"
    )

    with pytest.raises(TransientError, match="工作流事件证据暂不可用") as caught:
        await projector.handle(_event())

    assert len(sourcing.ready_calls) == 1
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "secret" not in str(caught.value)

    await projector.handle(_event())

    assert len(sourcing.ready_calls) == 2
    assert engine.product_cards_prepared_events == 1


@pytest.mark.asyncio
async def test_prepare_ordering_race_is_transient_and_exact_retry_wakes_same_run() -> (
    None
):
    """投影先于 prepare bridge 提交是合法排序竞争，不能永久死信。"""

    projector, sourcing, _products, engine = _projector()
    engine.run = replace(engine.run, current_step="prepare_candidates")

    with pytest.raises(TransientError, match="尚未进入产品卡等待边界") as caught:
        await projector.handle(_event())

    assert len(sourcing.ready_calls) == 1
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    engine.run = replace(engine.run, current_step="await_product_cards")

    await projector.handle(_event())

    assert engine.product_cards_prepared_events == 1


@pytest.mark.asyncio
async def test_prior_run_delivery_cannot_satisfy_active_run_or_mismatched_generation() -> (
    None
):
    """历史 Run 的相同指纹和错误 generation 都不得证明当前 Run 已被唤醒。"""

    projector, _sourcing, _products, engine = _projector()
    engine.historical_prepared = True

    await projector.handle(_event())

    assert engine.product_cards_prepared_events == 1
    assert all(query[7] == engine.run.run_id for query in engine.queries)

    mismatch_projector, _sourcing, _products, mismatch = _projector()
    mismatch.run.context["candidate_set_hash"] = "b" * 64
    with pytest.raises(ValidationError, match="generation"):
        await mismatch_projector.handle(_event())
    assert mismatch.product_cards_prepared_events == 0


@pytest.mark.asyncio
async def test_projection_rejects_more_than_three_or_final_ready_event() -> None:
    """三卡上限与唯一触发事件如被放宽，会扩大审核成本或形成循环投影。"""

    projector, sourcing, products, engine = _projector()
    four = tuple(SupplierCandidateId(f"spc-{index}") for index in range(1, 5))
    event = SourcingCandidatesVerified(
        tenant_id=TENANT,
        occurred_at=NOW,
        case_id=CASE_ID,
        candidate_ids=four,
        case_version=VERSION,
        candidate_set_hash=GENERATION_HASH,
    )
    with pytest.raises(ValidationError, match="至多三个"):
        await projector.handle(event)
    with pytest.raises(ValidationError, match="SourcingCandidatesVerified"):
        await projector.handle(
            SourcingCandidatesReady(
                tenant_id=TENANT,
                occurred_at=NOW,
                case_id=CASE_ID,
                option_ids=(SourcingSupplyOptionId("sop-ready"),),
                candidate_ids=(CANDIDATE_ID,),
            )
        )
    assert sourcing.read_calls == []
    assert products.calls == []
    assert engine.product_cards_prepared_events == 0


REVIEW_COMMAND = SourcingReviewCommand(
    primary_option_id=SourcingSupplyOptionId("sop-primary"),
    alternate_option_ids=(SourcingSupplyOptionId("sop-alternate"),),
    reason="证据最完整",
    expected_case_version=8,
)


class _ReviewSourcing:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.error: Exception | None = None
        self.review_fact = SourcingReview(
            review_id=SourcingReviewId("srv-review"),
            tenant_id=TENANT,
            case_id=CASE_ID,
            primary_option_id=REVIEW_COMMAND.primary_option_id,
            alternate_option_ids=REVIEW_COMMAND.alternate_option_ids,
            reason=REVIEW_COMMAND.reason,
            expected_case_version=REVIEW_COMMAND.expected_case_version,
            submitted_by=EmployeeId("emp-reviewer"),
            submitted_at=NOW,
            confirmed_by=EmployeeId("emp-boss"),
            confirmed_at=NOW,
        )

    async def review(self, tenant_id, case_id, command, *, actor):
        self.calls.append((tenant_id, case_id, command, actor))
        if self.error is not None:
            raise self.error
        return self.review_fact

    async def get_case(self, tenant_id, actor, case_id):
        return CaseView(
            case_id=str(case_id),
            need_id="need-projection",
            state="candidates_ready",
            opened_at=NOW,
        )


class _ReviewEngine:
    def __init__(
        self,
        *,
        step: str = "await_review",
        status: StepStatus = StepStatus.WAITING_EVENT,
        stop_reason: str | None = None,
    ) -> None:
        context: dict[str, Any] = {
            "case_id": str(CASE_ID),
            "supplier_candidate_ids": ["spc-candidate-1"],
            "candidate_case_version": VERSION,
            "candidate_set_hash": GENERATION_HASH,
        }
        if stop_reason is not None:
            context["sourcing_stop_reason"] = stop_reason
        self.run = WorkflowRun(
            run_id=RunId("run-review"),
            tenant_id=TENANT,
            workflow_type="sourcing_case",
            workflow_version=2,
            subject_ref=str(CASE_ID),
            current_step=step,
            status=status,
            created_at=NOW,
            context=context,
        )
        self.delivered: list[tuple[str, dict[str, Any]]] = []
        self.ledger: list[tuple[str, dict[str, Any]]] = []
        self.historical_delivered = False
        self.queries: list[tuple[Any, ...]] = []
        self.find_error: Exception | None = None
        self.get_error: Exception | None = None
        self.query_error: Exception | None = None
        self.deliver_error: Exception | None = None
        self.accept_delivery = True

    async def find_active_run(self, tenant_id, workflow_type, subject_ref):
        if self.find_error is not None:
            raise self.find_error
        return self.run.run_id

    async def get_run(self, tenant_id, run_id):
        if self.get_error is not None:
            raise self.get_error
        return self.run

    async def has_delivered_event(
        self,
        tenant_id,
        workflow_type,
        subject_ref,
        event_type,
        payload,
        *,
        workflow_version=None,
        required_context=None,
        run_id=None,
    ):
        if self.query_error is not None:
            raise self.query_error
        self.queries.append(
            (
                tenant_id,
                workflow_type,
                subject_ref,
                event_type,
                dict(payload),
                workflow_version,
                dict(required_context or {}),
                run_id,
            )
        )
        if run_id is None and self.historical_delivered:
            return True
        return (event_type, dict(payload)) in self.ledger

    async def deliver_event(self, tenant_id, run_id, event_type, payload):
        if self.deliver_error is not None:
            raise self.deliver_error
        item = (event_type, dict(payload))
        self.delivered.append(item)
        if not self.accept_delivery:
            return False
        self.ledger.append(item)
        if event_type == "SourcingReviewSubmitted":
            self.run = replace(
                self.run,
                current_step="handoff_costing",
                status=StepStatus.RUNNING,
                context={
                    **self.run.context,
                    "sourcing_stop_reason": "opportunity_required",
                },
            )
        return True


class _UnusedQuota:
    pass


@pytest.mark.asyncio
async def test_review_saves_then_wakes_await_review_and_exact_request_replays() -> None:
    """审核事实必须先于唤醒持久化，同请求重放不得改为交接重试。"""

    sourcing = _ReviewSourcing()
    engine = _ReviewEngine()
    application = SourcingCaseApplication(
        sourcing=sourcing, quota=_UnusedQuota(), engine=engine
    )

    first = await application.review(
        TENANT,
        CASE_ID,
        REVIEW_COMMAND,
        request_id="review-request-1",
        actor=SOURCING_ACTOR,
    )
    replayed = await application.review(
        TENANT,
        CASE_ID,
        REVIEW_COMMAND,
        request_id="review-request-1",
        actor=SOURCING_ACTOR,
    )

    assert first == replayed == sourcing.review_fact
    assert len(sourcing.calls) == 2
    assert engine.delivered == [
        (
            "SourcingReviewSubmitted",
            {"review_id": "srv-review", "request_id": "review-request-1"},
        )
    ]
    assert all(
        query[0:4] == (TENANT, "sourcing_case", str(CASE_ID), "SourcingReviewSubmitted")
        and query[5:]
        == (
            2,
            {
                "case_id": str(CASE_ID),
                "supplier_candidate_ids": ["spc-candidate-1"],
                "candidate_case_version": VERSION,
                "candidate_set_hash": GENERATION_HASH,
            },
            RunId("run-review"),
        )
        for query in engine.queries
        if query[3] == "SourcingReviewSubmitted"
    )


@pytest.mark.asyncio
async def test_new_review_request_retries_only_opportunity_required_handoff() -> None:
    """新显式请求 ID 只能在持久停止原因精确匹配时唤醒交接。"""

    sourcing = _ReviewSourcing()
    engine = _ReviewEngine(
        step="handoff_costing",
        status=StepStatus.RUNNING,
        stop_reason="opportunity_required",
    )
    application = SourcingCaseApplication(
        sourcing=sourcing, quota=_UnusedQuota(), engine=engine
    )

    result = await application.review(
        TENANT,
        CASE_ID,
        REVIEW_COMMAND,
        request_id="retry-request-2",
        actor=SOURCING_ACTOR,
    )

    assert result == sourcing.review_fact
    assert engine.delivered == [
        (
            "SourcingHandoffRetryRequested",
            {"review_id": "srv-review", "request_id": "retry-request-2"},
        )
    ]


@pytest.mark.asyncio
async def test_review_ignores_identical_event_from_prior_run_and_binds_generation() -> (
    None
):
    """相同 Case 的历史 Run 指纹不得吞掉当前 owning Run 的审核唤醒。"""

    sourcing = _ReviewSourcing()
    engine = _ReviewEngine()
    engine.historical_delivered = True
    application = SourcingCaseApplication(
        sourcing=sourcing, quota=_UnusedQuota(), engine=engine
    )

    await application.review(
        TENANT,
        CASE_ID,
        REVIEW_COMMAND,
        request_id="review-current-run",
        actor=SOURCING_ACTOR,
    )

    assert engine.delivered == [
        (
            "SourcingReviewSubmitted",
            {"review_id": "srv-review", "request_id": "review-current-run"},
        )
    ]
    assert all(query[7] == RunId("run-review") for query in engine.queries)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure_point", "message"),
    (
        ("find_error", "寻源审核工作流暂不可用"),
        ("get_error", "寻源审核工作流暂不可用"),
        ("query_error", "寻源审核工作流证据暂不可用"),
        ("deliver_error", "寻源审核工作流唤醒暂不可用"),
    ),
)
async def test_review_maps_every_raw_engine_failure_to_detached_transient(
    failure_point: str, message: str
) -> None:
    """Engine 任一原始失败若被永久化，会使已持久化 Review 无法恢复投递。"""

    sourcing = _ReviewSourcing()
    engine = _ReviewEngine()
    setattr(
        engine,
        failure_point,
        RuntimeError("postgres://user:secret@db/private token=raw-secret"),
    )
    application = SourcingCaseApplication(
        sourcing=sourcing, quota=_UnusedQuota(), engine=engine
    )

    with pytest.raises(SourcingPlanDeliveryError, match=message) as caught:
        await application.review(
            TENANT,
            CASE_ID,
            REVIEW_COMMAND,
            request_id=f"review-{failure_point}",
            actor=SOURCING_ACTOR,
        )

    assert sourcing.calls
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_review_false_delivery_at_exact_target_is_detached_transient() -> None:
    """目标 step 的 entry 尚未完成时，false delivery 必须等待精确重试。"""

    sourcing = _ReviewSourcing()
    engine = _ReviewEngine(status=StepStatus.RUNNING)
    engine.accept_delivery = False
    application = SourcingCaseApplication(
        sourcing=sourcing, quota=_UnusedQuota(), engine=engine
    )

    with pytest.raises(
        SourcingPlanDeliveryError, match="寻源审核工作流尚未进入等待边界"
    ) as caught:
        await application.review(
            TENANT,
            CASE_ID,
            REVIEW_COMMAND,
            request_id="review-pending-entry",
            actor=SOURCING_ACTOR,
        )

    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_review_rejects_unbounded_request_terminal_run_and_raw_sourcing_error() -> (
    None
):
    """请求 ID、终态 Run 或下层自由错误不得穿过应用边界。"""

    sourcing = _ReviewSourcing()
    terminal = _ReviewEngine(status=StepStatus.COMPLETED)
    application = SourcingCaseApplication(
        sourcing=sourcing, quota=_UnusedQuota(), engine=terminal
    )
    with pytest.raises(ValidationError, match="request_id 无效"):
        await application.review(
            TENANT,
            CASE_ID,
            REVIEW_COMMAND,
            request_id=" ",
            actor=SOURCING_ACTOR,
        )
    with pytest.raises(ValidationError, match="Workflow Run 绑定无效"):
        await application.review(
            TENANT,
            CASE_ID,
            REVIEW_COMMAND,
            request_id="review-terminal",
            actor=SOURCING_ACTOR,
        )

    sourcing.error = ValidationError(
        "postgres://user:secret@db/private", context={"token": "raw-secret"}
    )
    with pytest.raises(ValidationError, match="寻源审核保存失败") as caught:
        await application.review(
            TENANT,
            CASE_ID,
            REVIEW_COMMAND,
            request_id="review-error",
            actor=SOURCING_ACTOR,
        )
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "secret" not in str(caught.value)
