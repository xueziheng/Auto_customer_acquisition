"""Sourcing 公开搜索持久回执与 restart 恢复。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from apps.scheduler_worker.sourcing_web import PostgresSourcingWebPersistence
from connectors.search_contracts import SearchResult
from domains.sourcing.schemas import (
    NeedFact,
    PublicPageAttemptClaim,
    PublicPageAttemptOutcome,
    PublicPageAttemptStatus,
    PublicSourcingPlanCommand,
    PublicSourcingQuery,
    SourcingNeedSnapshot,
)
from domains.sourcing.service import PublicPlanStatus, PublicSourcingPlan
from infra.db.sourcing_uow import SqlAlchemySourcingUnitOfWork
from infra.db.tables import SourcingPageAttemptRow, SourcingSearchExecutionRow
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    RunId,
    SourcingCaseId,
    SourcingPlanId,
    TenantId,
    ValidatedNeedId,
    new_id,
)
from shared.schemas.provenance import ProvenanceSummary, SourceType
from tests.public_page_url_fixtures import HOSTILE_PUBLIC_PAGE_URLS
from tool_gateway.handlers.web_slots import WebSearchResultSlot
from workflows.sourcing_case.steps import sourcing_search_request_key

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 8, 31, 10, tzinfo=UTC)
CaseState = importlib.import_module("domains.sourcing.models").CaseState
SourcingCase = importlib.import_module("domains.sourcing.models").SourcingCase


def _unchecked_provider_result(url: str) -> SearchResult:
    """模拟旧版本或损坏 provider 回执，不能让 DTO 构造器掩盖持久化边界测试。"""

    result = object.__new__(SearchResult)
    object.__setattr__(result, "title", "Unsafe factory")
    object.__setattr__(result, "url", url)
    object.__setattr__(result, "description", "hostile locator")
    return result


async def test_committed_locator_receipt_rehydrates_exact_batch_without_query_payload(
    integration_engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = TenantId(new_id("tn"))
    need_id = ValidatedNeedId(new_id("need"))
    case_id = SourcingCaseId(new_id("src"))
    plan_id = SourcingPlanId(new_id("spl"))
    run_id = RunId(new_id("run"))
    artifact_id = ArtifactId(new_id("art"))
    provenance = ProvenanceSummary(
        source_type=SourceType.CONVERSATION,
        source_id="msg-sourcing-receipt",
        extracted_by="human",
        extracted_at=NOW,
        confirmed_by=EmployeeId("emp-sourcing-receipt"),
        confirmed_at=NOW,
    )
    snapshot = SourcingNeedSnapshot(
        need_id=need_id,
        completeness=2,
        derivation_version="need-completeness-v1",
        product_category=NeedFact(value="hinges", provenance=provenance),
        quantity=NeedFact(value=100, provenance=provenance),
        snapshot_hash="a" * 64,
    )
    case = SourcingCase(
        case_id=case_id,
        tenant_id=tenant_id,
        need_id=need_id,
        opened_at=NOW,
        workflow_version=2,
        trigger_key=f"sourcing-v2:{need_id}",
        need_snapshot=snapshot,
        need_snapshot_hash=snapshot.snapshot_hash,
        state_changed_at=NOW,
    )
    plan = PublicSourcingPlan.create(
        tenant_id,
        PublicSourcingPlanCommand(
            plan_id=plan_id,
            case_id=case_id,
            target_countries=("US",),
            product_category="hinges",
            queries=(
                PublicSourcingQuery(
                    query_text="US stainless hinge factory", target_country="US"
                ),
                PublicSourcingQuery(
                    query_text="US stainless hinge supplier", target_country="US"
                ),
                PublicSourcingQuery(
                    query_text="US stainless hinge manufacturer", target_country="US"
                ),
            ),
            max_search_queries=3,
            max_pages_read=1,
            provider="tavily",
            search_depth="basic",
            usage_credits_remaining=10,
            worst_case_credits=3,
            version=1,
            expected_case_version=case.version,
        ),
        created_at=NOW,
    )
    plan = plan.confirm(EmployeeId("emp-sourcing-receipt"), confirmed_at=NOW)
    plan = plan.transition_to(PublicPlanStatus.RUNNING)
    context = {
        "case_id": str(case_id),
        "need_id": str(need_id),
        "need_snapshot_hash": snapshot.snapshot_hash,
        "sourcing_plan_id": str(plan_id),
        "sourcing_plan_hash": plan.plan_hash,
    }
    async with integration_engine.begin() as connection:
        # 本测试构造的是已推进到 public_search 的持久投影，不模拟 Run start。
        await connection.execute(text("SET LOCAL session_replication_role = replica"))
        await connection.execute(
            text(
                "INSERT INTO validated_needs "
                "(tenant_id, need_id, account_id, product_category, source_message_id, status, created_at) "
                "VALUES (:tenant, :need, 'account-a', '{\"value\":\"hinges\"}', "
                "'message-a', 'sourcing_ready', :now)"
            ),
            {"tenant": tenant_id, "need": need_id, "now": NOW},
        )
        await connection.execute(
            text(
                "INSERT INTO raw_artifacts "
                "(tenant_id, artifact_id, kind, content_hash, size_bytes, mime_type, object_key, uploaded_at) "
                "VALUES (:tenant, :artifact, 'web_snapshot', :hash, 1, 'text/html', :key, :now)"
            ),
            {
                "tenant": tenant_id,
                "artifact": artifact_id,
                "hash": "b" * 64,
                "key": f"raw/{tenant_id}/{artifact_id}",
                "now": NOW,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO workflow_runs "
                "(run_id, tenant_id, workflow_type, workflow_version, subject_ref, current_step, "
                "status, context, idempotency_key) VALUES "
                "(:run, :tenant, 'sourcing_case', 2, :case, 'public_search', 'running', "
                "CAST(:context AS jsonb), :key)"
            ),
            {
                "run": run_id,
                "tenant": tenant_id,
                "case": case_id,
                "context": json.dumps(context),
                "key": f"sourcing-public:{case_id}",
            },
        )
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    async with SqlAlchemySourcingUnitOfWork(factory, tenant_id) as uow:
        await uow.cases.add(tenant_id, case)
        await uow.plans.add(tenant_id, plan)
    async with SqlAlchemySourcingUnitOfWork(factory, tenant_id) as uow:
        stored = await uow.cases.get(tenant_id, case_id)
        assert stored is not None
        stored.transition_to(
            CaseState.DISCOVERING, changed_at=NOW + timedelta(seconds=1)
        )
        await uow.cases.update(tenant_id, stored)
    async with SqlAlchemySourcingUnitOfWork(factory, tenant_id) as uow:
        stored = await uow.cases.get(tenant_id, case_id)
        assert stored is not None
        stored.active_search_plan_id = plan_id
        stored.transition_to(CaseState.VERIFYING, changed_at=NOW + timedelta(seconds=2))
        await uow.cases.update(tenant_id, stored)

    slot = WebSearchResultSlot(new_id, maximum_batches=4)
    persistence = PostgresSourcingWebPersistence(
        factory, tenant_id, slot, now=lambda: NOW
    )
    batch = slot.put(
        tenant_id,
        "US",
        "hinges",
        (
            SearchResult(
                "Factory A",
                "https://Factory.Example:443/products/hinge",
                "locator only",
            ),
            SearchResult(
                "Factory B",
                "http://Factory-B.Example:80/products/hinge",
                "locator only",
            ),
        ),
    )
    request_key = sourcing_search_request_key(plan.plan_hash, 0)
    await persistence.commit_locator_receipt(
        tenant_id=tenant_id,
        case_id=case_id,
        run_id=run_id,
        plan_id=plan_id,
        plan_hash=plan.plan_hash,
        query_index=0,
        request_key=request_key,
        query_hash=hashlib.sha256(plan.queries[0].query_text.encode()).hexdigest(),
        batch=batch,
    )
    slot.discard(batch.handle)

    restored = await persistence.restore(
        tenant_id=tenant_id,
        run_id=run_id,
        plan_hash=plan.plan_hash,
        query_index=0,
    )

    assert restored is not None
    canonical_results = (
        SearchResult(
            "Factory A",
            "https://factory.example/products/hinge",
            "locator only",
        ),
        SearchResult(
            "Factory B",
            "http://factory-b.example/products/hinge",
            "locator only",
        ),
    )
    assert restored.results == canonical_results
    async with factory() as session:
        row = (
            await session.execute(
                select(SourcingSearchExecutionRow).where(
                    SourcingSearchExecutionRow.tenant_id == str(tenant_id),
                    SourcingSearchExecutionRow.request_key == request_key,
                )
            )
        ).scalar_one()
    assert not hasattr(row, "query_text")
    assert (
        row.query_hash
        == hashlib.sha256(plan.queries[0].query_text.encode()).hexdigest()
    )
    assert row.locator_results == [
        {
            "title": "Factory A",
            "url": "https://factory.example/products/hinge",
            "description": "locator only",
        },
        {
            "title": "Factory B",
            "url": "http://factory-b.example/products/hinge",
            "description": "locator only",
        },
    ]

    binding = {
        "tenant_id": tenant_id,
        "case_id": case_id,
        "run_id": run_id,
        "plan_id": plan_id,
        "plan_hash": plan.plan_hash,
        "query_index": 0,
        "result_index": 0,
    }
    original_load = persistence._load
    both_loaded = asyncio.Event()
    load_count = 0

    async def align_competing_claims(*args, **kwargs):
        nonlocal load_count
        loaded = await original_load(*args, **kwargs)
        load_count += 1
        if load_count == 2:
            both_loaded.set()
        await asyncio.wait_for(both_loaded.wait(), timeout=2)
        return loaded

    monkeypatch.setattr(persistence, "_load", align_competing_claims)
    competing = {**binding, "result_index": 1}
    claims = await asyncio.gather(
        persistence.claim_page_attempt(**binding),
        persistence.claim_page_attempt(**competing),
    )
    assert (
        sum(
            isinstance(claim, PublicPageAttemptClaim) and claim.claimed_new
            for claim in claims
        )
        == 1
    )
    assert sum(claim is None for claim in claims) == 1
    monkeypatch.setattr(persistence, "_load", original_load)
    winner = next(
        claim
        for claim in claims
        if isinstance(claim, PublicPageAttemptClaim) and claim.claimed_new
    )
    restored_attempts = await persistence.restore_page_attempts(
        **{
            key: value
            for key, value in binding.items()
            if key not in {"query_index", "result_index"}
        }
    )
    assert restored_attempts == (winner.slot,)
    assert winner.slot.status is PublicPageAttemptStatus.CLAIMED

    draft_id = f"scd_{new_id('src')[:26]}"
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO sourcing_candidate_drafts "
                "(tenant_id, draft_id, case_id, run_id, plan_id, plan_hash, query_index, "
                "result_index, source_key, supplier_name, product_title, specs, moq, "
                "indicative_price_tiers, rejection_codes, evidence_url, evidence_observed_at, "
                "evidence_hash, evidence_artifact_ref, created_at) VALUES "
                "(:tenant, :draft, :case, :run, :plan, :hash, 0, :result, :source, "
                "'Factory A', 'Hinge', '[]', NULL, '[]', '[]', :url, :now, :evidence_hash, "
                ":artifact, :now)"
            ),
            {
                "tenant": tenant_id,
                "draft": draft_id,
                "case": case_id,
                "run": run_id,
                "plan": plan_id,
                "hash": plan.plan_hash,
                "result": winner.slot.result_index,
                "source": "e" * 64,
                "url": canonical_results[winner.slot.result_index].url,
                "now": NOW,
                "evidence_hash": "b" * 64,
                "artifact": artifact_id,
            },
        )
    completed = await persistence.complete_page_attempt(
        **{
            **binding,
            "result_index": winner.slot.result_index,
            "outcome": PublicPageAttemptOutcome.DRAFT_SAVED,
            "draft_id": draft_id,
        }
    )
    assert completed.status is PublicPageAttemptStatus.COMPLETED
    assert completed.outcome is PublicPageAttemptOutcome.DRAFT_SAVED
    assert completed.draft_id == draft_id
    assert completed.has_supplier_identity is True
    assert await persistence.restore_page_attempts(
        **{
            key: value
            for key, value in binding.items()
            if key not in {"query_index", "result_index"}
        }
    ) == (completed,)
    canonical = await persistence.claim_page_attempt(
        **{**binding, "result_index": winner.slot.result_index}
    )
    assert canonical == PublicPageAttemptClaim(claimed_new=False, slot=completed)
    async with factory() as session, session.begin():
        stored_attempt = (
            await session.execute(
                select(SourcingPageAttemptRow).where(
                    SourcingPageAttemptRow.tenant_id == str(tenant_id),
                    SourcingPageAttemptRow.run_id == str(run_id),
                    SourcingPageAttemptRow.plan_hash == plan.plan_hash,
                )
            )
        ).scalar_one()
        stored_attempt.result_index = 99
    with pytest.raises(ValidationError, match="页面槽绑定无效"):
        await persistence.restore_page_attempts(
            **{
                key: value
                for key, value in binding.items()
                if key not in {"query_index", "result_index"}
            }
        )
    async with factory() as session, session.begin():
        stored_attempt = (
            await session.execute(
                select(SourcingPageAttemptRow).where(
                    SourcingPageAttemptRow.tenant_id == str(tenant_id),
                    SourcingPageAttemptRow.run_id == str(run_id),
                    SourcingPageAttemptRow.plan_hash == plan.plan_hash,
                )
            )
        ).scalar_one()
        stored_attempt.result_index = winner.slot.result_index
    assert (
        await persistence.count_page_attempts(
            **{
                key: value
                for key, value in binding.items()
                if key not in {"query_index", "result_index"}
            }
        )
        == 1
    )

    canonical_locators = row.locator_results
    for assignments in (
        {
            "locator_results": [
                {
                    "title": 7,
                    "url": "https://factory.example/products/hinge",
                    "description": "locator only",
                }
            ]
        },
        {
            "locator_results": [
                {
                    "title": "Factory A",
                    "url": "http://127.0.0.1/private",
                    "description": "locator only",
                }
            ]
        },
        {"locator_results": [], "provider_status": "succeeded"},
        {"plan_hash": "f" * 64},
        {"query_index": 1},
        {"query_hash": "e" * 64},
    ):
        async with factory() as session, session.begin():
            target = (
                await session.execute(
                    select(SourcingSearchExecutionRow).where(
                        SourcingSearchExecutionRow.tenant_id == str(tenant_id),
                        SourcingSearchExecutionRow.request_key == request_key,
                    )
                )
            ).scalar_one()
            for name, value in assignments.items():
                setattr(target, name, value)
        with pytest.raises(ValidationError):
            await persistence.restore(
                tenant_id=tenant_id,
                run_id=run_id,
                plan_hash=plan.plan_hash,
                query_index=0,
            )
        async with factory() as session, session.begin():
            target = (
                await session.execute(
                    select(SourcingSearchExecutionRow).where(
                        SourcingSearchExecutionRow.tenant_id == str(tenant_id),
                        SourcingSearchExecutionRow.request_key == request_key,
                    )
                )
            ).scalar_one()
            target.locator_results = canonical_locators
            target.provider_status = "succeeded"
            target.plan_hash = plan.plan_hash
            target.query_index = 0
            target.query_hash = hashlib.sha256(
                plan.queries[0].query_text.encode()
            ).hexdigest()

    hostile_request_keys: list[str] = []
    for query_index, urls in enumerate(
        (HOSTILE_PUBLIC_PAGE_URLS[:20], HOSTILE_PUBLIC_PAGE_URLS[20:]), start=1
    ):
        hostile_batch = slot.put(
            tenant_id,
            "US",
            "hinges",
            tuple(_unchecked_provider_result(url) for url in urls),
        )
        hostile_request_key = sourcing_search_request_key(plan.plan_hash, query_index)
        hostile_request_keys.append(hostile_request_key)
        try:
            with pytest.raises(ValidationError, match="公开寻源 locator 回执无效"):
                await persistence.commit_locator_receipt(
                    tenant_id=tenant_id,
                    case_id=case_id,
                    run_id=run_id,
                    plan_id=plan_id,
                    plan_hash=plan.plan_hash,
                    query_index=query_index,
                    request_key=hostile_request_key,
                    query_hash=hashlib.sha256(
                        plan.queries[query_index].query_text.encode()
                    ).hexdigest(),
                    batch=hostile_batch,
                )
        finally:
            slot.discard(hostile_batch.handle)
    async with factory() as session:
        hostile_executions = (
            (
                await session.execute(
                    select(SourcingSearchExecutionRow).where(
                        SourcingSearchExecutionRow.tenant_id == str(tenant_id),
                        SourcingSearchExecutionRow.request_key.in_(
                            hostile_request_keys
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert hostile_executions == []

    attempts_before_refusal = await persistence.count_page_attempts(
        **{
            key: value
            for key, value in binding.items()
            if key not in {"query_index", "result_index"}
        }
    )
    assert attempts_before_refusal == 1
    for unsafe_url in (
        "https://Factory.Example:443/products/hinge",
        *HOSTILE_PUBLIC_PAGE_URLS,
    ):
        async with factory() as session, session.begin():
            target = (
                await session.execute(
                    select(SourcingSearchExecutionRow).where(
                        SourcingSearchExecutionRow.tenant_id == str(tenant_id),
                        SourcingSearchExecutionRow.request_key == request_key,
                    )
                )
            ).scalar_one()
            target.locator_results = [
                {
                    "title": "Unsafe factory",
                    "url": unsafe_url,
                    "description": "hostile locator",
                }
            ]
        with pytest.raises(ValidationError, match="公开寻源 locator 回执无效"):
            await persistence.restore(
                tenant_id=tenant_id,
                run_id=run_id,
                plan_hash=plan.plan_hash,
                query_index=0,
            )
        assert (
            await persistence.count_page_attempts(
                **{
                    key: value
                    for key, value in binding.items()
                    if key not in {"query_index", "result_index"}
                }
            )
            == attempts_before_refusal
        )
