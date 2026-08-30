"""Sourcing Case V2 有界公开搜索、回执恢复与零下游动作。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from agent_runtime.sourcing_agent import (
    SourcingObservedLiteral,
    SourcingObservedPriceTier,
    SourcingObservedSpec,
    SourcingPageCandidateDraft,
    SourcingPageEvidence,
)
from connectors.search_contracts import SearchResult
from connectors.web_search.client import PageSnapshot
from domains.sourcing.schemas import (
    NeedFact,
    PublicPageAttempt,
    PublicPageAttemptClaim,
    PublicPageAttemptOutcome,
    PublicPageAttemptStatus,
    PublicSourcingPlanCommand,
    PublicSourcingQuery,
    SourcingNeedSnapshot,
)
from domains.sourcing.service import PublicPlanStatus, PublicSourcingPlan
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    RunId,
    SourcingCaseId,
    SourcingPlanId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.provenance import ProvenanceSummary, SourceType
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.free_search_contracts import FreeSearchError, FreeSearchStopReason
from tool_gateway.handlers.web_slots import SearchResultBatch
from workflows.engine.runner import StepStatus, WorkflowRun
from workflows.sourcing_case.steps import PublicSearchStep, sourcing_search_request_key

NOW = datetime(2026, 8, 31, 10, tzinfo=UTC)
TENANT = TenantId("tenant-sourcing-search")
CASE_ID = SourcingCaseId("src-sourcing-search")
NEED_ID = ValidatedNeedId("need-sourcing-search")
PLAN_ID = SourcingPlanId("spl-sourcing-search")
RUN_ID = RunId("run-sourcing-search")
PLAN_HASH = "a" * 64


def _provenance() -> ProvenanceSummary:
    return ProvenanceSummary(
        source_type=SourceType.CONVERSATION,
        source_id="msg-sourcing-search",
        extracted_by="human",
        extracted_at=NOW,
        confirmed_by=EmployeeId("emp-sourcing-search"),
        confirmed_at=NOW,
    )


def _need() -> SourcingNeedSnapshot:
    return SourcingNeedSnapshot(
        need_id=NEED_ID,
        completeness=3,
        derivation_version="need-completeness-v1",
        product_category=NeedFact(value="hinges", provenance=_provenance()),
        material=NeedFact(value="stainless steel", provenance=_provenance()),
        quantity=NeedFact(value=500, provenance=_provenance()),
        unit=NeedFact(value="piece", provenance=_provenance()),
        snapshot_hash="b" * 64,
    )


def _plan(*, pages: int = 3) -> PublicSourcingPlan:
    created = PublicSourcingPlan.create(
        TENANT,
        PublicSourcingPlanCommand(
            plan_id=PLAN_ID,
            case_id=CASE_ID,
            target_countries=("US", "DE"),
            product_category="hinges",
            queries=(
                PublicSourcingQuery(query_text="US hinge factory", target_country="US"),
                PublicSourcingQuery(query_text="DE hinge factory", target_country="DE"),
            ),
            max_search_queries=2,
            max_pages_read=pages,
            provider="tavily",
            search_depth="basic",
            usage_credits_remaining=10,
            worst_case_credits=2,
            version=1,
            expected_case_version=6,
        ),
        created_at=NOW,
    )
    return replace(
        created,
        plan_hash=PLAN_HASH,
        status=PublicPlanStatus.RUNNING,
        confirmed_by=EmployeeId("boss-sourcing-search"),
        confirmed_at=NOW,
        authorized_plan_hash=PLAN_HASH,
    )


def _run() -> WorkflowRun:
    return WorkflowRun(
        run_id=RUN_ID,
        tenant_id=TENANT,
        workflow_type="sourcing_case",
        workflow_version=2,
        subject_ref=str(CASE_ID),
        current_step="public_search",
        status=StepStatus.RUNNING,
        created_at=NOW,
        context={
            "case_id": str(CASE_ID),
            "need_id": str(NEED_ID),
            "need_snapshot_hash": "b" * 64,
            "sourcing_plan_id": str(PLAN_ID),
            "sourcing_plan_hash": PLAN_HASH,
        },
    )


def _result(index: int) -> SearchResult:
    return SearchResult(
        f"Factory {index}",
        f"https://factory-{index}.example/products/hinge",
        f"locator description {index}",
    )


def _batch(query_index: int, results: tuple[SearchResult, ...]) -> SearchResultBatch:
    return SearchResultBatch(
        f"wsb_0{query_index + 1}K39P9M5D6K4A91YEQ80EJZ0X",
        TENANT,
        ("US", "DE")[query_index],
        "hinges",
        results,
    )


def _page(index: int) -> PageSnapshot:
    return PageSnapshot(
        text=f"Factory {index} supplies stainless steel hinges. MOQ 100, USD 2.50 per piece.",
        url=f"https://factory-{index}.example/products/hinge",
        observed_at=NOW,
        content_hash=f"{index + 1:x}" * 64,
        snapshot_artifact_ref=ArtifactId(f"art_0{index + 1}K39P9M5D6K4A91YEQ80EJZ0X"),
    )


def _draft(page: PageSnapshot, index: int) -> SourcingPageCandidateDraft:
    evidence = SourcingPageEvidence(
        source_url=page.url,
        observed_at=page.observed_at,
        content_hash=page.content_hash,
        snapshot_artifact_ref=page.snapshot_artifact_ref,
    )

    def literal(value: str) -> SourcingObservedLiteral:
        return SourcingObservedLiteral(
            literal=value,
            source_quote=page.text,
            snapshot_artifact_ref=page.snapshot_artifact_ref,
        )

    price = literal("USD 2.50")
    quantity = literal("100")
    unit = literal("piece")
    currency = literal("USD")
    return SourcingPageCandidateDraft(
        evidence=evidence,
        supplier_name=literal(f"Factory {index}"),
        product_title=literal("stainless steel hinges"),
        specs=(
            SourcingObservedSpec(
                spec_name="product_type", required="hinges", observed=literal("hinges")
            ),
            SourcingObservedSpec(
                spec_name="material",
                required="stainless steel",
                observed=literal("stainless steel"),
            ),
        ),
        moq=100,
        moq_literal=quantity,
        price_tiers=(
            SourcingObservedPriceTier(
                minimum_quantity=100,
                amount=Decimal("2.50"),
                unit="piece",
                currency="USD",
                quantity_literal=quantity,
                price_literal=price,
                unit_literal=unit,
                currency_literal=currency,
                rejection_reasons=(),
            ),
        ),
        rejection_reasons=(),
    )


class _NeedReader:
    async def read(self, tenant_id, need_id):
        assert (tenant_id, need_id) == (TENANT, NEED_ID)
        return _need()


class _PlanReader:
    def __init__(self, plan: PublicSourcingPlan | None = None) -> None:
        self.plan = plan or _plan()
        self.error: BaseException | None = None

    async def load_authorized(self, **binding):
        if self.error is not None:
            raise self.error
        assert binding == {
            "tenant_id": TENANT,
            "case_id": CASE_ID,
            "run_id": RUN_ID,
            "plan_id": PLAN_ID,
            "plan_hash": PLAN_HASH,
        }
        return self.plan


class _Quota:
    def __init__(self) -> None:
        self.reservations: dict[tuple[RunId, str], object] = {}
        self.error: BaseException | None = None

    async def get(self, run_id, request_key):
        if self.error is not None:
            raise self.error
        return self.reservations.get((run_id, request_key))


def _page_slot(
    query_index: int,
    result_index: int,
    *,
    status: str = "claimed",
    outcome: str | None = None,
    draft_id: str | None = None,
    has_supplier_identity: bool | None = None,
):
    return PublicPageAttempt(
        tenant_id=TENANT,
        case_id=CASE_ID,
        run_id=RUN_ID,
        plan_id=PLAN_ID,
        plan_hash=PLAN_HASH,
        query_index=query_index,
        result_index=result_index,
        status=PublicPageAttemptStatus(status),
        outcome=PublicPageAttemptOutcome(outcome) if outcome is not None else None,
        draft_id=draft_id,
        has_supplier_identity=has_supplier_identity,
    )


class _Receipts:
    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline
        self.restored: dict[int, SearchResultBatch] = {}
        self.saved: list[tuple[int, tuple[dict[str, str], ...]]] = []
        self.uncertain: list[int] = []
        self.commit_error: BaseException | None = None
        self.restore_error: BaseException | None = None
        self.record_uncertain_error: BaseException | None = None
        self.page_attempt_restore_error: BaseException | None = None
        self.page_attempt_claim_error: BaseException | None = None
        self.page_completion_error: BaseException | None = None
        self.page_slots: dict[tuple[int, int], object] = {}
        self.claim_conflicts: dict[tuple[int, int], PublicPageAttempt] = {}

    async def restore(self, *, tenant_id, run_id, plan_hash, query_index):
        if self.restore_error is not None:
            raise self.restore_error
        assert (tenant_id, run_id, plan_hash) == (TENANT, RUN_ID, PLAN_HASH)
        return self.restored.get(query_index)

    async def commit_locator_receipt(self, **values):
        if self.commit_error is not None:
            raise self.commit_error
        batch = values.pop("batch")
        assert values["tenant_id"] == TENANT
        locators = tuple(
            {"title": item.title, "url": item.url, "description": item.description}
            for item in batch.results
        )
        self.saved.append((values["query_index"], locators))
        self.timeline.append(f"receipt:{values['query_index']}")

    async def record_uncertain(self, **values):
        if self.record_uncertain_error is not None:
            raise self.record_uncertain_error
        self.uncertain.append(values["query_index"])

    async def restore_page_attempts(self, **values):
        if self.page_attempt_restore_error is not None:
            raise self.page_attempt_restore_error
        return tuple(self.page_slots.values())

    async def claim_page_attempt(self, **values):
        if self.page_attempt_claim_error is not None:
            raise self.page_attempt_claim_error
        key = (values["query_index"], values["result_index"])
        conflict = self.claim_conflicts.get(key)
        if conflict is not None:
            self.page_slots[key] = conflict
            return PublicPageAttemptClaim(claimed_new=False, slot=conflict)
        existing = self.page_slots.get(key)
        if existing is not None:
            return PublicPageAttemptClaim(claimed_new=False, slot=existing)
        slot = _page_slot(*key)
        self.page_slots[key] = slot
        return PublicPageAttemptClaim(claimed_new=True, slot=slot)

    async def complete_page_attempt(self, **values):
        if self.page_completion_error is not None:
            raise self.page_completion_error
        key = (values["query_index"], values["result_index"])
        existing = self.page_slots[key]
        completed = _page_slot(
            *key,
            status="completed",
            outcome=str(values["outcome"]),
            draft_id=values.get("draft_id"),
            has_supplier_identity=(
                True if values.get("draft_id") is not None else None
            ),
        )
        assert existing.status == "claimed" or existing == completed
        self.page_slots[key] = completed
        return completed


class _Searcher:
    def __init__(self, batches: list[SearchResultBatch], timeline: list[str]) -> None:
        self.batches = batches
        self.timeline = timeline
        self.calls: list[tuple[str, str, int, str]] = []
        self.released: list[str] = []
        self.discarded = 0
        self.error: BaseException | None = None
        self.release_error: BaseException | None = None
        self.discard_error: BaseException | None = None

    async def search(
        self, tenant_id, run_id, query, country, category, limit, *, quota_request_key
    ):
        assert (tenant_id, run_id, category) == (TENANT, RUN_ID, "hinges")
        self.calls.append((query, country, limit, quota_request_key))
        if self.error is not None:
            raise self.error
        return self.batches[("US", "DE").index(country)]

    def release(self, batch):
        if self.release_error is not None:
            raise self.release_error
        self.released.append(batch.handle)

    def discard_all(self):
        if self.discard_error is not None:
            raise self.discard_error
        self.discarded += 1


class _Pages:
    def __init__(self, pages: list[PageSnapshot], timeline: list[str]) -> None:
        self.pages = pages
        self.timeline = timeline
        self.calls: list[tuple[str, int]] = []
        self.error: BaseException | None = None

    async def read_page(self, tenant_id, run_id, batch, result_index):
        self.calls.append((batch.handle, result_index))
        self.timeline.append(f"page:{len(self.calls) - 1}")
        if self.error is not None:
            raise self.error
        return self.pages[len(self.calls) - 1]


class _Extractor:
    def __init__(self, pages: list[PageSnapshot]) -> None:
        self.pages = pages
        self.calls = 0
        self.no_identity = False
        self.error: BaseException | None = None

    async def extract(self, need, page):
        if self.error is not None:
            raise self.error
        assert need == _need()
        draft = _draft(page, self.calls)
        self.calls += 1
        if self.no_identity:
            return draft.model_copy(update={"supplier_name": None})
        return draft


class _Drafts:
    def __init__(self) -> None:
        self.calls: list[SourcingPageCandidateDraft] = []
        self.error: BaseException | None = None

    async def save(self, **values):
        if self.error is not None:
            raise self.error
        self.calls.append(values["draft"])
        return f"scd-{len(self.calls)}"


def _step(
    *,
    batches: list[SearchResultBatch] | None = None,
    pages: list[PageSnapshot] | None = None,
    plan: PublicSourcingPlan | None = None,
):
    timeline: list[str] = []
    page_values = pages or [_page(0), _page(1), _page(2)]
    searcher = _Searcher(
        batches or [_batch(0, (_result(0), _result(1))), _batch(1, (_result(2),))],
        timeline,
    )
    receipts = _Receipts(timeline)
    page_reader = _Pages(page_values, timeline)
    extractor = _Extractor(page_values)
    drafts = _Drafts()
    quota = _Quota()
    return (
        PublicSearchStep(
            need_reader=_NeedReader(),
            plan_reader=_PlanReader(plan),
            quota=quota,
            searcher=searcher,
            page_reader=page_reader,
            receipts=receipts,
            extractor=extractor,
            drafts=drafts,
        ),
        searcher,
        page_reader,
        receipts,
        extractor,
        drafts,
        quota,
        timeline,
    )


def test_sourcing_request_key_binds_plan_hash_and_zero_based_query_index() -> None:
    assert sourcing_search_request_key(PLAN_HASH, 0) == (
        "1a5fa746c6ae1b3bcd61a2370ed1ea6556ceb6ca6a686b2442a1786fcbfde8b0"
    )
    assert sourcing_search_request_key(PLAN_HASH, 1) != sourcing_search_request_key(
        PLAN_HASH, 0
    )


@pytest.mark.asyncio
async def test_ordered_search_persists_locator_before_pages_and_honors_budgets() -> (
    None
):
    step, searcher, pages, receipts, extractor, drafts, _, timeline = _step()

    result = await step.execute(_run())

    assert result == (
        "advance",
        "verify_candidates",
        {
            "sourcing_searches_used": 2,
            "sourcing_pages_used": 3,
            "supplier_candidate_draft_ids": ["scd-1", "scd-2", "scd-3"],
        },
    )
    assert [(call[0], call[1], call[2]) for call in searcher.calls] == [
        ("US hinge factory", "US", 3),
        ("DE hinge factory", "DE", 1),
    ]
    assert [call[3] for call in searcher.calls] == [
        sourcing_search_request_key(PLAN_HASH, 0),
        sourcing_search_request_key(PLAN_HASH, 1),
    ]
    assert timeline.index("receipt:0") < timeline.index("page:0")
    assert timeline.index("receipt:1") < timeline.index("page:2")
    assert len(pages.calls) == extractor.calls == len(drafts.calls) == 3
    assert len(receipts.saved) == 2
    assert len(searcher.released) == 2
    assert searcher.discarded == 1


@pytest.mark.asyncio
async def test_committed_locator_receipt_rehydrates_without_second_search() -> None:
    step, searcher, _, receipts, _, drafts, _, _ = _step(
        batches=[_batch(0, (_result(0),)), _batch(1, (_result(1),))],
        pages=[_page(0), _page(1)],
        plan=_plan(pages=2),
    )
    receipts.restored[0] = _batch(0, (_result(0),))

    result = await step.execute(_run())

    assert result[0:2] == ("advance", "verify_candidates")
    assert [call[0] for call in searcher.calls] == ["DE hinge factory"]
    assert len(drafts.calls) == 2
    assert searcher.released == [
        receipts.restored[0].handle,
        _batch(1, (_result(1),)).handle,
    ]


@pytest.mark.asyncio
async def test_empty_search_is_not_no_verifiable_supplier() -> None:
    step, searcher, pages, _, _, drafts, _, _ = _step(
        batches=[_batch(0, ()), _batch(1, ())]
    )

    result = await step.execute(_run())

    assert result[0:2] == ("wait", None)
    assert result[2]["sourcing_stop_reason"] == "no_search_results"
    assert len(searcher.calls) == 2
    assert pages.calls == drafts.calls == []


@pytest.mark.asyncio
async def test_pages_without_supplier_identity_stop_as_no_verifiable_supplier() -> None:
    step, _, _, _, extractor, drafts, _, _ = _step()
    extractor.no_identity = True

    result = await step.execute(_run())

    assert result[0:2] == ("wait", None)
    assert result[2]["sourcing_stop_reason"] == "no_verifiable_supplier"
    assert len(drafts.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "reason"),
    (
        (ToolErrorCategory.PAGE_ACCESS_FORBIDDEN, "page_access_forbidden"),
        (ToolErrorCategory.LOGIN_OR_CAPTCHA, "login_or_captcha"),
        (ToolErrorCategory.UNSAFE_REDIRECT, "unsafe_redirect"),
        (ToolErrorCategory.RATE_LIMITED, "provider_rate_limited"),
        (ToolErrorCategory.PROVIDER_TRANSIENT, "provider_timeout"),
    ),
)
async def test_page_or_provider_failures_keep_exact_stop_reason_and_cleanup(
    category: ToolErrorCategory, reason: str
) -> None:
    step, searcher, pages, _, _, drafts, _, _ = _step()
    if category in {
        ToolErrorCategory.RATE_LIMITED,
        ToolErrorCategory.PROVIDER_TRANSIENT,
    }:
        searcher.error = ToolGatewayError(category)
    else:
        pages.error = ToolGatewayError(category)

    result = await step.execute(_run())

    assert result[0:2] == ("wait", None)
    assert result[2]["sourcing_stop_reason"] == reason
    assert drafts.calls == []
    if category not in {
        ToolErrorCategory.RATE_LIMITED,
        ToolErrorCategory.PROVIDER_TRANSIENT,
    }:
        assert len(pages.calls) == 3
    assert searcher.discarded == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ToolGatewayError(ToolErrorCategory.RATE_LIMITED),
        ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT),
        FreeSearchError(FreeSearchStopReason.QUOTA_EXHAUSTED),
        FreeSearchError(FreeSearchStopReason.REQUEST_UNCERTAIN),
    ],
)
async def test_every_dispatched_search_failure_counts_one_attempt(error) -> None:
    step, searcher, _, _, _, _, _, _ = _step()
    searcher.error = error

    result = await step.execute(_run())

    assert result[2]["sourcing_searches_used"] == 1


@pytest.mark.asyncio
async def test_existing_reservation_without_receipt_counts_once() -> None:
    step, _, _, _, _, _, quota, _ = _step()
    quota.reservations[(RUN_ID, sourcing_search_request_key(PLAN_HASH, 0))] = object()

    result = await step.execute(_run())

    assert result[2]["sourcing_searches_used"] == 1


@pytest.mark.asyncio
async def test_gateway_validation_is_never_mislabeled_provider_timeout() -> None:
    step, searcher, _, _, _, _, _, _ = _step()
    searcher.error = ToolGatewayError(ToolErrorCategory.VALIDATION)

    with pytest.raises(ValidationError, match="搜索请求无效"):
        await step.execute(_run())


@pytest.mark.asyncio
async def test_restart_of_claimed_but_incomplete_page_requires_reconciliation() -> None:
    step, _, pages, receipts, _, drafts, _, _ = _step(plan=_plan(pages=1))
    receipts.page_slots[(0, 0)] = _page_slot(0, 0)

    result = await step.execute(_run())

    assert result[2]["sourcing_pages_used"] == 1
    assert result[2]["sourcing_stop_reason"] == "reconciliation_required"
    assert pages.calls == []
    assert drafts.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["after_read", "after_extract", "after_draft_save"])
async def test_crash_windows_leave_claimed_slot_for_exact_restart_reconciliation(
    failure_stage: str,
) -> None:
    step, _, pages, receipts, extractor, drafts, _, _ = _step(plan=_plan(pages=1))
    if failure_stage == "after_read":
        extractor.error = RuntimeError("sensitive-extract")
    elif failure_stage == "after_extract":
        drafts.error = RuntimeError("sensitive-save")
    else:
        receipts.page_completion_error = RuntimeError("sensitive-completion")

    with pytest.raises(ValidationError) as caught:
        await step.execute(_run())
    assert "sensitive" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None

    receipts.page_completion_error = None
    restarted = PublicSearchStep(
        need_reader=_NeedReader(),
        plan_reader=_PlanReader(_plan(pages=1)),
        quota=_Quota(),
        searcher=_Searcher([_batch(0, (_result(0),)), _batch(1, ())], []),
        page_reader=pages,
        receipts=receipts,
        extractor=_Extractor([_page(0)]),
        drafts=_Drafts(),
    )
    result = await restarted.execute(_run())
    assert result[2]["sourcing_stop_reason"] == "reconciliation_required"
    assert len(pages.calls) == 1


@pytest.mark.asyncio
async def test_restart_rehydrates_completed_draft_without_page_io() -> None:
    step, _, pages, receipts, _, drafts, _, _ = _step(plan=_plan(pages=1))
    receipts.page_slots[(0, 0)] = _page_slot(
        0,
        0,
        status="completed",
        outcome="draft_saved",
        draft_id="scd-existing",
        has_supplier_identity=True,
    )
    receipts.restored[0] = _batch(0, (_result(0),))

    result = await step.execute(_run())

    assert result == (
        "advance",
        "verify_candidates",
        {
            "sourcing_searches_used": 1,
            "sourcing_pages_used": 1,
            "supplier_candidate_draft_ids": ["scd-existing"],
        },
    )
    assert pages.calls == []
    assert drafts.calls == []


@pytest.mark.asyncio
async def test_restart_rehydrates_completed_page_rejection_without_no_results_label() -> None:
    step, _, pages, receipts, _, drafts, _, _ = _step(plan=_plan(pages=1))
    receipts.page_slots[(0, 0)] = _page_slot(
        0,
        0,
        status="completed",
        outcome="page_access_forbidden",
    )
    receipts.restored[0] = _batch(0, (_result(0),))

    result = await step.execute(_run())

    assert result[2]["sourcing_stop_reason"] == "page_access_forbidden"
    assert result[2]["sourcing_pages_used"] == 1
    assert pages.calls == []
    assert drafts.calls == []


@pytest.mark.asyncio
async def test_claim_conflict_aggregates_completed_qualified_draft_without_reread() -> None:
    step, _, pages, receipts, extractor, drafts, _, _ = _step(
        batches=[_batch(0, (_result(0),)), _batch(1, ())],
        pages=[_page(0)],
        plan=_plan(pages=1),
    )
    receipts.claim_conflicts[(0, 0)] = _page_slot(
        0,
        0,
        status="completed",
        outcome="draft_saved",
        draft_id="scd-race-winner",
        has_supplier_identity=True,
    )

    result = await step.execute(_run())

    assert result == (
        "advance",
        "verify_candidates",
        {
            "sourcing_searches_used": 1,
            "sourcing_pages_used": 1,
            "supplier_candidate_draft_ids": ["scd-race-winner"],
        },
    )
    assert pages.calls == []
    assert extractor.calls == 0
    assert drafts.calls == []


@pytest.mark.asyncio
async def test_claim_conflict_aggregates_completed_rejection_without_reread() -> None:
    step, _, pages, receipts, extractor, drafts, _, _ = _step(
        batches=[_batch(0, (_result(0),)), _batch(1, ())],
        pages=[_page(0)],
        plan=_plan(pages=1),
    )
    receipts.claim_conflicts[(0, 0)] = _page_slot(
        0,
        0,
        status="completed",
        outcome="login_or_captcha",
    )

    result = await step.execute(_run())

    assert result[2] == {
        "sourcing_stop_reason": "login_or_captcha",
        "sourcing_searches_used": 1,
        "sourcing_pages_used": 1,
    }
    assert pages.calls == []
    assert extractor.calls == 0
    assert drafts.calls == []


@pytest.mark.asyncio
async def test_cancellation_releases_batch_and_discards_outer_slot() -> None:
    step, searcher, pages, _, _, _, _, _ = _step()
    pages.error = asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await step.execute(_run())

    assert len(searcher.released) == 1
    assert searcher.discarded == 1


@pytest.mark.asyncio
async def test_receipt_failure_still_releases_acquired_batch() -> None:
    step, searcher, _, receipts, _, _, _, _ = _step()
    receipts.commit_error = RuntimeError("receipt-sensitive-payload")

    with pytest.raises(ValidationError) as caught:
        await step.execute(_run())

    assert "receipt-sensitive-payload" not in str(caught.value)
    assert len(searcher.released) == 1
    assert searcher.discarded == 1


@pytest.mark.asyncio
async def test_cleanup_failures_do_not_mask_primary_safe_result() -> None:
    step, searcher, pages, _, _, _, _, _ = _step()
    pages.error = ToolGatewayError(ToolErrorCategory.PAGE_ACCESS_FORBIDDEN)
    searcher.release_error = RuntimeError("sensitive-release")
    searcher.discard_error = RuntimeError("sensitive-discard")

    result = await step.execute(_run())

    assert result[2]["sourcing_stop_reason"] == "page_access_forbidden"


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ("release", "discard"))
async def test_cleanup_cancelled_error_never_masks_success(cleanup: str) -> None:
    step, searcher, _, _, _, _, _, _ = _step()
    error = asyncio.CancelledError(f"sensitive-{cleanup}")
    if cleanup == "release":
        searcher.release_error = error
    else:
        searcher.discard_error = error

    result = await step.execute(_run())

    assert result[0:2] == ("advance", "verify_candidates")


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ("release", "discard"))
async def test_cleanup_cancelled_error_never_masks_prior_safe_failure(
    cleanup: str,
) -> None:
    step, searcher, pages, _, _, _, _, _ = _step()
    pages.error = RuntimeError("sensitive-primary-page")
    cleanup_error = asyncio.CancelledError(f"sensitive-{cleanup}")
    if cleanup == "release":
        searcher.release_error = cleanup_error
    else:
        searcher.discard_error = cleanup_error

    with pytest.raises(ValidationError) as caught:
        await step.execute(_run())

    assert str(caught.value) == "公开寻源页面读取失败"
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "boundary",
    (
        "receipt_restore",
        "quota_get",
        "attempt_restore",
        "attempt_claim",
        "page_reader",
        "extractor",
        "draft_repository",
        "record_uncertain",
        "attempt_completion",
        "locator_commit",
    ),
)
async def test_dependency_exceptions_are_fully_detached(
    boundary: str,
) -> None:
    step, searcher, pages, receipts, extractor, drafts, quota, _ = _step(
        plan=_plan(pages=1)
    )
    sensitive = RuntimeError(f"sensitive-{boundary}")
    if boundary == "receipt_restore":
        receipts.restore_error = sensitive
    elif boundary == "quota_get":
        quota.error = sensitive
    elif boundary == "attempt_restore":
        receipts.page_attempt_restore_error = sensitive
    elif boundary == "attempt_claim":
        receipts.page_attempt_claim_error = sensitive
    elif boundary == "page_reader":
        pages.error = sensitive
    elif boundary == "extractor":
        extractor.error = sensitive
    elif boundary == "draft_repository":
        drafts.error = sensitive
    elif boundary == "record_uncertain":
        searcher.error = FreeSearchError(FreeSearchStopReason.REQUEST_UNCERTAIN)
        receipts.record_uncertain_error = sensitive
    elif boundary == "attempt_completion":
        receipts.page_completion_error = sensitive
    else:
        receipts.commit_error = sensitive

    with pytest.raises((TransientError, ValidationError)) as caught:
        await step.execute(_run())

    assert "sensitive" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_trusted_draft_writer_excludes_raw_quote_and_keeps_incomplete_calibration(
    monkeypatch,
) -> None:
    from apps.scheduler_worker import sourcing_web
    from domains.sourcing.schemas import PublicCandidateDraft

    saved: list[PublicCandidateDraft] = []

    class _Repo:
        async def get_or_create_canonical(self, tenant_id, draft):
            assert tenant_id == TENANT
            saved.append(draft)
            return draft

    class _Uow:
        candidate_drafts = _Repo()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(
        sourcing_web,
        "SqlAlchemySourcingUnitOfWork",
        lambda *_args, **_kwargs: _Uow(),
    )

    async def verified(_self, _draft):
        return None

    monkeypatch.setattr(
        sourcing_web.PostgresPublicCandidateDraftWriter,
        "_require_verified_artifact",
        verified,
    )
    writer = sourcing_web.PostgresPublicCandidateDraftWriter(
        object(), TENANT, now=lambda: NOW
    )
    raw = _draft(_page(0), 0).model_copy(update={"supplier_name": None})

    first = await writer.save(
        tenant_id=TENANT,
        case_id=CASE_ID,
        run_id=RUN_ID,
        plan_id=PLAN_ID,
        plan_hash=PLAN_HASH,
        query_index=0,
        result_index=0,
        draft=raw,
    )
    second = await writer.save(
        tenant_id=TENANT,
        case_id=CASE_ID,
        run_id=RUN_ID,
        plan_id=PLAN_ID,
        plan_hash=PLAN_HASH,
        query_index=0,
        result_index=0,
        draft=raw,
    )

    assert first == second
    assert saved[0].source_key == saved[1].source_key
    assert not saved[0].is_verification_complete
    serialized = json.dumps(saved[0].model_dump(mode="json"))
    assert raw.price_tiers[0].price_literal.source_quote not in serialized
    assert "source_quote" not in serialized
