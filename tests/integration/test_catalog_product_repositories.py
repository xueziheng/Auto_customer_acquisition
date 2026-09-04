"""Catalog Product Proposal tenant-bound PostgreSQL repositories."""

from __future__ import annotations

import asyncio
import importlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from domains.products.catalog_rules import (
    catalog_policy_content_hash,
    evaluate_catalog_facts,
)
from domains.products.schemas import (
    CatalogBlockedFactsInput,
    CatalogClusterFactsInput,
    CatalogProposalPolicyContent,
)
from infra.db.catalog_products_uow import SqlAlchemyCatalogProductsUnitOfWork
from infra.db.tables import (
    CatalogCultivationCaseRow,
    CatalogProductProposalRow,
    CatalogProposalEvaluationRow,
    CatalogProposalPolicyVersionRow,
    OutboxEventRow,
)
from shared.errors import IdempotencyConflict, TenantIsolationViolation, ValidationError
from shared.events.catalog import CatalogProposalPolicyActivated
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
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _symbol(module: str, name: str) -> Any:
    try:
        return getattr(importlib.import_module(module), name)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{module}.{name} 尚未实现（{exc}）")


CatalogCultivationCase = _symbol("domains.products.models", "CatalogCultivationCase")
CatalogProductProposal = _symbol("domains.products.models", "CatalogProductProposal")
CatalogProductProposalState = _symbol(
    "domains.products.models", "CatalogProductProposalState"
)
CatalogProposalEvaluation = _symbol(
    "domains.products.models", "CatalogProposalEvaluation"
)
CatalogProposalPolicyState = _symbol(
    "domains.products.models", "CatalogProposalPolicyState"
)
CatalogProposalPolicyVersion = _symbol(
    "domains.products.models", "CatalogProposalPolicyVersion"
)
CatalogPageCursor = _symbol("domains.products.repository", "CatalogPageCursor")


@pytest_asyncio.fixture
async def catalog_engine(db_url: str) -> AsyncEngine:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _ids(suffix: str) -> dict[str, str]:
    return {
        "tenant": f"tn_cat_{suffix}",
        "owner": f"emp_owner_{suffix}",
        "reviewer": f"emp_review_{suffix}",
        "cluster": f"ncl_{suffix}",
        "run": f"run_{suffix}",
    }


async def _seed_parents(engine: AsyncEngine, suffix: str) -> dict[str, str]:
    values = _ids(suffix)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO employees (employee_id,tenant_id,name,role) VALUES "
                "(:owner,:tenant,'Owner','product'),(:reviewer,:tenant,'Reviewer','boss')"
            ),
            values,
        )
        await connection.execute(
            text(
                "INSERT INTO need_clusters "
                "(tenant_id,cluster_id,category,keywords,countries,created_at,updated_at) "
                "VALUES (:tenant,:cluster,'hardware','[]'::jsonb,'[]'::jsonb,:now,:now)"
            ),
            values | {"now": NOW},
        )
        await connection.execute(
            text(
                "INSERT INTO workflow_runs "
                "(run_id,tenant_id,workflow_type,workflow_version,subject_ref,current_step,"
                "status,context,idempotency_key) VALUES "
                "(:run,:tenant,'catalog_cluster_evaluation',1,:cluster,'evaluate',"
                "'running','{}'::jsonb,:key)"
            ),
            values | {"key": f"catalog:{suffix}"},
        )
    return values


async def _cleanup(engine: AsyncEngine, tenant: str) -> None:
    async with engine.begin() as connection:
        for table, trigger in (
            ("catalog_cultivation_cases", "trg_catalog_cultivation_immutable"),
            ("catalog_product_proposals", "trg_catalog_product_proposal_guard"),
            ("catalog_proposal_evaluations", "trg_catalog_evaluation_immutable"),
            ("catalog_proposal_policy_versions", "trg_catalog_policy_version_guard"),
        ):
            await connection.execute(
                text(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
            )
            await connection.execute(
                text(f"DELETE FROM {table} WHERE tenant_id=:tenant"), {"tenant": tenant}
            )
            await connection.execute(
                text(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")
            )
        await connection.execute(
            text(
                "ALTER TABLE approval_packages DISABLE TRIGGER trg_approval_packages_guard"
            )
        )
        await connection.execute(
            text("DELETE FROM approval_packages WHERE tenant_id=:tenant"),
            {"tenant": tenant},
        )
        await connection.execute(
            text(
                "ALTER TABLE approval_packages ENABLE TRIGGER trg_approval_packages_guard"
            )
        )
        await connection.execute(
            text("DELETE FROM outbox_events WHERE tenant_id=:tenant"),
            {"tenant": tenant},
        )
        await connection.execute(
            text("DELETE FROM workflow_runs WHERE tenant_id=:tenant"),
            {"tenant": tenant},
        )
        await connection.execute(
            text("DELETE FROM need_clusters WHERE tenant_id=:tenant"),
            {"tenant": tenant},
        )
        await connection.execute(
            text("DELETE FROM employees WHERE tenant_id=:tenant"), {"tenant": tenant}
        )


def _policy_content() -> CatalogProposalPolicyContent:
    return CatalogProposalPolicyContent(
        minimum_distinct_accounts=3,
        minimum_recurring_accounts=None,
        minimum_distinct_countries=None,
        minimum_quantity_unit_accounts=None,
        require_unified_unit=False,
    )


def _policy(
    values: dict[str, str], marker: str, *, at: datetime = NOW
) -> CatalogProposalPolicyVersion:
    content = _policy_content()
    return CatalogProposalPolicyVersion(
        tenant_id=TenantId(values["tenant"]),
        policy_version_id=CatalogProposalPolicyVersionId(f"cpv_{marker}"),
        content=content,
        content_hash=catalog_policy_content_hash(content),
        base_active_version_id=None,
        proposed_by=EmployeeId(values["owner"]),
        creation_key=f"create-{marker}",
        creation_request_hash=HASH_B,
        approval_id=None,
        state=CatalogProposalPolicyState.PENDING_APPROVAL,
        created_at=at,
    )


def _facts(values: dict[str, str], marker: str) -> CatalogClusterFactsInput:
    return CatalogClusterFactsInput(
        tenant_id=TenantId(values["tenant"]),
        cluster_id=NeedClusterId(values["cluster"]),
        cluster_category="hardware",
        member_need_ids=(f"need_{marker}_1", f"need_{marker}_2", f"need_{marker}_3"),
        distinct_account_ids=(f"acc_{marker}_1", f"acc_{marker}_2", f"acc_{marker}_3"),
        member_count=3,
        distinct_account_count=3,
        known_country_codes=("KE",),
        unknown_country_account_count=2,
        recurring_true_account_count=0,
        recurring_false_account_count=0,
        recurring_unknown_account_count=3,
        quantity_unit_covered_account_count=0,
        unified_unit=None,
        safe_total_quantity=None,
        evidence_summaries=(),
        display_codes=("country_unknown",),
        facts_observed_at=NOW,
        facts_hash=(marker[0] * 64),
    )


def _evaluation(
    values: dict[str, str],
    policy: CatalogProposalPolicyVersion,
    marker: str,
    *,
    blocked: bool = False,
) -> CatalogProposalEvaluation:
    facts = _facts(values, marker)
    if blocked:
        damaged = facts.model_copy(update={"member_count": 4})
        result = evaluate_catalog_facts(_policy_content(), damaged)
        stored_facts = CatalogBlockedFactsInput(
            tenant_id=facts.tenant_id,
            cluster_id=facts.cluster_id,
            facts_hash=facts.facts_hash,
        )
    else:
        result = evaluate_catalog_facts(_policy_content(), facts)
        stored_facts = facts
    return CatalogProposalEvaluation(
        tenant_id=TenantId(values["tenant"]),
        evaluation_id=CatalogProposalEvaluationId(f"cpe_{marker}"),
        cluster_id=NeedClusterId(values["cluster"]),
        policy_version_id=policy.policy_version_id,
        facts_hash=facts.facts_hash,
        facts=stored_facts,
        rule_results=result.rule_results,
        overall_passed=result.overall_passed,
        blocked_reason=result.blocked_reason,
        proposed_by_run=RunId(values["run"]),
        created_at=NOW,
    )


async def _approval(
    engine: AsyncEngine,
    values: dict[str, str],
    approval_id: str,
    *,
    policy: CatalogProposalPolicyVersion,
    proposal: CatalogProductProposal | None = None,
    request_hash: str = HASH_C,
    state: str = "approved",
) -> None:
    if proposal is None:
        namespace = "catalog-policy-v1"
        approval_type = "catalog_proposal_policy_change"
        payload = {
            "schema_version": namespace,
            "tenant_id": values["tenant"],
            "approval_type": approval_type,
            "policy_version_id": str(policy.policy_version_id),
            "content_hash": policy.content_hash,
            "request_hash": request_hash,
        }
        change_set = f"catalog-policy:{policy.policy_version_id}:{policy.content_hash}"
        expires = NOW + timedelta(days=7)
    else:
        namespace = "catalog-cultivation-v1"
        approval_type = "catalog_product_cultivation"
        payload = {
            "schema_version": namespace,
            "tenant_id": values["tenant"],
            "approval_type": approval_type,
            "proposal_id": str(proposal.proposal_id),
            "policy_version_id": str(proposal.policy_version_id),
            "facts_hash": proposal.facts_hash,
            "request_hash": request_hash,
        }
        change_set = (
            f"catalog-cultivation:{proposal.proposal_id}:"
            f"{proposal.policy_version_id}:{proposal.facts_hash}"
        )
        expires = NOW + timedelta(days=3)
    decided = state in {"approved", "rejected"}
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO approval_packages "
                "(tenant_id,approval_id,approval_type,title,proposed_change,reason,"
                "blast_radius,created_at,expires_at,state,proposed_by_employee,evidence_refs,"
                "change_set_ref,owner_employee,contract_namespace,request_hash,"
                "expires_at_limit,decided_at,decided_by) VALUES "
                "(:tenant,:approval,:approval_type,'Catalog approval',CAST(:payload AS jsonb),"
                "'internal only','{}'::jsonb,:now,:expires,:state,:owner,'[]'::jsonb,"
                ":change_set,:owner,:namespace,:request_hash,:expires,:decided_at,:decided_by)"
            ),
            {
                **values,
                "approval": approval_id,
                "approval_type": approval_type,
                "payload": json.dumps(payload),
                "now": NOW,
                "expires": expires,
                "state": state,
                "change_set": change_set,
                "namespace": namespace,
                "request_hash": request_hash,
                "decided_at": NOW if decided else None,
                "decided_by": values["reviewer"] if decided else None,
            },
        )


