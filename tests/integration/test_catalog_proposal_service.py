"""Catalog Product Proposal 服务的真实 PostgreSQL 原子性测试。"""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from domains.products.catalog_rules import catalog_policy_content_hash
from domains.products.catalog_service_impl import CatalogProposalServiceImpl
from domains.products.errors import CatalogProposalApprovalConflictError
from domains.products.permissions import (
    Phase2ProductAuthorizer,
    ProductActor,
    ProductRole,
)
from domains.products.schemas import (
    CatalogApprovalDecisionInput,
    CatalogClusterFactsInput,
    CatalogEvidenceSummaryInput,
    CatalogProposalPolicyContent,
)
from domains.products.service import (
    CatalogProposalPolicyState,
    CatalogProposalPolicyVersion,
    catalog_policy_creation_request_hash,
)
from infra.db.catalog_products_uow import SqlAlchemyCatalogProductsUnitOfWork
from infra.db.tables import (
    CatalogCultivationCaseRow,
    CatalogProductProposalRow,
    CatalogProposalEvaluationRow,
    OutboxEventRow,
    ProductRow,
    SourcingCaseRow,
)
from shared.errors import TransientError
from shared.schemas.identifiers import (
    ApprovalId,
    CatalogProductProposalId,
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


@pytest_asyncio.fixture
async def catalog_proposal_engine(db_url: str) -> AsyncEngine:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _system(tenant: TenantId) -> ProductActor:
    return ProductActor("system:catalog-proposal-workflow", ProductRole.SYSTEM, tenant)


def _reader(tenant: TenantId, owner: str) -> ProductActor:
    return ProductActor(owner, ProductRole.PRODUCT, tenant)


def _content() -> CatalogProposalPolicyContent:
    return CatalogProposalPolicyContent(
        minimum_distinct_accounts=3,
        minimum_recurring_accounts=None,
        minimum_distinct_countries=None,
        minimum_quantity_unit_accounts=None,
        require_unified_unit=False,
    )


def _facts(
    values: dict[str, str], *, facts_hash: str = HASH_A, accounts: int = 3
) -> CatalogClusterFactsInput:
    return CatalogClusterFactsInput(
        tenant_id=TenantId(values["tenant"]),
        cluster_id=NeedClusterId(values["cluster"]),
        cluster_category="three_wheelers",
        member_need_ids=tuple(f"need_{index}" for index in range(1, accounts + 1)),
        distinct_account_ids=tuple(f"acc_{index}" for index in range(1, accounts + 1)),
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
        evidence_summaries=(
            CatalogEvidenceSummaryInput(
                source_type="conversation",
                source_id=values["message"],
                extracted_by="employee",
                confirmed_by=EmployeeId(values["owner"]),
                confirmed_at=NOW - timedelta(hours=2),
                observed_at=NOW - timedelta(hours=2),
                content_hash=HASH_B,
            ),
        ),
        display_codes=("country_unknown",),
        facts_observed_at=NOW - timedelta(hours=1),
        facts_hash=facts_hash,
    )


async def _seed_parents(engine: AsyncEngine, marker: str) -> dict[str, str]:
    values = {
        "tenant": f"tn_cps_{marker}",
        "owner": f"emp_cps_owner_{marker}",
        "reviewer": f"emp_cps_review_{marker}",
        "cluster": f"ncl_cps_{marker}",
        "run": f"run_cps_{marker}",
        "other_run": f"run_cps_other_{marker}",
        "message": f"msg_cps_{marker}",
    }
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO employees (employee_id,tenant_id,name,role) VALUES "
                "(:owner,:tenant,'Owner','product'),"
                "(:reviewer,:tenant,'Reviewer','boss')"
            ),
            values,
        )
        await connection.execute(
            text(
                "INSERT INTO need_clusters "
                "(tenant_id,cluster_id,category,keywords,countries,created_at,updated_at) "
                "VALUES (:tenant,:cluster,'three_wheelers','[]'::jsonb,"
                "'[]'::jsonb,:now,:now)"
            ),
            values | {"now": NOW},
        )
        await connection.execute(
            text(
                "INSERT INTO workflow_runs "
                "(run_id,tenant_id,workflow_type,workflow_version,subject_ref,current_step,"
                "status,context,idempotency_key) VALUES "
                "(:run,:tenant,'catalog_cluster_evaluation',1,:cluster,'evaluate',"
                "'running','{}'::jsonb,:key),"
                "(:other_run,:tenant,'catalog_cluster_evaluation',1,:cluster,'evaluate',"
                "'running','{}'::jsonb,:other_key)"
            ),
            values
            | {
                "key": f"catalog-evaluation:{marker}",
                "other_key": f"catalog-evaluation-other:{marker}",
            },
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
                text(f"DELETE FROM {table} WHERE tenant_id=:tenant"),
                {"tenant": tenant},
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
        for table in (
            "outbox_events",
            "workflow_runs",
            "need_clusters",
            "employees",
        ):
            await connection.execute(
                text(f"DELETE FROM {table} WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )


async def _insert_policy_approval(
    engine: AsyncEngine,
    values: dict[str, str],
    policy: CatalogProposalPolicyVersion,
) -> ApprovalId:
    approval_id = ApprovalId(f"apr_cps_policy_{values['tenant'][7:]}")
    payload = {
        "schema_version": "catalog-policy-v1",
        "tenant_id": values["tenant"],
        "approval_type": "catalog_proposal_policy_change",
        "policy_version_id": str(policy.policy_version_id),
        "content_hash": policy.content_hash,
        "request_hash": policy.creation_request_hash,
    }
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO approval_packages "
                "(tenant_id,approval_id,approval_type,title,proposed_change,reason,"
                "blast_radius,created_at,expires_at,state,proposed_by_employee,"
                "evidence_refs,change_set_ref,owner_employee,contract_namespace,"
                "request_hash,expires_at_limit,decided_at,decided_by) VALUES "
                "(:tenant,:approval,'catalog_proposal_policy_change','Policy',"
                "CAST(:payload AS jsonb),'internal','{}'::jsonb,:now,:expires,'approved',"
                ":owner,'[]'::jsonb,:change_set,:owner,'catalog-policy-v1',"
                ":request_hash,:expires,:now,:reviewer)"
            ),
            values
            | {
                "approval": str(approval_id),
                "payload": json.dumps(payload),
                "now": NOW,
                "expires": NOW + timedelta(days=7),
                "change_set": f"catalog-policy:{policy.policy_version_id}:{policy.content_hash}",
                "request_hash": policy.creation_request_hash,
            },
        )
    return approval_id


async def _active_policy(
    engine: AsyncEngine,
    factory: async_sessionmaker,
    values: dict[str, str],
) -> CatalogProposalPolicyVersion:
    tenant = TenantId(values["tenant"])
    content = _content()
    policy_id = CatalogProposalPolicyVersionId(f"cpv_cps_{values['tenant'][7:]}")
    policy = CatalogProposalPolicyVersion(
        tenant_id=tenant,
        policy_version_id=policy_id,
        content=content,
        content_hash=catalog_policy_content_hash(content),
        base_active_version_id=None,
        proposed_by=EmployeeId(values["owner"]),
        creation_key=f"create-{policy_id}",
        creation_request_hash=catalog_policy_creation_request_hash(
            content, EmployeeId(values["owner"]), None
        ),
        approval_id=None,
        state=CatalogProposalPolicyState.PENDING_APPROVAL,
        created_at=NOW - timedelta(days=1),
    )
    async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
        await uow.policies.add(tenant, policy)
    approval_id = await _insert_policy_approval(engine, values, policy)
    active = replace(
        policy,
        approval_id=approval_id,
        state=CatalogProposalPolicyState.ACTIVE,
        activated_at=NOW - timedelta(hours=23),
    )
    async with SqlAlchemyCatalogProductsUnitOfWork(factory, tenant) as uow:
        return await uow.policies.update(tenant, active)


async def _insert_cultivation_approval(
    engine: AsyncEngine,
    values: dict[str, str],
    proposal_id: CatalogProductProposalId,
    policy_id: CatalogProposalPolicyVersionId,
    *,
    approval_id: str,
    owner: str | None = None,
    run_id: str | None = None,
    request_hash: str = HASH_C,
    state: str = "approved",
    subject_proposal_id: CatalogProductProposalId | None = None,
) -> ApprovalId:
    subject_id = subject_proposal_id or proposal_id
    payload = {
        "schema_version": "catalog-cultivation-v1",
        "tenant_id": values["tenant"],
        "approval_type": "catalog_product_cultivation",
        "proposal_id": str(subject_id),
        "policy_version_id": str(policy_id),
        "facts_hash": HASH_A,
        "request_hash": request_hash,
    }
    decided = state in {"approved", "rejected"}
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO approval_packages "
                "(tenant_id,approval_id,approval_type,title,proposed_change,reason,"
                "blast_radius,created_at,expires_at,state,proposed_by_run,"
                "evidence_refs,change_set_ref,owner_employee,contract_namespace,"
                "request_hash,expires_at_limit,decided_at,decided_by) VALUES "
                "(:tenant,:approval,'catalog_product_cultivation','Cultivation',"
                "CAST(:payload AS jsonb),'internal','{}'::jsonb,:now,:expires,:state,"
                ":run,'[]'::jsonb,:change_set,:owner,'catalog-cultivation-v1',"
                ":request_hash,:expires,:decided_at,:decided_by)"
            ),
            values
            | {
                "approval": approval_id,
                "payload": json.dumps(payload),
                "now": NOW - timedelta(minutes=2),
                "expires": NOW + timedelta(days=3),
                "state": state,
                "run": run_id or values["run"],
                "owner": owner or values["owner"],
                "change_set": f"catalog-cultivation:{subject_id}:{policy_id}:{HASH_A}",
                "request_hash": request_hash,
                "decided_at": NOW - timedelta(minutes=1) if decided else None,
                "decided_by": values["reviewer"] if decided else None,
            },
        )
    return ApprovalId(approval_id)


