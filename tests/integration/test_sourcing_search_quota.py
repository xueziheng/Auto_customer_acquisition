"""Sourcing 公开搜索持久回执与 restart 恢复。"""

from __future__ import annotations

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
    PublicSourcingPlanCommand,
    PublicSourcingQuery,
    SourcingNeedSnapshot,
)
from domains.sourcing.service import PublicPlanStatus, PublicSourcingPlan
from infra.db.sourcing_uow import SqlAlchemySourcingUnitOfWork
from infra.db.tables import SourcingSearchExecutionRow
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
from tool_gateway.handlers.web_slots import WebSearchResultSlot
from workflows.sourcing_case.steps import sourcing_search_request_key

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 8, 31, 10, tzinfo=UTC)
CaseState = importlib.import_module("domains.sourcing.models").CaseState
SourcingCase = importlib.import_module("domains.sourcing.models").SourcingCase


async def test_committed_locator_receipt_rehydrates_exact_batch_without_query_payload(
    integration_engine: AsyncEngine,
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
            ),
            max_search_queries=1,
            max_pages_read=2,
            provider="tavily",
            search_depth="basic",
            usage_credits_remaining=10,
            worst_case_credits=1,
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
                "https://factory.example/products/hinge",
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
    assert restored.results == batch.results
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
    assert row.locator_results == [
        {
            "title": "Factory A",
            "url": "https://factory.example/products/hinge",
            "description": "locator only",
        }
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
    assert await persistence.claim_page_attempt(**binding) is True
    assert await persistence.claim_page_attempt(**binding) is False
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