async def _active_policy(
    engine: AsyncEngine,
    factory: async_sessionmaker,
    values: dict[str, str],
    marker: str,
) -> CatalogProposalPolicyVersion:
    policy = _policy(values, marker)
    async with SqlAlchemyCatalogProductsUnitOfWork(
        factory, TenantId(values["tenant"])
    ) as uow:
        await uow.policies.add(TenantId(values["tenant"]), policy)
    approval_id = f"apr_pol_{marker}"
    await _approval(engine, values, approval_id, policy=policy)
    active = replace(
        policy,
        approval_id=ApprovalId(approval_id),
        state=CatalogProposalPolicyState.ACTIVE,
        activated_at=NOW + timedelta(seconds=1),
    )
    async with SqlAlchemyCatalogProductsUnitOfWork(
        factory, TenantId(values["tenant"])
    ) as uow:
        return await uow.policies.update(TenantId(values["tenant"]), active)


async def _queued_proposal(
    engine: AsyncEngine,
    factory: async_sessionmaker,
    values: dict[str, str],
    marker: str,
) -> tuple[CatalogProposalPolicyVersion, CatalogProductProposal]:
    tenant = TenantId(values["tenant"])
    policy = await _active_policy(engine, factory, values, marker)
    evaluation = _evaluation(values, policy, marker[0])
    proposal = CatalogProductProposal(
        tenant_id=tenant,
        proposal_id=CatalogProductProposalId(f"cpr_{marker}"),
        evaluation_id=evaluation.evaluation_id,
        cluster_id=evaluation.cluster_id,
        policy_version_id=evaluation.policy_version_id,
        facts_hash=evaluation.facts_hash,
        owner_employee=EmployeeId(values["owner"]),
        proposed_by_run=RunId(values["run"]),
        approval_id=None,
        approval_request_hash=None,
        state=CatalogProductProposalState.AWAITING_APPROVAL_SUBMISSION,
        created_at=NOW,
        updated_at=NOW,
    )
    async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
        await uow.evaluations.add(tenant, evaluation)
        await uow.proposals.add(tenant, proposal)
    approval_id = f"apr_{marker}"
    await _approval(engine, values, approval_id, policy=policy, proposal=proposal)
    pending = replace(
        proposal,
        approval_id=ApprovalId(approval_id),
        approval_request_hash=HASH_C,
        state=CatalogProductProposalState.PENDING_REVIEW,
        updated_at=NOW + timedelta(seconds=2),
    )
    queued = replace(
        pending,
        state=CatalogProductProposalState.CULTIVATION_QUEUED,
        updated_at=NOW + timedelta(seconds=3),
    )
    async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
        await uow.proposals.update(tenant, pending)
    async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
        queued = await uow.proposals.update(tenant, queued)
    return policy, queued


async def _wait_for_for_update_block(
    engine: AsyncEngine,
    backend_pid: int,
    table_name: str,
    contender: asyncio.Task[object],
) -> None:
    async with asyncio.timeout(3):
        while True:
            if contender.done():
                await contender
                pytest.fail("contender 未在 SELECT FOR UPDATE 等待行锁")
            async with engine.connect() as connection:
                activity = (
                    (
                        await connection.execute(
                            text(
                                "SELECT pg_blocking_pids(pid) AS blockers,wait_event_type,query "
                                "FROM pg_stat_activity WHERE pid=:pid"
                            ),
                            {"pid": backend_pid},
                        )
                    )
                    .mappings()
                    .one()
                )
            query = activity["query"]
            if activity["blockers"]:
                assert activity["wait_event_type"] == "Lock"
                assert isinstance(query, str)
                assert query.lstrip().upper().startswith("SELECT")
                assert table_name in query
                assert "FOR UPDATE" in query.upper()
                return


@pytest.mark.parametrize(
    "evidence_refs",
    [
        "artifact-single",
        ["artifact-list"],
        (),
        ("",),
        (" artifact",),
        ("artifact ",),
        ("artifact\x00ref",),
        ("x" * 201,),
        ("artifact", 1),
        (["nested-list"],),
        ("artifact", "artifact"),
    ],
)
def test_cultivation_case_rejects_non_tuple_or_unsafe_evidence_refs(
    evidence_refs: object,
) -> None:
    with pytest.raises(ValidationError, match="evidence_refs|evidence_ref"):
        CatalogCultivationCase(
            tenant_id=TenantId("tn_evidence_model"),
            cultivation_case_id=CatalogCultivationCaseId("ccc_evidence_model"),
            proposal_id=CatalogProductProposalId("cpr_evidence_model"),
            approval_id=ApprovalId("apr_evidence_model"),
            cluster_id=NeedClusterId("ncl_evidence_model"),
            policy_version_id=CatalogProposalPolicyVersionId("cpv_evidence_model"),
            facts_hash=HASH_A,
            evidence_refs=evidence_refs,
            state="queued",
            queued_at=NOW,
        )