def _decision(
    values: dict[str, str],
    proposal_id: CatalogProductProposalId,
    policy_id: CatalogProposalPolicyVersionId,
    approval_id: ApprovalId,
    *,
    state: str = "approved",
) -> CatalogApprovalDecisionInput:
    decided = state in {"approved", "rejected"}
    return CatalogApprovalDecisionInput(
        approval_id=approval_id,
        approval_type="catalog_product_cultivation",
        contract_namespace="catalog-cultivation-v1",
        change_set_ref=f"catalog-cultivation:{proposal_id}:{policy_id}:{HASH_A}",
        request_hash=HASH_C,
        state=state,
        proposed_by_run=RunId(values["run"]),
        proposed_by_employee=None,
        owner_employee=EmployeeId(values["owner"]),
        decided_by_employee=(EmployeeId(values["reviewer"]) if decided else None),
        decided_at=NOW - timedelta(minutes=1) if decided else None,
        expires_at=NOW if state == "expired" else NOW + timedelta(days=3),
    )


def _service(
    factory: async_sessionmaker, tenant: TenantId
) -> CatalogProposalServiceImpl:
    return CatalogProposalServiceImpl(
        lambda scoped: SqlAlchemyCatalogProductsUnitOfWork(factory, scoped),
        Phase2ProductAuthorizer(tenant),
        now=lambda: NOW,
    )