async def test_cultivation_repository_revalidates_before_sql_and_round_trips_tuple(
    catalog_engine: AsyncEngine,
) -> None:
    values = await _seed_parents(catalog_engine, "evidence")
    tenant = TenantId(values["tenant"])
    factory = async_sessionmaker(catalog_engine, expire_on_commit=False)
    try:
        policy, proposal = await _queued_proposal(
            catalog_engine, factory, values, "evidence"
        )
        cultivation_case = CatalogCultivationCase(
            tenant_id=tenant,
            cultivation_case_id=CatalogCultivationCaseId("ccc_evidence"),
            proposal_id=proposal.proposal_id,
            approval_id=proposal.approval_id,
            cluster_id=proposal.cluster_id,
            policy_version_id=policy.policy_version_id,
            facts_hash=proposal.facts_hash,
            evidence_refs=("artifact-a", "artifact-b"),
            state="queued",
            queued_at=NOW + timedelta(seconds=4),
        )
        malformed = replace(cultivation_case)
        object.__setattr__(malformed, "evidence_refs", "artifact-split")
        statements = 0

        def counted(*_args: object) -> None:
            nonlocal statements
            statements += 1

        event.listen(catalog_engine.sync_engine, "before_cursor_execute", counted)
        try:
            with pytest.raises(ValidationError, match="evidence_refs"):
                async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
                    await uow.cultivation_cases.add(tenant, malformed)
        finally:
            event.remove(catalog_engine.sync_engine, "before_cursor_execute", counted)
        assert statements == 0

        async with catalog_engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(func.count())
                    .select_from(CatalogCultivationCaseRow)
                    .where(CatalogCultivationCaseRow.tenant_id == str(tenant))
                )
                == 0
            )
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            persisted = await uow.cultivation_cases.add(tenant, cultivation_case)
            loaded = await uow.cultivation_cases.get(
                tenant, cultivation_case.cultivation_case_id
            )
        assert persisted.evidence_refs == ("artifact-a", "artifact-b")
        assert loaded == cultivation_case
    finally:
        await _cleanup(catalog_engine, values["tenant"])


async def test_policy_same_row_lock_waits_then_contender_reads_latest_state(
    catalog_engine: AsyncEngine,
) -> None:
    values = await _seed_parents(catalog_engine, "policy_lock")
    tenant = TenantId(values["tenant"])
    factory = async_sessionmaker(catalog_engine, expire_on_commit=False)
    contender: asyncio.Task[object] | None = None
    try:
        policy = _policy(values, "policy_lock")
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            await uow.policies.add(tenant, policy)
        await _approval(catalog_engine, values, "apr_policy_lock", policy=policy)
        active = replace(
            policy,
            approval_id=ApprovalId("apr_policy_lock"),
            state=CatalogProposalPolicyState.ACTIVE,
            activated_at=NOW + timedelta(seconds=1),
        )
        contender_pid: asyncio.Queue[int] = asyncio.Queue(maxsize=1)

        async def supersede_after_lock() -> object:
            async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
                pid = await uow._session.scalar(text("SELECT pg_backend_pid()"))
                assert isinstance(pid, int)
                await contender_pid.put(pid)
                current = await uow.policies.get_for_update(
                    tenant, policy.policy_version_id
                )
                assert current is not None
                assert current.state is CatalogProposalPolicyState.ACTIVE
                superseded = replace(
                    current,
                    state=CatalogProposalPolicyState.SUPERSEDED,
                    terminal_at=NOW + timedelta(seconds=2),
                )
                return await uow.policies.update(tenant, superseded)

        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            await uow.policies.update(tenant, active)
            contender = asyncio.create_task(supersede_after_lock())
            pid = await asyncio.wait_for(contender_pid.get(), timeout=3)
            await _wait_for_for_update_block(
                catalog_engine,
                pid,
                "catalog_proposal_policy_versions",
                contender,
            )
        result = await asyncio.wait_for(contender, timeout=3)
        assert result.state is CatalogProposalPolicyState.SUPERSEDED
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            stored = await uow.policies.get(tenant, policy.policy_version_id)
        assert stored == result
    finally:
        if contender is not None and not contender.done():
            contender.cancel()
            await asyncio.gather(contender, return_exceptions=True)
        await _cleanup(catalog_engine, values["tenant"])


async def test_proposal_same_row_lock_waits_then_contender_reads_latest_state(
    catalog_engine: AsyncEngine,
) -> None:
    values = await _seed_parents(catalog_engine, "proposal_lock")
    tenant = TenantId(values["tenant"])
    factory = async_sessionmaker(catalog_engine, expire_on_commit=False)
    contender: asyncio.Task[object] | None = None
    try:
        policy = await _active_policy(catalog_engine, factory, values, "proposal_lock")
        evaluation = _evaluation(values, policy, "d")
        proposal = CatalogProductProposal(
            tenant_id=tenant,
            proposal_id=CatalogProductProposalId("cpr_proposal_lock"),
            evaluation_id=evaluation.evaluation_id,
            cluster_id=evaluation.cluster_id,
            policy_version_id=evaluation.policy_version_id,
            facts_hash=evaluation.facts_hash,
            owner_employee=EmployeeId(values["owner"]),
            proposed_by_run=RunId(values["run"]),
            approval_id=None,
            approval_request_hash=None,
            state=CatalogProductProposalState.AWAITING_APPROVAL_SUBMISSION,
            created_at=NOW,
            updated_at=NOW,
        )
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            await uow.evaluations.add(tenant, evaluation)
            await uow.proposals.add(tenant, proposal)
        await _approval(
            catalog_engine,
            values,
            "apr_proposal_lock",
            policy=policy,
            proposal=proposal,
        )
        pending = replace(
            proposal,
            approval_id=ApprovalId("apr_proposal_lock"),
            approval_request_hash=HASH_C,
            state=CatalogProductProposalState.PENDING_REVIEW,
            updated_at=NOW + timedelta(seconds=2),
        )
        contender_pid: asyncio.Queue[int] = asyncio.Queue(maxsize=1)

        async def queue_after_lock() -> object:
            async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
                pid = await uow._session.scalar(text("SELECT pg_backend_pid()"))
                assert isinstance(pid, int)
                await contender_pid.put(pid)
                current = await uow.proposals.get_for_update(
                    tenant, proposal.proposal_id
                )
                assert current is not None
                assert current.state is CatalogProductProposalState.PENDING_REVIEW
                queued = replace(
                    current,
                    state=CatalogProductProposalState.CULTIVATION_QUEUED,
                    updated_at=NOW + timedelta(seconds=3),
                )
                return await uow.proposals.update(tenant, queued)

        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            await uow.proposals.update(tenant, pending)
            contender = asyncio.create_task(queue_after_lock())
            pid = await asyncio.wait_for(contender_pid.get(), timeout=3)
            await _wait_for_for_update_block(
                catalog_engine,
                pid,
                "catalog_product_proposals",
                contender,
            )
        result = await asyncio.wait_for(contender, timeout=3)
        assert result.state is CatalogProductProposalState.CULTIVATION_QUEUED
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            stored = await uow.proposals.get(tenant, proposal.proposal_id)
        assert stored == result
    finally:
        if contender is not None and not contender.done():
            contender.cancel()
            await asyncio.gather(contender, return_exceptions=True)
        await _cleanup(catalog_engine, values["tenant"])