async def _proposal(
    engine: AsyncEngine,
    factory: async_sessionmaker,
    values: dict[str, str],
) -> tuple[
    CatalogProposalServiceImpl, CatalogProductProposalId, CatalogProposalPolicyVersionId
]:
    tenant = TenantId(values["tenant"])
    policy = await _active_policy(engine, factory, values)
    service = _service(factory, tenant)
    await service.evaluate_cluster(
        tenant,
        _facts(values),
        proposed_by_run=RunId(values["run"]),
        actor=_system(tenant),
    )
    proposals = await service.list_proposals(
        tenant, actor=_reader(tenant, values["owner"]), limit=10
    )
    return service, proposals[0].proposal_id, policy.policy_version_id


@pytest.mark.asyncio
async def test_concurrent_evaluation_replay_converges_with_one_proposal_and_event(
    catalog_proposal_engine: AsyncEngine,
) -> None:
    values = await _seed_parents(catalog_proposal_engine, "concurrent")
    tenant = TenantId(values["tenant"])
    factory = async_sessionmaker(catalog_proposal_engine, expire_on_commit=False)
    await _active_policy(catalog_proposal_engine, factory, values)
    first = _service(factory, tenant)
    second = _service(factory, tenant)
    try:
        views = await asyncio.wait_for(
            asyncio.gather(
                first.evaluate_cluster(
                    tenant,
                    _facts(values),
                    proposed_by_run=RunId(values["run"]),
                    actor=_system(tenant),
                ),
                second.evaluate_cluster(
                    tenant,
                    _facts(values),
                    proposed_by_run=RunId(values["run"]),
                    actor=_system(tenant),
                ),
            ),
            timeout=5,
        )
        assert views[0] == views[1]
        async with catalog_proposal_engine.connect() as connection:
            counts = []
            for row in (
                CatalogProposalEvaluationRow,
                CatalogProductProposalRow,
            ):
                counts.append(
                    await connection.scalar(
                        select(func.count())
                        .select_from(row)
                        .where(row.tenant_id == values["tenant"])
                    )
                )
            event_count = await connection.scalar(
                select(func.count())
                .select_from(OutboxEventRow)
                .where(
                    OutboxEventRow.tenant_id == values["tenant"],
                    OutboxEventRow.event_type == "CatalogProductProposalCreated",
                )
            )
        assert counts == [1, 1]
        assert event_count == 1
    finally:
        await _cleanup(catalog_proposal_engine, values["tenant"])