async def test_round_trip_preserves_full_and_blocked_json_and_uow_atomicity(
    catalog_engine: AsyncEngine,
) -> None:
    values = await _seed_parents(catalog_engine, "roundtrip")
    tenant = TenantId(values["tenant"])
    factory = async_sessionmaker(catalog_engine, expire_on_commit=False)
    try:
        policy = await _active_policy(catalog_engine, factory, values, "roundtrip")
        normal = _evaluation(values, policy, "d")
        blocked = _evaluation(values, policy, "e", blocked=True)
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            assert await uow.evaluations.add(tenant, normal) == normal
            assert await uow.evaluations.add(tenant, blocked) == blocked
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            assert await uow.policies.get(tenant, policy.policy_version_id) == policy
            loaded_normal = await uow.evaluations.get(tenant, normal.evaluation_id)
            loaded_blocked = await uow.evaluations.get(tenant, blocked.evaluation_id)
        assert loaded_normal == normal
        assert type(loaded_normal.facts) is CatalogClusterFactsInput
        assert loaded_blocked == blocked
        assert type(loaded_blocked.facts) is CatalogBlockedFactsInput

        async with catalog_engine.begin() as connection:
            await connection.execute(
                text(
                    "ALTER TABLE catalog_proposal_evaluations "
                    "DISABLE TRIGGER trg_catalog_evaluation_immutable"
                )
            )
            await connection.execute(
                text(
                    "UPDATE catalog_proposal_evaluations SET facts=CAST(:facts AS jsonb) "
                    "WHERE tenant_id=:tenant AND evaluation_id=:evaluation"
                ),
                {
                    "tenant": str(tenant),
                    "evaluation": str(blocked.evaluation_id),
                    "facts": json.dumps(normal.facts.model_dump(mode="json")),
                },
            )
            await connection.execute(
                text(
                    "UPDATE catalog_proposal_evaluations SET facts=CAST(:facts AS jsonb) "
                    "WHERE tenant_id=:tenant AND evaluation_id=:evaluation"
                ),
                {
                    "tenant": str(tenant),
                    "evaluation": str(normal.evaluation_id),
                    "facts": json.dumps(blocked.facts.model_dump(mode="json")),
                },
            )
            await connection.execute(
                text(
                    "ALTER TABLE catalog_proposal_evaluations "
                    "ENABLE TRIGGER trg_catalog_evaluation_immutable"
                )
            )
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            with pytest.raises(ValueError):
                await uow.evaluations.get(tenant, blocked.evaluation_id)
            with pytest.raises(ValueError):
                await uow.evaluations.get(tenant, normal.evaluation_id)

        rolled_back = _policy(values, "rollback", at=NOW + timedelta(minutes=1))
        with pytest.raises(RuntimeError, match="force rollback"):
            async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
                await uow.policies.add(tenant, rolled_back)
                await uow.bus.publish(
                    CatalogProposalPolicyActivated(
                        tenant_id=tenant,
                        occurred_at=NOW,
                        policy_version_id=rolled_back.policy_version_id,
                        content_hash=rolled_back.content_hash,
                    )
                )
                raise RuntimeError("force rollback")
        async with catalog_engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(func.count())
                    .select_from(CatalogProposalPolicyVersionRow)
                    .where(
                        CatalogProposalPolicyVersionRow.tenant_id == str(tenant),
                        CatalogProposalPolicyVersionRow.policy_version_id
                        == str(rolled_back.policy_version_id),
                    )
                )
                == 0
            )
            assert (
                await connection.scalar(
                    select(func.count())
                    .select_from(OutboxEventRow)
                    .where(
                        OutboxEventRow.tenant_id == str(tenant),
                        OutboxEventRow.event_type == "CatalogProposalPolicyActivated",
                    )
                )
                == 0
            )
    finally:
        await _cleanup(catalog_engine, values["tenant"])


async def test_wrong_tenant_short_circuits_before_sql_and_cursor_is_tenant_bound(
    catalog_engine: AsyncEngine,
) -> None:
    values = await _seed_parents(catalog_engine, "tenant")
    tenant = TenantId(values["tenant"])
    other = TenantId("tn_cat_other")
    factory = async_sessionmaker(catalog_engine, expire_on_commit=False)
    statements = 0

    def counted(*_args: object) -> None:
        nonlocal statements
        statements += 1

    event.listen(catalog_engine.sync_engine, "before_cursor_execute", counted)
    try:
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            assert (
                await uow.policies.get(
                    other, CatalogProposalPolicyVersionId("cpv_hidden")
                )
                is None
            )
            assert (await uow.policies.list_versions(other, limit=10)).items == ()
            assert (
                await uow.evaluations.get(
                    other, CatalogProposalEvaluationId("cpe_hidden")
                )
                is None
            )
            assert (await uow.evaluations.list_evaluations(other, limit=10)).items == ()
            assert (
                await uow.proposals.get(other, CatalogProductProposalId("cpr_hidden"))
                is None
            )
            assert (await uow.proposals.list_proposals(other, limit=10)).items == ()
            assert (
                await uow.cultivation_cases.get(
                    other, CatalogCultivationCaseId("ccc_hidden")
                )
                is None
            )
            assert (await uow.cultivation_cases.list_cases(other, limit=10)).items == ()
            with pytest.raises(TenantIsolationViolation):
                await uow.policies.add(
                    other, _policy(values | {"tenant": str(other)}, "wrong")
                )
            wrong_values = values | {
                "tenant": str(other),
                "cluster": "ncl_other",
                "run": "run_other",
            }
            wrong_policy = _policy(wrong_values, "wrong_eval")
            wrong_evaluation = _evaluation(wrong_values, wrong_policy, "a")
            with pytest.raises(TenantIsolationViolation):
                await uow.evaluations.add(other, wrong_evaluation)
            wrong_proposal = CatalogProductProposal(
                tenant_id=other,
                proposal_id=CatalogProductProposalId("cpr_wrong"),
                evaluation_id=wrong_evaluation.evaluation_id,
                cluster_id=wrong_evaluation.cluster_id,
                policy_version_id=wrong_policy.policy_version_id,
                facts_hash=wrong_evaluation.facts_hash,
                owner_employee=EmployeeId("emp_other"),
                proposed_by_run=RunId("run_other"),
                approval_id=None,
                approval_request_hash=None,
                state=CatalogProductProposalState.AWAITING_APPROVAL_SUBMISSION,
                created_at=NOW,
                updated_at=NOW,
            )
            with pytest.raises(TenantIsolationViolation):
                await uow.proposals.add(other, wrong_proposal)
            with pytest.raises(TenantIsolationViolation):
                await uow.cultivation_cases.add(
                    other,
                    CatalogCultivationCase(
                        tenant_id=other,
                        cultivation_case_id=CatalogCultivationCaseId("ccc_wrong"),
                        proposal_id=wrong_proposal.proposal_id,
                        approval_id=ApprovalId("apr_wrong"),
                        cluster_id=wrong_proposal.cluster_id,
                        policy_version_id=wrong_policy.policy_version_id,
                        facts_hash=wrong_proposal.facts_hash,
                        evidence_refs=("evidence",),
                        state="queued",
                        queued_at=NOW,
                    ),
                )
            with pytest.raises(TenantIsolationViolation):
                await uow.policies.list_versions(
                    tenant,
                    limit=10,
                    cursor=CatalogPageCursor(
                        tenant_id=other,
                        stream="policies",
                        position_at=NOW,
                        entity_id="cpv_other",
                    ),
                )
        assert statements == 0
    finally:
        event.remove(catalog_engine.sync_engine, "before_cursor_execute", counted)
        await _cleanup(catalog_engine, values["tenant"])