@pytest.mark.asyncio
async def test_failed_and_blocked_evaluations_persist_without_proposals(
    catalog_proposal_engine: AsyncEngine,
) -> None:
    values = await _seed_parents(catalog_proposal_engine, "blocked")
    tenant = TenantId(values["tenant"])
    factory = async_sessionmaker(catalog_proposal_engine, expire_on_commit=False)
    await _active_policy(catalog_proposal_engine, factory, values)
    service = _service(factory, tenant)
    try:
        failed = await service.evaluate_cluster(
            tenant,
            _facts(values, accounts=2),
            proposed_by_run=RunId(values["run"]),
            actor=_system(tenant),
        )
        damaged = _facts(values, facts_hash=HASH_C).model_copy(
            update={"member_count": 99}
        )
        blocked = await service.evaluate_cluster(
            tenant,
            damaged,
            proposed_by_run=RunId(values["run"]),
            actor=_system(tenant),
        )
        assert failed.overall_passed is False and failed.blocked_reason is None
        assert blocked.blocked_reason == "catalog_facts_invalid"

        async with catalog_proposal_engine.connect() as connection:
            rows = list(
                (
                    await connection.execute(
                        select(
                            CatalogProposalEvaluationRow.facts,
                            CatalogProposalEvaluationRow.blocked_reason,
                        ).where(
                            CatalogProposalEvaluationRow.tenant_id == values["tenant"]
                        )
                    )
                ).mappings()
            )
            proposal_count = await connection.scalar(
                select(func.count())
                .select_from(CatalogProductProposalRow)
                .where(CatalogProductProposalRow.tenant_id == values["tenant"])
            )
        assert len(rows) == 2
        blocked_row = next(item for item in rows if item["blocked_reason"] is not None)
        assert blocked_row["facts"] == {
            "tenant_id": values["tenant"],
            "cluster_id": values["cluster"],
            "facts_hash": HASH_C,
        }
        assert proposal_count == 0
    finally:
        await _cleanup(catalog_proposal_engine, values["tenant"])