async def test_stable_bounded_descending_and_reconciliation_pagination(
    catalog_engine: AsyncEngine,
) -> None:
    values = await _seed_parents(catalog_engine, "paging")
    tenant = TenantId(values["tenant"])
    factory = async_sessionmaker(catalog_engine, expire_on_commit=False)
    try:
        policies = (
            _policy(values, "paging_a", at=NOW),
            _policy(values, "paging_b", at=NOW),
            _policy(values, "paging_c", at=NOW + timedelta(seconds=1)),
        )
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            for policy in policies:
                await uow.policies.add(tenant, policy)
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            first = await uow.policies.list_versions(tenant, limit=2)
            second = await uow.policies.list_versions(
                tenant, limit=2, cursor=first.next_cursor
            )
            ascending = await uow.policies.list_pending_reconciliation(tenant, limit=3)
        assert [item.policy_version_id for item in first.items] == [
            "cpv_paging_c",
            "cpv_paging_b",
        ]
        assert [item.policy_version_id for item in second.items] == ["cpv_paging_a"]
        assert [item.policy_version_id for item in ascending.items] == [
            "cpv_paging_a",
            "cpv_paging_b",
            "cpv_paging_c",
        ]
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            with pytest.raises(ValueError, match="1..200"):
                await uow.policies.list_versions(tenant, limit=201)
    finally:
        await _cleanup(catalog_engine, values["tenant"])


async def test_every_catalog_stream_uses_complete_stable_composite_pagination(
    catalog_engine: AsyncEngine,
) -> None:
    values = await _seed_parents(catalog_engine, "all_pages")
    tenant = TenantId(values["tenant"])
    factory = async_sessionmaker(catalog_engine, expire_on_commit=False)
    try:
        active = await _active_policy(catalog_engine, factory, values, "page_active")
        pending_policies = tuple(
            _policy(values, f"page_{index}", at=NOW) for index in range(4)
        )
        evaluations = tuple(
            _evaluation(values, active, f"{marker}_page") for marker in "abcd"
        )
        proposals = tuple(
            CatalogProductProposal(
                tenant_id=tenant,
                proposal_id=CatalogProductProposalId(f"cpr_page_{marker}"),
                evaluation_id=evaluation.evaluation_id,
                cluster_id=evaluation.cluster_id,
                policy_version_id=evaluation.policy_version_id,
                facts_hash=evaluation.facts_hash,
                owner_employee=EmployeeId(values["owner"]),
                proposed_by_run=RunId(values["run"]),
                approval_id=None,
                approval_request_hash=None,
                state=CatalogProductProposalState.AWAITING_APPROVAL_SUBMISSION,
                created_at=NOW + timedelta(seconds=2),
                updated_at=NOW + timedelta(seconds=2),
            )
            for marker, evaluation in zip("abcd", evaluations, strict=True)
        )
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            for policy in pending_policies:
                await uow.policies.add(tenant, policy)
            for evaluation, proposal in zip(evaluations, proposals, strict=True):
                await uow.evaluations.add(tenant, evaluation)
                await uow.proposals.add(tenant, proposal)

        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            policy_1 = await uow.policies.list_versions(tenant, limit=2)
            policy_2 = await uow.policies.list_versions(
                tenant, limit=2, cursor=policy_1.next_cursor
            )
            policy_3 = await uow.policies.list_versions(
                tenant, limit=2, cursor=policy_2.next_cursor
            )
            pending_1 = await uow.policies.list_pending_reconciliation(tenant, limit=2)
            pending_2 = await uow.policies.list_pending_reconciliation(
                tenant, limit=2, cursor=pending_1.next_cursor
            )
            evaluation_1 = await uow.evaluations.list_evaluations(tenant, limit=2)
            evaluation_2 = await uow.evaluations.list_evaluations(
                tenant, limit=2, cursor=evaluation_1.next_cursor
            )
            awaiting_1 = await uow.proposals.list_awaiting_reconciliation(
                tenant, limit=2
            )
            awaiting_2 = await uow.proposals.list_awaiting_reconciliation(
                tenant, limit=2, cursor=awaiting_1.next_cursor
            )

        assert policy_1.next_cursor is not None
        assert policy_2.next_cursor is not None
        assert policy_3.next_cursor is None
        assert [
            str(item.policy_version_id)
            for item in (*policy_1.items, *policy_2.items, *policy_3.items)
        ] == [
            "cpv_page_active",
            "cpv_page_3",
            "cpv_page_2",
            "cpv_page_1",
            "cpv_page_0",
        ]
        assert pending_1.next_cursor is not None
        assert pending_2.next_cursor is None
        assert [
            str(item.policy_version_id) for item in (*pending_1.items, *pending_2.items)
        ] == ["cpv_page_0", "cpv_page_1", "cpv_page_2", "cpv_page_3"]
        assert evaluation_1.next_cursor is not None
        assert evaluation_2.next_cursor is None
        assert [
            str(item.evaluation_id)
            for item in (*evaluation_1.items, *evaluation_2.items)
        ] == ["cpe_d_page", "cpe_c_page", "cpe_b_page", "cpe_a_page"]
        assert awaiting_1.next_cursor is not None
        assert awaiting_2.next_cursor is None
        assert [
            str(item.proposal_id) for item in (*awaiting_1.items, *awaiting_2.items)
        ] == ["cpr_page_a", "cpr_page_b", "cpr_page_c", "cpr_page_d"]

        queued_proposals: list[CatalogProductProposal] = []
        for marker, proposal in zip("abcd", proposals, strict=True):
            approval_id = f"apr_page_{marker}"
            await _approval(
                catalog_engine,
                values,
                approval_id,
                policy=active,
                proposal=proposal,
            )
            pending = replace(
                proposal,
                approval_id=ApprovalId(approval_id),
                approval_request_hash=HASH_C,
                state=CatalogProductProposalState.PENDING_REVIEW,
                updated_at=NOW + timedelta(seconds=3),
            )
            queued = replace(
                pending,
                state=CatalogProductProposalState.CULTIVATION_QUEUED,
                updated_at=NOW + timedelta(seconds=4),
            )
            async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
                await uow.proposals.update(tenant, pending)
            async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
                queued = await uow.proposals.update(tenant, queued)
            queued_proposals.append(queued)

        cultivation_cases = tuple(
            CatalogCultivationCase(
                tenant_id=tenant,
                cultivation_case_id=CatalogCultivationCaseId(f"ccc_page_{marker}"),
                proposal_id=proposal.proposal_id,
                approval_id=proposal.approval_id,
                cluster_id=proposal.cluster_id,
                policy_version_id=proposal.policy_version_id,
                facts_hash=proposal.facts_hash,
                evidence_refs=(f"artifact-{marker}",),
                state="queued",
                queued_at=NOW + timedelta(seconds=5),
            )
            for marker, proposal in zip("abcd", queued_proposals, strict=True)
        )
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            for cultivation_case in cultivation_cases:
                await uow.cultivation_cases.add(tenant, cultivation_case)
            proposal_1 = await uow.proposals.list_proposals(tenant, limit=2)
            proposal_2 = await uow.proposals.list_proposals(
                tenant, limit=2, cursor=proposal_1.next_cursor
            )
            case_1 = await uow.cultivation_cases.list_cases(tenant, limit=2)
            case_2 = await uow.cultivation_cases.list_cases(
                tenant, limit=2, cursor=case_1.next_cursor
            )

        assert proposal_1.next_cursor is not None
        assert proposal_2.next_cursor is None
        assert [
            str(item.proposal_id) for item in (*proposal_1.items, *proposal_2.items)
        ] == ["cpr_page_d", "cpr_page_c", "cpr_page_b", "cpr_page_a"]
        assert case_1.next_cursor is not None
        assert case_2.next_cursor is None
        assert [
            str(item.cultivation_case_id) for item in (*case_1.items, *case_2.items)
        ] == ["ccc_page_d", "ccc_page_c", "ccc_page_b", "ccc_page_a"]
    finally:
        await _cleanup(catalog_engine, values["tenant"])


async def test_every_catalog_list_rejects_strict_limits_and_mismatched_cursors(
    catalog_engine: AsyncEngine,
) -> None:
    tenant = TenantId("tn_catalog_list_guards")
    other = TenantId("tn_catalog_list_other")
    factory = async_sessionmaker(catalog_engine, expire_on_commit=False)
    async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
        entrances = (
            (
                "policies",
                lambda limit, cursor=None: uow.policies.list_versions(
                    tenant, limit=limit, cursor=cursor
                ),
            ),
            (
                "pending_policies",
                lambda limit, cursor=None: uow.policies.list_pending_reconciliation(
                    tenant, limit=limit, cursor=cursor
                ),
            ),
            (
                "evaluations",
                lambda limit, cursor=None: uow.evaluations.list_evaluations(
                    tenant, limit=limit, cursor=cursor
                ),
            ),
            (
                "proposals",
                lambda limit, cursor=None: uow.proposals.list_proposals(
                    tenant, limit=limit, cursor=cursor
                ),
            ),
            (
                "awaiting_proposals",
                lambda limit, cursor=None: uow.proposals.list_awaiting_reconciliation(
                    tenant, limit=limit, cursor=cursor
                ),
            ),
            (
                "cultivation_cases",
                lambda limit, cursor=None: uow.cultivation_cases.list_cases(
                    tenant, limit=limit, cursor=cursor
                ),
            ),
        )
        for stream, entrance in entrances:
            for invalid_limit in (True, 0, 201):
                with pytest.raises(ValueError, match="1..200"):
                    await entrance(invalid_limit)
            wrong_stream = "evaluations" if stream == "policies" else "policies"
            with pytest.raises(ValueError, match="查询流"):
                await entrance(
                    1,
                    CatalogPageCursor(
                        tenant_id=tenant,
                        stream=wrong_stream,
                        position_at=NOW,
                        entity_id="cursor_entity",
                    ),
                )
            with pytest.raises(TenantIsolationViolation, match="不可跨租户"):
                await entrance(
                    1,
                    CatalogPageCursor(
                        tenant_id=other,
                        stream=stream,
                        position_at=NOW,
                        entity_id="cursor_entity",
                    ),
                )
        with pytest.raises(ValueError, match="1..200"):
            await uow.cultivation_cases.list_cases(tenant, limit=-1)


async def test_concurrent_evaluation_proposal_and_cultivation_return_one_winner(
    catalog_engine: AsyncEngine,
) -> None:
    values = await _seed_parents(catalog_engine, "race")
    tenant = TenantId(values["tenant"])
    factory = async_sessionmaker(catalog_engine, expire_on_commit=False)
    try:
        policy = await _active_policy(catalog_engine, factory, values, "race")
        evaluation_a = _evaluation(values, policy, "f")
        evaluation_b = replace(
            evaluation_a, evaluation_id=CatalogProposalEvaluationId("cpe_race_b")
        )

        async def add_evaluation(
            item: CatalogProposalEvaluation,
        ) -> CatalogProposalEvaluation:
            async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
                return await uow.evaluations.add(tenant, item)

        evaluation_results = await asyncio.gather(
            add_evaluation(evaluation_a), add_evaluation(evaluation_b)
        )
        assert (
            evaluation_results[0].evaluation_id == evaluation_results[1].evaluation_id
        )
        winner_evaluation = evaluation_results[0]
        proposal_a = CatalogProductProposal(
            tenant_id=tenant,
            proposal_id=CatalogProductProposalId("cpr_race_a"),
            evaluation_id=winner_evaluation.evaluation_id,
            cluster_id=winner_evaluation.cluster_id,
            policy_version_id=policy.policy_version_id,
            facts_hash=winner_evaluation.facts_hash,
            owner_employee=EmployeeId(values["owner"]),
            proposed_by_run=RunId(values["run"]),
            approval_id=None,
            approval_request_hash=None,
            state=CatalogProductProposalState.AWAITING_APPROVAL_SUBMISSION,
            created_at=NOW,
            updated_at=NOW,
        )
        proposal_b = replace(
            proposal_a, proposal_id=CatalogProductProposalId("cpr_race_b")
        )

        async def add_proposal(item: CatalogProductProposal) -> CatalogProductProposal:
            async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
                return await uow.proposals.add(tenant, item)

        proposal_results = await asyncio.gather(
            add_proposal(proposal_a), add_proposal(proposal_b)
        )
        assert proposal_results[0].proposal_id == proposal_results[1].proposal_id
        proposal = proposal_results[0]
        await _approval(
            catalog_engine, values, "apr_cult_race", policy=policy, proposal=proposal
        )
        pending = replace(
            proposal,
            approval_id=ApprovalId("apr_cult_race"),
            approval_request_hash=HASH_C,
            state=CatalogProductProposalState.PENDING_REVIEW,
            updated_at=NOW + timedelta(seconds=2),
        )
        queued = replace(
            pending,
            state=CatalogProductProposalState.CULTIVATION_QUEUED,
            updated_at=NOW + timedelta(seconds=3),
        )
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            await uow.proposals.update(tenant, pending)
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            proposal = await uow.proposals.update(tenant, queued)
        case_a = CatalogCultivationCase(
            tenant_id=tenant,
            cultivation_case_id=CatalogCultivationCaseId("ccc_race_a"),
            proposal_id=proposal.proposal_id,
            approval_id=ApprovalId("apr_cult_race"),
            cluster_id=proposal.cluster_id,
            policy_version_id=proposal.policy_version_id,
            facts_hash=proposal.facts_hash,
            evidence_refs=("evidence-1",),
            state="queued",
            queued_at=NOW + timedelta(seconds=4),
        )
        case_b = replace(
            case_a, cultivation_case_id=CatalogCultivationCaseId("ccc_race_b")
        )

        async def add_case(item: CatalogCultivationCase) -> CatalogCultivationCase:
            async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
                return await uow.cultivation_cases.add(tenant, item)

        case_results = await asyncio.gather(add_case(case_a), add_case(case_b))
        assert (
            case_results[0].cultivation_case_id == case_results[1].cultivation_case_id
        )
        async with catalog_engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(func.count())
                    .select_from(CatalogProposalEvaluationRow)
                    .where(CatalogProposalEvaluationRow.tenant_id == str(tenant))
                )
                == 1
            )
            assert (
                await connection.scalar(
                    select(func.count())
                    .select_from(CatalogProductProposalRow)
                    .where(CatalogProductProposalRow.tenant_id == str(tenant))
                )
                == 1
            )
            assert (
                await connection.scalar(
                    select(func.count())
                    .select_from(CatalogCultivationCaseRow)
                    .where(CatalogCultivationCaseRow.tenant_id == str(tenant))
                )
                == 1
            )
    finally:
        await _cleanup(catalog_engine, values["tenant"])


async def test_concurrent_approval_binding_unique_winner_never_aliases_subject(
    catalog_engine: AsyncEngine,
) -> None:
    values = await _seed_parents(catalog_engine, "binding")
    tenant = TenantId(values["tenant"])
    factory = async_sessionmaker(catalog_engine, expire_on_commit=False)
    try:
        first = _policy(values, "binding_a")
        second = _policy(values, "binding_b", at=NOW + timedelta(seconds=1))
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            await uow.policies.add(tenant, first)
            await uow.policies.add(tenant, second)
        await _approval(
            catalog_engine, values, "apr_shared_binding", policy=first, state="pending"
        )
        async with catalog_engine.begin() as connection:
            await connection.execute(
                text(
                    "ALTER TABLE catalog_proposal_policy_versions "
                    "DISABLE TRIGGER trg_catalog_policy_version_guard"
                )
            )
        try:

            async def bind(item: CatalogProposalPolicyVersion) -> object:
                candidate = replace(item, approval_id=ApprovalId("apr_shared_binding"))
                try:
                    async with SqlAlchemyCatalogProductsUnitOfWork(
                        factory, tenant
                    ) as uow:
                        return await uow.policies.update(tenant, candidate)
                except IdempotencyConflict:
                    return "conflict"

            results = await asyncio.gather(bind(first), bind(second))
            assert sum(result == "conflict" for result in results) == 1
        finally:
            async with catalog_engine.begin() as connection:
                await connection.execute(
                    text(
                        "ALTER TABLE catalog_proposal_policy_versions "
                        "ENABLE TRIGGER trg_catalog_policy_version_guard"
                    )
                )
    finally:
        await _cleanup(catalog_engine, values["tenant"])