@pytest.mark.asyncio
async def test_proposal_event_failure_rolls_back_evaluation_and_proposal(
    catalog_proposal_engine: AsyncEngine,
) -> None:
    values = await _seed_parents(catalog_proposal_engine, "eval_rb")
    tenant = TenantId(values["tenant"])
    factory = async_sessionmaker(catalog_proposal_engine, expire_on_commit=False)
    await _active_policy(catalog_proposal_engine, factory, values)
    service = _service(factory, tenant)
    armed = True

    def fail_outbox(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if armed and statement.lstrip().upper().startswith("INSERT INTO OUTBOX_EVENTS"):
            raise RuntimeError("postgresql://secret@private/catalog")

    event.listen(
        catalog_proposal_engine.sync_engine, "before_cursor_execute", fail_outbox
    )
    try:
        with pytest.raises(TransientError) as caught:
            await service.evaluate_cluster(
                tenant,
                _facts(values),
                proposed_by_run=RunId(values["run"]),
                actor=_system(tenant),
            )
        assert "secret" not in str(caught.value)
        armed = False
        event.remove(
            catalog_proposal_engine.sync_engine, "before_cursor_execute", fail_outbox
        )
        async with catalog_proposal_engine.connect() as connection:
            counts = []
            for row in (CatalogProposalEvaluationRow, CatalogProductProposalRow):
                counts.append(
                    await connection.scalar(
                        select(func.count())
                        .select_from(row)
                        .where(row.tenant_id == values["tenant"])
                    )
                )
        assert counts == [0, 0]
    finally:
        if armed:
            event.remove(
                catalog_proposal_engine.sync_engine,
                "before_cursor_execute",
                fail_outbox,
            )
        await _cleanup(catalog_proposal_engine, values["tenant"])


@pytest.mark.asyncio
async def test_binding_checks_persisted_owner_and_run_before_mutation(
    catalog_proposal_engine: AsyncEngine,
) -> None:
    values = await _seed_parents(catalog_proposal_engine, "binding")
    tenant = TenantId(values["tenant"])
    factory = async_sessionmaker(catalog_proposal_engine, expire_on_commit=False)
    try:
        service, proposal_id, policy_id = await _proposal(
            catalog_proposal_engine, factory, values
        )
        active = await service.get_active_policy(
            tenant, actor=_reader(tenant, values["owner"])
        )
        assert active is not None and active.approval_id is not None
        wrong_approvals = [active.approval_id]
        for marker, overrides in (
            ("owner", {"owner": values["reviewer"]}),
            ("run", {"run_id": values["other_run"]}),
            ("hash", {"request_hash": HASH_B}),
            (
                "subject",
                {"subject_proposal_id": CatalogProductProposalId("cpr_cps_other")},
            ),
        ):
            wrong_approvals.append(
                await _insert_cultivation_approval(
                    catalog_proposal_engine,
                    values,
                    proposal_id,
                    policy_id,
                    approval_id=f"apr_cps_wrong_{marker}",
                    **overrides,  # type: ignore[arg-type]
                )
            )
        for wrong_id in wrong_approvals:
            with pytest.raises(CatalogProposalApprovalConflictError):
                await service.bind_proposal_approval(
                    tenant, proposal_id, wrong_id, HASH_C, actor=_system(tenant)
                )
        async with catalog_proposal_engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(CatalogProductProposalRow.state).where(
                        CatalogProductProposalRow.tenant_id == values["tenant"],
                        CatalogProductProposalRow.proposal_id == str(proposal_id),
                    )
                )
                == "awaiting_approval_submission"
            )

        approval_id = await _insert_cultivation_approval(
            catalog_proposal_engine,
            values,
            proposal_id,
            policy_id,
            approval_id="apr_cps_exact_binding",
        )
        first = await service.bind_proposal_approval(
            tenant, proposal_id, approval_id, HASH_C, actor=_system(tenant)
        )
        replay = await service.bind_proposal_approval(
            tenant, proposal_id, approval_id, HASH_C, actor=_system(tenant)
        )
        assert replay == first
        assert first.state == "pending_review"
    finally:
        await _cleanup(catalog_proposal_engine, values["tenant"])


@pytest.mark.asyncio
async def test_approved_application_is_one_queued_case_and_no_product_or_sourcing(
    catalog_proposal_engine: AsyncEngine,
) -> None:
    values = await _seed_parents(catalog_proposal_engine, "approved")
    tenant = TenantId(values["tenant"])
    factory = async_sessionmaker(catalog_proposal_engine, expire_on_commit=False)
    try:
        service, proposal_id, policy_id = await _proposal(
            catalog_proposal_engine, factory, values
        )
        approval_id = await _insert_cultivation_approval(
            catalog_proposal_engine,
            values,
            proposal_id,
            policy_id,
            approval_id="apr_cps_approved",
        )
        await service.bind_proposal_approval(
            tenant, proposal_id, approval_id, HASH_C, actor=_system(tenant)
        )
        decision = _decision(values, proposal_id, policy_id, approval_id)
        first = await service.apply_cultivation_decision(
            tenant, proposal_id, decision, _facts(values), actor=_system(tenant)
        )
        replay = await service.apply_cultivation_decision(
            tenant,
            proposal_id,
            decision,
            _facts(values, facts_hash=HASH_B),
            actor=_system(tenant),
        )
        assert replay == first
        assert first.state == "cultivation_queued"

        async with catalog_proposal_engine.connect() as connection:
            case_count = await connection.scalar(
                select(func.count())
                .select_from(CatalogCultivationCaseRow)
                .where(CatalogCultivationCaseRow.tenant_id == values["tenant"])
            )
            queued_events = await connection.scalar(
                select(func.count())
                .select_from(OutboxEventRow)
                .where(
                    OutboxEventRow.tenant_id == values["tenant"],
                    OutboxEventRow.event_type == "CatalogCultivationQueued",
                )
            )
            product_count = await connection.scalar(
                select(func.count())
                .select_from(ProductRow)
                .where(ProductRow.tenant_id == values["tenant"])
            )
            sourcing_count = await connection.scalar(
                select(func.count())
                .select_from(SourcingCaseRow)
                .where(SourcingCaseRow.tenant_id == values["tenant"])
            )
        assert (case_count, queued_events, product_count, sourcing_count) == (
            1,
            1,
            0,
            0,
        )
    finally:
        await _cleanup(catalog_proposal_engine, values["tenant"])