async def test_concurrent_proposal_approval_binding_unique_winner_never_aliases_subject(
    catalog_engine: AsyncEngine,
) -> None:
    values = await _seed_parents(catalog_engine, "proposal_binding")
    tenant = TenantId(values["tenant"])
    factory = async_sessionmaker(catalog_engine, expire_on_commit=False)
    try:
        policy = await _active_policy(
            catalog_engine, factory, values, "proposal_binding"
        )
        evaluations = (
            _evaluation(values, policy, "a"),
            _evaluation(values, policy, "b"),
        )
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            for evaluation in evaluations:
                await uow.evaluations.add(tenant, evaluation)
        proposals = tuple(
            CatalogProductProposal(
                tenant_id=tenant,
                proposal_id=CatalogProductProposalId(f"cpr_binding_{index}"),
                evaluation_id=evaluation.evaluation_id,
                cluster_id=evaluation.cluster_id,
                policy_version_id=evaluation.policy_version_id,
                facts_hash=evaluation.facts_hash,
                owner_employee=EmployeeId(values["owner"]),
                proposed_by_run=RunId(values["run"]),
                approval_id=None,
                approval_request_hash=None,
                state=CatalogProductProposalState.AWAITING_APPROVAL_SUBMISSION,
                created_at=NOW,
                updated_at=NOW,
            )
            for index, evaluation in enumerate(evaluations)
        )
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            for proposal in proposals:
                await uow.proposals.add(tenant, proposal)
        await _approval(
            catalog_engine,
            values,
            "apr_shared_proposal",
            policy=policy,
            proposal=proposals[0],
            state="pending",
        )
        async with catalog_engine.begin() as connection:
            await connection.execute(
                text(
                    "ALTER TABLE catalog_product_proposals "
                    "DISABLE TRIGGER trg_catalog_product_proposal_guard"
                )
            )
        try:

            async def bind(item: CatalogProductProposal) -> object:
                candidate = replace(
                    item,
                    approval_id=ApprovalId("apr_shared_proposal"),
                    approval_request_hash=HASH_C,
                    state=CatalogProductProposalState.PENDING_REVIEW,
                    updated_at=NOW + timedelta(seconds=1),
                )
                try:
                    async with SqlAlchemyCatalogProductsUnitOfWork(
                        factory, tenant
                    ) as uow:
                        return await uow.proposals.update(tenant, candidate)
                except IdempotencyConflict:
                    return "conflict"

            results = await asyncio.gather(*(bind(item) for item in proposals))
            assert sum(result == "conflict" for result in results) == 1
        finally:
            async with catalog_engine.begin() as connection:
                await connection.execute(
                    text(
                        "ALTER TABLE catalog_product_proposals "
                        "ENABLE TRIGGER trg_catalog_product_proposal_guard"
                    )
                )
    finally:
        await _cleanup(catalog_engine, values["tenant"])


async def test_all_lifecycle_states_and_all_four_cursor_orders_round_trip(
    catalog_engine: AsyncEngine,
) -> None:
    """固定状态 hydration 或省略稳定 ID tie-break 会破坏完整遍历。"""

    values = await _seed_parents(catalog_engine, "all_states")
    tenant = TenantId(values["tenant"])
    factory = async_sessionmaker(catalog_engine, expire_on_commit=False)
    try:
        pending_policy = _policy(values, "state_pending", at=NOW)
        superseded_seed = _policy(
            values, "state_superseded", at=NOW + timedelta(seconds=1)
        )
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            await uow.policies.add(tenant, pending_policy)
            await uow.policies.add(tenant, superseded_seed)
        await _approval(
            catalog_engine,
            values,
            "apr_state_super",
            policy=superseded_seed,
        )
        superseded_active = replace(
            superseded_seed,
            approval_id=ApprovalId("apr_state_super"),
            state=CatalogProposalPolicyState.ACTIVE,
            activated_at=NOW + timedelta(seconds=2),
        )
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            await uow.policies.update(tenant, superseded_active)
        superseded = replace(
            superseded_active,
            state=CatalogProposalPolicyState.SUPERSEDED,
            terminal_at=NOW + timedelta(seconds=3),
        )
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            await uow.policies.update(tenant, superseded)

        terminal_specs = (
            (CatalogProposalPolicyState.REJECTED, "rejected"),
            (CatalogProposalPolicyState.EXPIRED, "expired"),
            (CatalogProposalPolicyState.STALE, "pending"),
        )
        terminal_policies: list[CatalogProposalPolicyVersion] = []
        for offset, (target, approval_state) in enumerate(terminal_specs, start=4):
            candidate = _policy(
                values,
                f"state_{target.value}",
                at=NOW + timedelta(seconds=offset),
            )
            async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
                await uow.policies.add(tenant, candidate)
            approval_id = f"apr_state_{target.value}"
            await _approval(
                catalog_engine,
                values,
                approval_id,
                policy=candidate,
                state=approval_state,
            )
            terminal = replace(
                candidate,
                approval_id=ApprovalId(approval_id),
                state=target,
                terminal_at=NOW + timedelta(seconds=offset + 1),
            )
            async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
                terminal = await uow.policies.update(tenant, terminal)
            terminal_policies.append(terminal)

        active_seed = _policy(values, "state_active", at=NOW + timedelta(seconds=8))
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            await uow.policies.add(tenant, active_seed)
        await _approval(catalog_engine, values, "apr_state_active", policy=active_seed)
        active = replace(
            active_seed,
            approval_id=ApprovalId("apr_state_active"),
            state=CatalogProposalPolicyState.ACTIVE,
            activated_at=NOW + timedelta(seconds=9),
        )
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            active = await uow.policies.update(tenant, active)
            policy_page = await uow.policies.list_versions(tenant, limit=20)
        assert {item.state for item in policy_page.items} == set(
            CatalogProposalPolicyState
        )

        markers = ("0", "1", "2", "a", "b", "c", "d", "e", "f")
        evaluations = tuple(
            replace(
                _evaluation(values, active, marker),
                created_at=NOW + timedelta(minutes=1, seconds=index // 3),
            )
            for index, marker in enumerate(markers)
        )
        proposals = tuple(
            CatalogProductProposal(
                tenant_id=tenant,
                proposal_id=CatalogProductProposalId(f"cpr_state_{marker}"),
                evaluation_id=evaluation.evaluation_id,
                cluster_id=evaluation.cluster_id,
                policy_version_id=evaluation.policy_version_id,
                facts_hash=evaluation.facts_hash,
                owner_employee=EmployeeId(values["owner"]),
                proposed_by_run=RunId(values["run"]),
                approval_id=None,
                approval_request_hash=None,
                state=CatalogProductProposalState.AWAITING_APPROVAL_SUBMISSION,
                created_at=evaluation.created_at,
                updated_at=evaluation.created_at,
            )
            for marker, evaluation in zip(markers, evaluations, strict=True)
        )
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            for evaluation, proposal in zip(evaluations, proposals, strict=True):
                await uow.evaluations.add(tenant, evaluation)
                await uow.proposals.add(tenant, proposal)

        targets = (
            CatalogProductProposalState.AWAITING_APPROVAL_SUBMISSION,
            CatalogProductProposalState.AWAITING_APPROVAL_SUBMISSION,
            CatalogProductProposalState.PENDING_REVIEW,
            CatalogProductProposalState.CULTIVATION_QUEUED,
            CatalogProductProposalState.CULTIVATION_QUEUED,
            CatalogProductProposalState.CULTIVATION_QUEUED,
            CatalogProductProposalState.REJECTED,
            CatalogProductProposalState.EXPIRED,
            CatalogProductProposalState.STALE,
        )
        final_proposals: list[CatalogProductProposal] = []
        queued: list[CatalogProductProposal] = []
        approval_state_for = {
            CatalogProductProposalState.PENDING_REVIEW: "pending",
            CatalogProductProposalState.CULTIVATION_QUEUED: "approved",
            CatalogProductProposalState.REJECTED: "rejected",
            CatalogProductProposalState.EXPIRED: "expired",
            CatalogProductProposalState.STALE: "pending",
        }
        for index, (proposal, target) in enumerate(
            zip(proposals, targets, strict=True)
        ):
            if target is CatalogProductProposalState.AWAITING_APPROVAL_SUBMISSION:
                final_proposals.append(proposal)
                continue
            approval_id = f"apr_prop_state_{index}"
            await _approval(
                catalog_engine,
                values,
                approval_id,
                policy=active,
                proposal=proposal,
                state=approval_state_for[target],
            )
            pending = replace(
                proposal,
                approval_id=ApprovalId(approval_id),
                approval_request_hash=HASH_C,
                state=CatalogProductProposalState.PENDING_REVIEW,
                updated_at=NOW + timedelta(minutes=2, seconds=index),
            )
            async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
                pending = await uow.proposals.update(tenant, pending)
            final = pending
            if target is not CatalogProductProposalState.PENDING_REVIEW:
                final = replace(
                    pending,
                    state=target,
                    updated_at=NOW + timedelta(minutes=3, seconds=index),
                )
                async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
                    final = await uow.proposals.update(tenant, final)
            final_proposals.append(final)
            if target is CatalogProductProposalState.CULTIVATION_QUEUED:
                queued.append(final)

        cases = tuple(
            CatalogCultivationCase(
                tenant_id=tenant,
                cultivation_case_id=CatalogCultivationCaseId(f"ccc_state_{index}"),
                proposal_id=proposal.proposal_id,
                approval_id=proposal.approval_id,
                cluster_id=proposal.cluster_id,
                policy_version_id=proposal.policy_version_id,
                facts_hash=proposal.facts_hash,
                evidence_refs=(f"evidence-{index}",),
                state="queued",
                queued_at=NOW + timedelta(minutes=4),
            )
            for index, proposal in enumerate(queued)
            if proposal.approval_id is not None
        )
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            for cultivation_case in cases:
                await uow.cultivation_cases.add(tenant, cultivation_case)

        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            evaluation_first = await uow.evaluations.list_evaluations(tenant, limit=4)
            evaluation_second = await uow.evaluations.list_evaluations(
                tenant, limit=20, cursor=evaluation_first.next_cursor
            )
            proposal_first = await uow.proposals.list_proposals(tenant, limit=4)
            proposal_second = await uow.proposals.list_proposals(
                tenant, limit=20, cursor=proposal_first.next_cursor
            )
            awaiting = await uow.proposals.list_awaiting_reconciliation(
                tenant, limit=20
            )
            case_first = await uow.cultivation_cases.list_cases(tenant, limit=2)
            case_second = await uow.cultivation_cases.list_cases(
                tenant, limit=2, cursor=case_first.next_cursor
            )

        evaluation_ids = [
            str(item.evaluation_id)
            for item in (*evaluation_first.items, *evaluation_second.items)
        ]
        assert evaluation_ids == [
            "cpe_f",
            "cpe_e",
            "cpe_d",
            "cpe_c",
            "cpe_b",
            "cpe_a",
            "cpe_2",
            "cpe_1",
            "cpe_0",
        ]
        assert {
            item.state for item in (*proposal_first.items, *proposal_second.items)
        } == set(CatalogProductProposalState)
        assert [str(item.proposal_id) for item in awaiting.items] == [
            "cpr_state_0",
            "cpr_state_1",
        ]
        assert [
            str(item.cultivation_case_id)
            for item in (*case_first.items, *case_second.items)
        ] == ["ccc_state_2", "ccc_state_1", "ccc_state_0"]
    finally:
        await _cleanup(catalog_engine, values["tenant"])


async def test_canonical_recovery_rejects_mismatched_immutable_payloads(
    catalog_engine: AsyncEngine,
) -> None:
    values = await _seed_parents(catalog_engine, "mismatch")
    tenant = TenantId(values["tenant"])
    factory = async_sessionmaker(catalog_engine, expire_on_commit=False)
    try:
        policy = await _active_policy(catalog_engine, factory, values, "mismatch")
        evaluation = _evaluation(values, policy, "a")
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            await uow.evaluations.add(tenant, evaluation)
        conflicting_facts = evaluation.facts.model_copy(
            update={"cluster_category": "different-category"}
        )
        conflicting = replace(
            evaluation,
            evaluation_id=CatalogProposalEvaluationId("cpe_mismatch_other"),
            facts=conflicting_facts,
        )
        with pytest.raises(IdempotencyConflict, match="不可变快照"):
            async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
                await uow.evaluations.add(tenant, conflicting)

        proposal = CatalogProductProposal(
            tenant_id=tenant,
            proposal_id=CatalogProductProposalId("cpr_mismatch"),
            evaluation_id=evaluation.evaluation_id,
            cluster_id=evaluation.cluster_id,
            policy_version_id=evaluation.policy_version_id,
            facts_hash=evaluation.facts_hash,
            owner_employee=EmployeeId(values["owner"]),
            proposed_by_run=RunId(values["run"]),
            approval_id=None,
            approval_request_hash=None,
            state=CatalogProductProposalState.AWAITING_APPROVAL_SUBMISSION,
            created_at=NOW,
            updated_at=NOW,
        )
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            await uow.proposals.add(tenant, proposal)
        with pytest.raises(IdempotencyConflict, match="不可变 subject"):
            async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
                await uow.proposals.add(
                    tenant,
                    replace(
                        proposal,
                        proposal_id=CatalogProductProposalId("cpr_mismatch_other"),
                        owner_employee=EmployeeId(values["reviewer"]),
                    ),
                )

        await _approval(
            catalog_engine,
            values,
            "apr_mismatch_case",
            policy=policy,
            proposal=proposal,
        )
        pending = replace(
            proposal,
            approval_id=ApprovalId("apr_mismatch_case"),
            approval_request_hash=HASH_C,
            state=CatalogProductProposalState.PENDING_REVIEW,
            updated_at=NOW + timedelta(seconds=1),
        )
        queued = replace(
            pending,
            state=CatalogProductProposalState.CULTIVATION_QUEUED,
            updated_at=NOW + timedelta(seconds=2),
        )
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            await uow.proposals.update(tenant, pending)
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            await uow.proposals.update(tenant, queued)
        cultivation_case = CatalogCultivationCase(
            tenant_id=tenant,
            cultivation_case_id=CatalogCultivationCaseId("ccc_mismatch"),
            proposal_id=proposal.proposal_id,
            approval_id=ApprovalId("apr_mismatch_case"),
            cluster_id=proposal.cluster_id,
            policy_version_id=proposal.policy_version_id,
            facts_hash=proposal.facts_hash,
            evidence_refs=("evidence-a",),
            state="queued",
            queued_at=NOW + timedelta(seconds=3),
        )
        async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
            await uow.cultivation_cases.add(tenant, cultivation_case)
        with pytest.raises(IdempotencyConflict, match="不可变 subject"):
            async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
                await uow.cultivation_cases.add(
                    tenant,
                    replace(
                        cultivation_case,
                        cultivation_case_id=CatalogCultivationCaseId(
                            "ccc_mismatch_other"
                        ),
                        evidence_refs=("evidence-b",),
                    ),
                )
    finally:
        await _cleanup(catalog_engine, values["tenant"])