@pytest.mark.asyncio
async def test_stale_facts_commit_terminal_without_case(
    catalog_proposal_engine: AsyncEngine,
) -> None:
    values = await _seed_parents(catalog_proposal_engine, "stale")
    tenant = TenantId(values["tenant"])
    factory = async_sessionmaker(catalog_proposal_engine, expire_on_commit=False)
    try:
        service, proposal_id, policy_id = await _proposal(
            catalog_proposal_engine, factory, values
        )
        approval_id = await _insert_cultivation_approval(
            catalog_proposal_engine,
            values,
            proposal_id,
            policy_id,
            approval_id="apr_cps_stale",
        )
        await service.bind_proposal_approval(
            tenant, proposal_id, approval_id, HASH_C, actor=_system(tenant)
        )
        result = await service.apply_cultivation_decision(
            tenant,
            proposal_id,
            _decision(values, proposal_id, policy_id, approval_id),
            _facts(values, facts_hash=HASH_B),
            actor=_system(tenant),
        )
        assert result.state == "stale"
        async with catalog_proposal_engine.connect() as connection:
            assert (
                await connection.scalar(
                    select(func.count())
                    .select_from(CatalogCultivationCaseRow)
                    .where(CatalogCultivationCaseRow.tenant_id == values["tenant"])
                )
                == 0
            )
    finally:
        await _cleanup(catalog_proposal_engine, values["tenant"])


@pytest.mark.asyncio
async def test_cultivation_outbox_failure_rolls_back_case_and_proposal_state(
    catalog_proposal_engine: AsyncEngine,
) -> None:
    values = await _seed_parents(catalog_proposal_engine, "rollback")
    tenant = TenantId(values["tenant"])
    factory = async_sessionmaker(catalog_proposal_engine, expire_on_commit=False)
    armed = False

    def fail_outbox(
        _conn: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if armed and statement.lstrip().upper().startswith("INSERT INTO OUTBOX_EVENTS"):
            raise RuntimeError("postgresql://secret@private/catalog")

    try:
        service, proposal_id, policy_id = await _proposal(
            catalog_proposal_engine, factory, values
        )
        approval_id = await _insert_cultivation_approval(
            catalog_proposal_engine,
            values,
            proposal_id,
            policy_id,
            approval_id="apr_cps_rollback",
        )
        await service.bind_proposal_approval(
            tenant, proposal_id, approval_id, HASH_C, actor=_system(tenant)
        )
        event.listen(
            catalog_proposal_engine.sync_engine, "before_cursor_execute", fail_outbox
        )
        armed = True
        with pytest.raises(TransientError) as caught:
            await service.apply_cultivation_decision(
                tenant,
                proposal_id,
                _decision(values, proposal_id, policy_id, approval_id),
                _facts(values),
                actor=_system(tenant),
            )
        assert "secret" not in str(caught.value)
        armed = False
        event.remove(
            catalog_proposal_engine.sync_engine, "before_cursor_execute", fail_outbox
        )

        async with catalog_proposal_engine.connect() as connection:
            state = await connection.scalar(
                select(CatalogProductProposalRow.state).where(
                    CatalogProductProposalRow.tenant_id == values["tenant"],
                    CatalogProductProposalRow.proposal_id == str(proposal_id),
                )
            )
            case_count = await connection.scalar(
                select(func.count())
                .select_from(CatalogCultivationCaseRow)
                .where(CatalogCultivationCaseRow.tenant_id == values["tenant"])
            )
        assert state == "pending_review"
        assert case_count == 0
    finally:
        if armed:
            event.remove(
                catalog_proposal_engine.sync_engine,
                "before_cursor_execute",
                fail_outbox,
            )
        await _cleanup(catalog_proposal_engine, values["tenant"])
