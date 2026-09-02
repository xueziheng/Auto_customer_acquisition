"""Sourcing V2、三个供给池与成本来源的数据库迁移合同。"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from domains.costing.permissions import (
    CostingActor,
    CostingScope,
    Phase1CostingAuthorizer,
)
from domains.costing.schemas import SourcingEstimateCreate
from domains.costing.service_impl import CostingServiceImpl
from infra.db.costing_uow import SqlAlchemyCostingUnitOfWork
from infra.db.session import create_engine_from
from infra.db.tables import Base
from shared.schemas.identifiers import (
    ArtifactId,
    CostSheetId,
    OpportunityId,
    ProductId,
    SourcingCaseId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
    TenantId,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

SOURCING_TABLES = {
    "sourcing_cases",
    "sourcing_ladder_checks",
    "sourcing_public_plans",
    "sourcing_candidates",
    "sourcing_candidate_evidence",
    "sourcing_supply_options",
    "sourcing_reviews",
    "sourcing_search_executions",
    "sourcing_page_attempts",
    "sourcing_search_reconciliations",
}
SUPPLY_TABLES = {
    "products",
    "product_match_specs",
    "product_variants",
    "supply_capabilities",
    "product_candidate_sources",
    "product_candidate_price_refs",
    "suppliers",
    "supplier_price_records",
}
ALL_TABLES = SOURCING_TABLES | SUPPLY_TABLES
ADMISSION_TABLES = {
    "sourcing_admissions",
    "sourcing_priority_snapshots",
}

TENANT_A = "tn_0" + "A" * 25
TENANT_B = "tn_0" + "B" * 25
ARTIFACT_A = "art_0" + "C" * 25
ARTIFACT_B = "art_0" + "D" * 25
EXPECTED_STOP_CODES = (
    "approval_required",
    "quota_status_unknown",
    "paid_usage_enabled",
    "quota_exhausted",
    "provider_timeout",
    "provider_rate_limited",
    "page_access_forbidden",
    "login_or_captcha",
    "unsafe_redirect",
    "no_search_results",
    "no_verifiable_supplier",
    "no_qualified_candidate",
    "reconciliation_required",
    "opportunity_required",
    "need_incomplete",
    "plan_confirmation_required",
    "free_quota_unavailable",
    "budget_exhausted",
    "no_qualified_supply",
    "manual_stop",
)


class _CostingUowFactory:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    def __call__(self, tenant_id: TenantId) -> SqlAlchemyCostingUnitOfWork:
        return SqlAlchemyCostingUnitOfWork(self._sessions, tenant_id)


def _run_alembic(db_url: str, *command: str) -> None:
    env = {**os.environ, "DATABASE_URL": db_url}
    result = subprocess.run(
        [sys.executable, "scripts/run_alembic.py", *command],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, f"alembic {' '.join(command)} 失败（输出已隐藏）"


def _alembic_result(db_url: str, *command: str) -> subprocess.CompletedProcess[bytes]:
    env = {**os.environ, "DATABASE_URL": db_url}
    return subprocess.run(
        [sys.executable, "scripts/run_alembic.py", *command],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        check=False,
    )


def _sync_table_names(connection: Connection) -> set[str]:
    return set(inspect(connection).get_table_names())


async def _current_tables(db_url: str) -> set[str]:
    engine = create_engine_from(db_url)
    try:
        async with engine.connect() as connection:
            return await connection.run_sync(_sync_table_names)
    finally:
        await engine.dispose()


def _sync_contract(connection: Connection) -> dict[str, dict[str, object]]:
    inspector = inspect(connection)
    result: dict[str, dict[str, object]] = {}
    for table in ALL_TABLES | {"cost_sheets"}:
        result[table] = {
            "columns": {
                str(column["name"]): column for column in inspector.get_columns(table)
            },
            "pk": tuple(inspector.get_pk_constraint(table)["constrained_columns"]),
            "foreign_keys": {
                str(item["name"]): (
                    tuple(item["constrained_columns"]),
                    str(item["referred_table"]),
                    tuple(item["referred_columns"]),
                )
                for item in inspector.get_foreign_keys(table)
            },
            "indexes": {
                str(item["name"]): item for item in inspector.get_indexes(table)
            },
            "unique_constraints": {
                str(item["name"]): tuple(item["column_names"])
                for item in inspector.get_unique_constraints(table)
            },
        }
    return result


async def _expect_integrity(
    connection: AsyncConnection, statement: str, values: dict[str, object]
) -> None:
    with pytest.raises(IntegrityError):
        async with connection.begin_nested():
            await connection.execute(text(statement), values)


async def _seed_tenant_evidence(
    connection: AsyncConnection, tenant_id: str, artifact_id: str
) -> None:
    await connection.execute(
        text(
            "INSERT INTO raw_artifacts "
            "(tenant_id, artifact_id, kind, content_hash, size_bytes, mime_type, object_key, uploaded_at) "
            "VALUES (:tenant, :artifact, 'web_snapshot', :hash, 1, 'text/html', :object_key, now()) "
            "ON CONFLICT (tenant_id, artifact_id) DO NOTHING"
        ),
        {
            "tenant": tenant_id,
            "artifact": artifact_id,
            "hash": hashlib.sha256(artifact_id.encode()).hexdigest(),
            "object_key": f"raw/{tenant_id}/{artifact_id}",
        },
    )


async def _seed_need_and_case(
    connection: AsyncConnection,
    *,
    tenant_id: str,
    need_id: str,
    case_id: str,
) -> None:
    await connection.execute(
        text(
            "INSERT INTO validated_needs "
            "(tenant_id, need_id, account_id, product_category, source_message_id, status, created_at) "
            "VALUES (:tenant, :need, 'account-a', CAST(:category AS jsonb), 'message-a', "
            "'sourcing_ready', now()) ON CONFLICT (tenant_id, need_id) DO NOTHING"
        ),
        {
            "tenant": tenant_id,
            "need": need_id,
            "category": json.dumps({"value": "hinges"}),
        },
    )
    await connection.execute(
        text(
            "INSERT INTO sourcing_cases "
            "(tenant_id, case_id, need_id, workflow_version, trigger_key, need_snapshot, "
            "need_snapshot_hash, state, version, opened_at, state_changed_at) "
            "VALUES (:tenant, :case, :need, 2, :trigger, CAST(:snapshot AS jsonb), "
            ":hash, 'opened', 1, now(), now())"
        ),
        {
            "tenant": tenant_id,
            "case": case_id,
            "need": need_id,
            "trigger": f"{need_id}:2",
            "snapshot": json.dumps({"need_id": need_id, "completeness": 3}),
            "hash": "a" * 64,
        },
    )


async def test_0053_admission_schema_is_tenant_bound_and_matches_orm(
    db_url: str,
) -> None:
    """0053 的准入、快照、复合 FK 与索引都由数据库真实约束。"""
    from infra.db.tables import SourcingAdmissionRow, SourcingPrioritySnapshotRow

    engine: AsyncEngine | None = None
    try:
        _run_alembic(db_url, "downgrade", "0052")
        assert not (await _current_tables(db_url)) & ADMISSION_TABLES
        _run_alembic(db_url, "upgrade", "0053")
        engine = create_engine_from(db_url)

        def inspect_admission_contract(connection: Connection) -> dict[str, object]:
            inspector = inspect(connection)
            return {
                "tables": set(inspector.get_table_names()),
                "admission_columns": {
                    str(column["name"])
                    for column in inspector.get_columns("sourcing_admissions")
                },
                "snapshot_columns": {
                    str(column["name"])
                    for column in inspector.get_columns("sourcing_priority_snapshots")
                },
                "admission_pk": tuple(
                    inspector.get_pk_constraint("sourcing_admissions")[
                        "constrained_columns"
                    ]
                ),
                "snapshot_pk": tuple(
                    inspector.get_pk_constraint("sourcing_priority_snapshots")[
                        "constrained_columns"
                    ]
                ),
                "admission_fks": {
                    str(item["name"]): (
                        tuple(item["constrained_columns"]),
                        str(item["referred_table"]),
                        tuple(item["referred_columns"]),
                    )
                    for item in inspector.get_foreign_keys("sourcing_admissions")
                },
                "snapshot_fks": {
                    str(item["name"]): (
                        tuple(item["constrained_columns"]),
                        str(item["referred_table"]),
                        tuple(item["referred_columns"]),
                    )
                    for item in inspector.get_foreign_keys(
                        "sourcing_priority_snapshots"
                    )
                },
                "admission_uniques": {
                    str(item["name"]): tuple(item["column_names"])
                    for item in inspector.get_unique_constraints("sourcing_admissions")
                },
                "snapshot_uniques": {
                    str(item["name"]): tuple(item["column_names"])
                    for item in inspector.get_unique_constraints(
                        "sourcing_priority_snapshots"
                    )
                },
                "admission_indexes": {
                    str(item["name"]): tuple(item["column_names"])
                    for item in inspector.get_indexes("sourcing_admissions")
                },
                "snapshot_indexes": {
                    str(item["name"]): tuple(item["column_names"])
                    for item in inspector.get_indexes("sourcing_priority_snapshots")
                },
                "admission_checks": {
                    str(item["name"])
                    for item in inspector.get_check_constraints("sourcing_admissions")
                },
                "snapshot_checks": {
                    str(item["name"])
                    for item in inspector.get_check_constraints(
                        "sourcing_priority_snapshots"
                    )
                },
            }

        async with engine.connect() as connection:
            contract = await connection.run_sync(inspect_admission_contract)
            current_fk = (
                await connection.execute(
                    text(
                        "SELECT condeferrable, condeferred FROM pg_constraint "
                        "WHERE conname='fk_sourcing_admissions_current_snapshot'"
                    )
                )
            ).one()
            revision = await connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
            order_index = await connection.scalar(
                text(
                    "SELECT pg_get_indexdef(indexrelid) FROM pg_index "
                    "WHERE indexrelid = "
                    "'ix_sourcing_priority_snapshots_order'::regclass"
                )
            )

        assert revision == "0053"
        assert ADMISSION_TABLES <= contract["tables"]
        assert contract["admission_columns"] == set(
            SourcingAdmissionRow.__table__.columns.keys()
        )
        assert contract["snapshot_columns"] == set(
            SourcingPrioritySnapshotRow.__table__.columns.keys()
        )
        assert contract["admission_pk"] == ("tenant_id", "admission_id")
        assert contract["snapshot_pk"] == ("tenant_id", "snapshot_id")
        assert contract["admission_fks"] == {
            "fk_sourcing_admissions_case": (
                ("tenant_id", "case_id"),
                "sourcing_cases",
                ("tenant_id", "case_id"),
            ),
            "fk_sourcing_admissions_need": (
                ("tenant_id", "need_id"),
                "validated_needs",
                ("tenant_id", "need_id"),
            ),
            "fk_sourcing_admissions_current_snapshot": (
                ("tenant_id", "admission_id", "current_snapshot_id"),
                "sourcing_priority_snapshots",
                ("tenant_id", "admission_id", "snapshot_id"),
            ),
        }
        assert current_fk == (True, True)
        assert contract["snapshot_fks"] == {
            "fk_sourcing_priority_snapshots_admission": (
                ("tenant_id", "admission_id"),
                "sourcing_admissions",
                ("tenant_id", "admission_id"),
            ),
            "fk_sourcing_priority_snapshots_case": (
                ("tenant_id", "case_id"),
                "sourcing_cases",
                ("tenant_id", "case_id"),
            ),
            "fk_sourcing_priority_snapshots_need": (
                ("tenant_id", "need_id"),
                "validated_needs",
                ("tenant_id", "need_id"),
            ),
        }
        assert contract["admission_uniques"] == {
            "uq_sourcing_admissions_case": ("tenant_id", "case_id"),
            "uq_sourcing_admissions_need": ("tenant_id", "need_id"),
        }
        assert contract["snapshot_uniques"] == {
            "uq_sourcing_priority_snapshots_admission_snapshot": (
                "tenant_id",
                "admission_id",
                "snapshot_id",
            ),
            "uq_sourcing_priority_snapshots_facts": (
                "tenant_id",
                "admission_id",
                "facts_hash",
            ),
        }
        assert contract["admission_indexes"]["ix_sourcing_admissions_queue"] == (
            "tenant_id",
            "state",
            "current_snapshot_id",
        )
        assert contract["snapshot_indexes"]["ix_sourcing_priority_snapshots_order"] == (
            "tenant_id",
            "cluster_member_count",
            "ready_at",
            "need_id",
            "snapshot_id",
        )
        assert "cluster_member_count DESC" in order_index
        assert contract["admission_checks"] == {
            "ck_sourcing_admissions_core",
            "ck_sourcing_admissions_state",
            "ck_sourcing_admissions_state_fields",
            "ck_sourcing_admissions_times",
        }
        assert contract["snapshot_checks"] == {
            "ck_sourcing_priority_snapshots_cluster",
            "ck_sourcing_priority_snapshots_core",
            "ck_sourcing_priority_snapshots_hash",
            "ck_sourcing_priority_snapshots_times",
            "ck_sourcing_priority_snapshots_version",
        }
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "upgrade", "head")


async def test_0053_enforces_state_tenant_immutability_and_safe_downgrade(
    db_url: str,
) -> None:
    """0053 在数据库边界拒绝坏状态、跨租户引用、快照改写和证据降级。"""
    created_at = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    ready_at = created_at - timedelta(days=2)
    engine: AsyncEngine | None = None
    try:
        _run_alembic(db_url, "downgrade", "0052")
        _run_alembic(db_url, "upgrade", "0053")
        engine = create_engine_from(db_url)
        async with engine.begin() as connection:
            await _seed_need_and_case(
                connection,
                tenant_id=TENANT_A,
                need_id="need-admission-schema-a",
                case_id="case-admission-schema-a",
            )
            await _seed_need_and_case(
                connection,
                tenant_id=TENANT_B,
                need_id="need-admission-schema-b",
                case_id="case-admission-schema-b",
            )
            await connection.execute(
                text(
                    "INSERT INTO sourcing_admissions "
                    "(tenant_id,admission_id,case_id,need_id,state,ready_at,"
                    "current_snapshot_id,created_at,updated_at) VALUES "
                    "(:tenant,'adm-schema-a','case-admission-schema-a',"
                    "'need-admission-schema-a','waiting',:ready,'sps-schema-a',"
                    ":created,:created)"
                ),
                {"tenant": TENANT_A, "ready": ready_at, "created": created_at},
            )
            await connection.execute(
                text(
                    "INSERT INTO sourcing_priority_snapshots "
                    "(tenant_id,snapshot_id,admission_id,case_id,need_id,cluster_id,"
                    "cluster_member_count,ready_at,ranking_version,facts_observed_at,"
                    "facts_hash,created_at) VALUES "
                    "(:tenant,'sps-schema-a','adm-schema-a',"
                    "'case-admission-schema-a','need-admission-schema-a',NULL,1,"
                    ":ready,'need-cluster-admission-v1',:created,:hash,:created)"
                ),
                {
                    "tenant": TENANT_A,
                    "ready": ready_at,
                    "created": created_at,
                    "hash": "b" * 64,
                },
            )

            await _expect_integrity(
                connection,
                "UPDATE sourcing_admissions SET state='blocked',"
                "current_snapshot_id=NULL,blocked_reason='case_state_mismatch' "
                "WHERE tenant_id=:tenant AND admission_id='adm-schema-a'",
                {"tenant": TENANT_A},
            )
            await _expect_integrity(
                connection,
                "INSERT INTO sourcing_priority_snapshots "
                "(tenant_id,snapshot_id,admission_id,case_id,need_id,cluster_id,"
                "cluster_member_count,ready_at,ranking_version,facts_observed_at,"
                "facts_hash,created_at) VALUES "
                "(:tenant,'sps-bad-hash','adm-schema-a','case-admission-schema-a',"
                "'need-admission-schema-a',NULL,1,:ready,"
                "'need-cluster-admission-v1',:created,:hash,:created)",
                {
                    "tenant": TENANT_A,
                    "ready": ready_at,
                    "created": created_at,
                    "hash": "B" * 64,
                },
            )
            await _expect_integrity(
                connection,
                "INSERT INTO sourcing_admissions "
                "(tenant_id,admission_id,case_id,need_id,state,ready_at,"
                "current_snapshot_id,blocked_reason,created_at,updated_at) VALUES "
                "(:tenant,'adm-cross-tenant','case-admission-schema-b',"
                "'need-admission-schema-a','blocked',:ready,NULL,"
                "'priority_facts_invalid',:created,:created)",
                {"tenant": TENANT_A, "ready": ready_at, "created": created_at},
            )
            with pytest.raises(DBAPIError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            "UPDATE sourcing_priority_snapshots "
                            "SET cluster_member_count=2 "
                            "WHERE tenant_id=:tenant AND snapshot_id='sps-schema-a'"
                        ),
                        {"tenant": TENANT_A},
                    )
            with pytest.raises(DBAPIError):
                async with connection.begin_nested():
                    await connection.execute(
                        text(
                            "DELETE FROM sourcing_priority_snapshots "
                            "WHERE tenant_id=:tenant AND snapshot_id='sps-schema-a'"
                        ),
                        {"tenant": TENANT_A},
                    )

        downgrade = _alembic_result(db_url, "downgrade", "0052")
        assert downgrade.returncode != 0
        async with engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "0053"
            )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE sourcing_admissions, sourcing_priority_snapshots CASCADE"
                )
            )
        _run_alembic(db_url, "downgrade", "0052")
        assert not (await _current_tables(db_url)) & ADMISSION_TABLES
        _run_alembic(db_url, "upgrade", "0053")
        assert ADMISSION_TABLES <= await _current_tables(db_url)
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "upgrade", "head")


_INSERT_PRODUCT = (
    "INSERT INTO products "
    "(tenant_id, product_id, pool, candidate_status, name_zh, name_en, category, "
    "normalized_category, lead_time_days_min, lead_time_days_max, internal_cost_amount, "
    "internal_cost_currency, internal_cost_basis, internal_cost_unit, internal_cost_source_ref, "
    "allowed_price_min_amount, allowed_price_min_currency, allowed_price_max_amount, "
    "allowed_price_max_currency, sellable_markets, selling_points, known_issues, "
    "customizable, created_at) VALUES "
    "(:tenant, :product, :pool, :candidate_status, '铰链', 'Hinge', 'hinges', 'hinges', "
    ":lead_min, :lead_max, :cost_amount, :cost_currency, :cost_basis, :cost_unit, :cost_source, "
    ":min_amount, :min_currency, :max_amount, :max_currency, '[]', '[]', '[]', false, now())"
)


def _product_values(product_id: str, **changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "tenant": TENANT_A,
        "product": product_id,
        "pool": "formal",
        "candidate_status": None,
        "lead_min": None,
        "lead_max": None,
        "cost_amount": None,
        "cost_currency": None,
        "cost_basis": None,
        "cost_unit": None,
        "cost_source": None,
        "min_amount": None,
        "min_currency": None,
        "max_amount": None,
        "max_currency": None,
    }
    values.update(changes)
    return values


async def _seed_public_search_execution(
    connection: AsyncConnection,
    *,
    tenant_id: str,
    need_id: str,
    case_id: str,
    plan_id: str,
    run_id: str,
    execution_id: str,
) -> None:
    await _seed_need_and_case(
        connection, tenant_id=tenant_id, need_id=need_id, case_id=case_id
    )
    await connection.execute(
        text(
            "INSERT INTO sourcing_public_plans "
            "(tenant_id, plan_id, case_id, target_countries, product_category, queries, "
            "max_search_queries, max_pages_read, provider, search_depth, usage_credits_remaining, "
            "worst_case_credits, version, expected_case_version, plan_hash, status, created_at) "
            "VALUES (:tenant, :plan, :case, '[\"US\"]', 'hinges', "
            '\'[{"query_text":"hinge factory","target_country":"US"}]\', '
            "1, 1, 'tavily', 'basic', 10, 1, 1, 1, :plan_hash, "
            "'pending_confirmation', now())"
        ),
        {
            "tenant": tenant_id,
            "plan": plan_id,
            "case": case_id,
            "plan_hash": "b" * 64,
        },
    )
    await connection.execute(
        text(
            "INSERT INTO workflow_runs "
            "(run_id, tenant_id, workflow_type, workflow_version, subject_ref, "
            "current_step, status, context, idempotency_key) "
            "VALUES (:run, :tenant, 'sourcing', 1, :case, 'public_search', "
            "'running', '{}', :idempotency_key)"
        ),
        {
            "run": run_id,
            "tenant": tenant_id,
            "case": case_id,
            "idempotency_key": f"{case_id}:public-search",
        },
    )
    await connection.execute(
        text(
            "INSERT INTO sourcing_search_executions "
            "(tenant_id, execution_id, case_id, plan_id, run_id, plan_hash, "
            "query_index, request_key, query_hash, locator_results, provider_status, created_at) "
            "VALUES (:tenant, :execution, :case, :plan, :run, :plan_hash, "
            "0, :request_key, :query_hash, '[]', 'uncertain', now())"
        ),
        {
            "tenant": tenant_id,
            "execution": execution_id,
            "case": case_id,
            "plan": plan_id,
            "run": run_id,
            "plan_hash": "b" * 64,
            "request_key": "c" * 64,
            "query_hash": "d" * 64,
        },
    )


async def test_0047_to_0049_roundtrip(db_url: str) -> None:
    try:
        _run_alembic(db_url, "downgrade", "0046")
        assert not (await _current_tables(db_url)) & ALL_TABLES
        _run_alembic(db_url, "upgrade", "0049")
        assert ALL_TABLES <= await _current_tables(db_url)
        _run_alembic(db_url, "downgrade", "0046")
        assert not (await _current_tables(db_url)) & ALL_TABLES
        _run_alembic(db_url, "upgrade", "0049")
    finally:
        _run_alembic(db_url, "upgrade", "head")


async def test_public_candidate_drafts_reject_update_and_delete_at_database_boundary(
    db_url: str,
) -> None:
    engine: AsyncEngine | None = None
    try:
        _run_alembic(db_url, "downgrade", "0046")
        _run_alembic(db_url, "upgrade", "0049")
        engine = create_engine_from(db_url)
        async with engine.begin() as connection:
            await _seed_tenant_evidence(connection, TENANT_A, ARTIFACT_A)
            await _seed_public_search_execution(
                connection,
                tenant_id=TENANT_A,
                need_id="need-draft-immutable",
                case_id="case-draft-immutable",
                plan_id="plan-draft-immutable",
                run_id="run-draft-immutable",
                execution_id="execution-draft-immutable",
            )
            await connection.execute(
                text(
                    "INSERT INTO sourcing_candidate_drafts "
                    "(tenant_id, draft_id, case_id, run_id, plan_id, plan_hash, "
                    "query_index, result_index, source_key, supplier_name, product_title, "
                    "specs, moq, indicative_price_tiers, rejection_codes, evidence_url, "
                    "evidence_observed_at, evidence_hash, evidence_artifact_ref, created_at) "
                    "VALUES (:tenant, 'draft-immutable', 'case-draft-immutable', "
                    "'run-draft-immutable', 'plan-draft-immutable', :plan_hash, 0, 0, "
                    ":source_key, 'Factory A', 'Hinge HX-4', CAST(:specs AS jsonb), 500, "
                    "CAST(:tiers AS jsonb), '[]', 'https://factory.example/hinge', now(), "
                    ":evidence_hash, :artifact, now())"
                ),
                {
                    "tenant": TENANT_A,
                    "plan_hash": "b" * 64,
                    "source_key": "f" * 64,
                    "specs": json.dumps(
                        [
                            {
                                "spec_name": "model",
                                "required": "HX-4",
                                "observed": "HX-4",
                            }
                        ]
                    ),
                    "tiers": json.dumps(
                        [
                            {
                                "minimum_quantity": 1000,
                                "amount": "1.25",
                                "currency": "USD",
                                "unit": "piece",
                            }
                        ]
                    ),
                    "evidence_hash": "e" * 64,
                    "artifact": ARTIFACT_A,
                },
            )
            await _expect_integrity(
                connection,
                "UPDATE sourcing_candidate_drafts SET product_title='Changed' "
                "WHERE tenant_id=:tenant AND draft_id='draft-immutable'",
                {"tenant": TENANT_A},
            )
            await _expect_integrity(
                connection,
                "DELETE FROM sourcing_candidate_drafts "
                "WHERE tenant_id=:tenant AND draft_id='draft-immutable'",
                {"tenant": TENANT_A},
            )
            row = (
                await connection.execute(
                    text(
                        "SELECT product_title FROM sourcing_candidate_drafts "
                        "WHERE tenant_id=:tenant AND draft_id='draft-immutable'"
                    ),
                    {"tenant": TENANT_A},
                )
            ).one()
            assert row == ("Hinge HX-4",)
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "upgrade", "head")


async def test_sourcing_and_supply_schema_is_tenant_bound_and_uses_exact_amounts(
    db_url: str,
) -> None:
    engine: AsyncEngine | None = None
    try:
        _run_alembic(db_url, "downgrade", "0046")
        _run_alembic(db_url, "upgrade", "0051")
        engine = create_engine_from(db_url)
        async with engine.connect() as connection:
            contract = await connection.run_sync(_sync_contract)

        for table, table_contract in contract.items():
            columns = table_contract["columns"]
            assert "tenant_id" in columns, table
            assert columns["tenant_id"]["nullable"] is False, table
            assert "tenant_id" in table_contract["pk"], table
            assert set(columns) == set(Base.metadata.tables[table].columns.keys()), (
                table
            )

        assert {
            "source_sourcing_case_id",
            "source_option_id",
            "source_product_id",
            "source_candidate_id",
            "source_tier_minimum_quantity",
            "source_unit",
        } <= set(Base.metadata.tables["cost_sheets"].columns.keys())

        candidate_fks = contract["sourcing_candidate_evidence"]["foreign_keys"]
        assert candidate_fks["fk_sourcing_candidate_evidence_artifact"] == (
            ("tenant_id", "artifact_id"),
            "raw_artifacts",
            ("tenant_id", "artifact_id"),
        )
        public_candidate_fks = contract["sourcing_candidates"]["foreign_keys"]
        assert public_candidate_fks["fk_sourcing_candidates_public_draft"] == (
            ("tenant_id", "public_draft_source_key"),
            "sourcing_candidate_drafts",
            ("tenant_id", "source_key"),
        )
        public_source_index = contract["sourcing_candidates"]["indexes"][
            "uq_sourcing_candidates_public_draft_source"
        ]
        assert public_source_index["unique"] is True
        assert tuple(public_source_index["column_names"]) == (
            "tenant_id",
            "public_draft_source_key",
        )
        assert contract["sourcing_candidates"]["columns"][
            "public_draft_source_key"
        ]["nullable"] is True
        source_fks = contract["product_candidate_sources"]["foreign_keys"]
        assert source_fks["fk_product_candidate_sources_candidate"] == (
            ("tenant_id", "sourcing_case_id", "supplier_candidate_id"),
            "sourcing_candidates",
            ("tenant_id", "case_id", "candidate_id"),
        )
        case_fks = contract["sourcing_cases"]["foreign_keys"]
        assert case_fks["fk_sourcing_cases_opportunity"] == (
            ("tenant_id", "opportunity_id"),
            "opportunities",
            ("tenant_id", "opportunity_id"),
        )
        reconciliation_fks = contract["sourcing_search_reconciliations"]["foreign_keys"]
        assert reconciliation_fks["fk_sourcing_search_reconciliations_artifact"] == (
            ("tenant_id", "provider_usage_artifact_ref"),
            "raw_artifacts",
            ("tenant_id", "artifact_id"),
        )
        page_attempt = contract["sourcing_page_attempts"]
        assert {"status", "outcome", "draft_id", "completed_at"} <= set(
            page_attempt["columns"]
        )
        assert page_attempt["foreign_keys"]["fk_sourcing_page_attempts_draft"] == (
            ("tenant_id", "draft_id"),
            "sourcing_candidate_drafts",
            ("tenant_id", "draft_id"),
        )
        assert "internal_cost_unit" in contract["products"]["columns"]
        assert "match_specs" not in contract["products"]["columns"]
        match_spec_contract = contract["product_match_specs"]
        assert match_spec_contract["pk"] == (
            "tenant_id",
            "product_id",
            "normalized_spec_name",
        )
        assert match_spec_contract["foreign_keys"][
            "fk_product_match_specs_product"
        ] == (
            ("tenant_id", "product_id"),
            "products",
            ("tenant_id", "product_id"),
        )
        assert match_spec_contract["foreign_keys"][
            "fk_product_match_specs_artifact"
        ] == (
            ("tenant_id", "evidence_ref"),
            "raw_artifacts",
            ("tenant_id", "artifact_id"),
        )
        assert (
            contract["sourcing_ladder_checks"]["columns"]["outcome"]["nullable"]
            is False
        )
        assert contract["sourcing_supply_options"]["unique_constraints"][
            "uq_sourcing_supply_options_supplier_candidate"
        ] == ("tenant_id", "case_id", "supplier_candidate_id")
        assert contract["sourcing_supply_options"]["unique_constraints"][
            "uq_sourcing_supply_options_case_option_product"
        ] == ("tenant_id", "case_id", "option_id", "product_id")
        assert (
            contract["sourcing_supply_options"]["indexes"][
            "uq_sourcing_supply_options_existing_product"
            ]["unique"]
            is True
        )
        cost_fks = contract["cost_sheets"]["foreign_keys"]
        assert cost_fks["fk_cost_sheets_sourcing_option_product"] == (
            (
                "tenant_id",
                "source_sourcing_case_id",
                "source_option_id",
                "source_product_id",
            ),
            "sourcing_supply_options",
            ("tenant_id", "case_id", "option_id", "product_id"),
        )
        assert cost_fks["fk_cost_sheets_sourcing_candidate_path"] == (
            (
                "tenant_id",
                "source_sourcing_case_id",
                "source_option_id",
                "source_candidate_id",
            ),
            "sourcing_supply_options",
            ("tenant_id", "case_id", "option_id", "supplier_candidate_id"),
        )
        assert str(
            contract["product_candidate_price_refs"]["columns"]["unit_amount"]["type"]
        ).startswith("NUMERIC")
        assert str(
            contract["supplier_price_records"]["columns"]["unit_amount"]["type"]
        ).startswith("NUMERIC")
        assert (
            contract["sourcing_cases"]["indexes"]["uq_sourcing_cases_active_need"][
                "unique"
            ]
            is True
        )
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "upgrade", "head")


async def test_sourcing_case_opportunity_reference_is_nullable_and_tenant_bound(
    db_url: str,
) -> None:
    """旧 Case 可无机会；一旦绑定只能引用同租户真实 Opportunity。"""

    engine: AsyncEngine | None = None
    try:
        _run_alembic(db_url, "downgrade", "0046")
        _run_alembic(db_url, "upgrade", "0049")
        engine = create_engine_from(db_url)
        async with engine.begin() as connection:
            await _seed_need_and_case(
                connection,
                tenant_id=TENANT_A,
                need_id="need-case-opportunity",
                case_id="case-opportunity",
            )
            assert (
                await connection.scalar(
                text(
                    "SELECT opportunity_id FROM sourcing_cases "
                    "WHERE tenant_id = :tenant AND case_id = 'case-opportunity'"
                ),
                {"tenant": TENANT_A},
                )
                is None
            )
            await connection.execute(
                text(
                    "INSERT INTO opportunities "
                    "(opportunity_id, tenant_id, account_id, account_name, country, "
                    "need_id, product_category) VALUES "
                    "('opp-cross-tenant', :tenant, 'account-b', 'Buyer B', 'US', "
                    "'need-b', 'hinges')"
                ),
                {"tenant": TENANT_B},
            )
            await _expect_integrity(
                connection,
                "UPDATE sourcing_cases SET opportunity_id = 'opp-cross-tenant' "
                "WHERE tenant_id = :tenant AND case_id = 'case-opportunity'",
                {"tenant": TENANT_A},
            )
            await connection.execute(
                text(
                    "INSERT INTO opportunities "
                    "(opportunity_id, tenant_id, account_id, account_name, country, "
                    "need_id, product_category) VALUES "
                    "('opp-same-tenant', :tenant, 'account-a', 'Buyer A', 'US', "
                    "'need-a', 'hinges')"
                ),
                {"tenant": TENANT_A},
            )
            await connection.execute(
                text(
                    "UPDATE sourcing_cases SET opportunity_id = 'opp-same-tenant' "
                    "WHERE tenant_id = :tenant AND case_id = 'case-opportunity'"
                ),
                {"tenant": TENANT_A},
            )
            assert (
                await connection.scalar(
                text(
                    "SELECT opportunity_id FROM sourcing_cases "
                    "WHERE tenant_id = :tenant AND case_id = 'case-opportunity'"
                ),
                {"tenant": TENANT_A},
                )
                == "opp-same-tenant"
            )
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "upgrade", "head")


async def test_stop_codes_and_safe_detail_roundtrip_without_draft_shorthands(
    db_url: str,
) -> None:
    engine: AsyncEngine | None = None
    try:
        _run_alembic(db_url, "downgrade", "0046")
        _run_alembic(db_url, "upgrade", "0049")
        engine = create_engine_from(db_url)
        async with engine.begin() as connection:
            await _seed_need_and_case(
                connection,
                tenant_id=TENANT_A,
                need_id="need-stop-codes",
                case_id="case-stop-codes",
            )
            for sequence, stop_code in enumerate(EXPECTED_STOP_CODES):
                detail = json.dumps(
                    {
                        "stage": "provider",
                        "query_index": sequence,
                        "provider_http_status": 429,
                        "observed_count": 1,
                        "configured_limit": 2,
                    }
                )
                await connection.execute(
                    text(
                        "UPDATE sourcing_cases SET stop_code = :stop_code, "
                        "stop_detail = CAST(:detail AS jsonb) "
                        "WHERE tenant_id = :tenant AND case_id = 'case-stop-codes'"
                    ),
                    {
                        "tenant": TENANT_A,
                        "stop_code": stop_code,
                        "detail": detail,
                    },
                )
                row = (
                    await connection.execute(
                        text(
                            "SELECT stop_code, stop_detail FROM sourcing_cases "
                            "WHERE tenant_id = :tenant AND case_id = 'case-stop-codes'"
                        ),
                        {"tenant": TENANT_A},
                    )
                ).one()
                assert row.stop_code == stop_code
                assert row.stop_detail["stage"] == "provider"

            for shorthand in (
                "usage_unknown",
                "paid_enabled",
                "request_uncertain",
                "no_results",
            ):
                await _expect_integrity(
                    connection,
                    "UPDATE sourcing_cases SET stop_code = :stop_code "
                    "WHERE tenant_id = :tenant AND case_id = 'case-stop-codes'",
                    {"tenant": TENANT_A, "stop_code": shorthand},
                )
            for unsafe_detail in (
                json.dumps(["not-an-object"]),
                json.dumps({"query_index": 0}),
                json.dumps({"stage": "unknown"}),
                json.dumps({"raw_error": "provider request payload"}),
                json.dumps({"stage": "provider", "query_index": "0"}),
                json.dumps({"stage": "provider", "query_index": -1}),
                json.dumps({"stage": "provider", "query_index": 0.5}),
                json.dumps({"stage": "provider", "provider_http_status": 99}),
                json.dumps({"stage": "provider", "provider_http_status": 429.5}),
            ):
                await _expect_integrity(
                    connection,
                    "UPDATE sourcing_cases SET stop_detail = CAST(:detail AS jsonb) "
                    "WHERE tenant_id = :tenant AND case_id = 'case-stop-codes'",
                    {"tenant": TENANT_A, "detail": unsafe_detail},
                )
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "upgrade", "head")


async def test_stop_detail_requires_a_non_null_string_stage(db_url: str) -> None:
    engine: AsyncEngine | None = None
    try:
        _run_alembic(db_url, "downgrade", "0046")
        _run_alembic(db_url, "upgrade", "0049")
        engine = create_engine_from(db_url)
        async with engine.begin() as connection:
            await _seed_need_and_case(
                connection,
                tenant_id=TENANT_A,
                need_id="need-stop-stage",
                case_id="case-stop-stage",
            )
            for invalid_stage in (None, 1, True):
                await _expect_integrity(
                    connection,
                    "UPDATE sourcing_cases SET stop_detail = CAST(:detail AS jsonb) "
                    "WHERE tenant_id = :tenant AND case_id = 'case-stop-stage'",
                    {
                        "tenant": TENANT_A,
                        "detail": json.dumps({"stage": invalid_stage}),
                    },
                )
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "upgrade", "head")


async def test_stop_detail_accepts_omitted_and_json_null_optional_fields(
    db_url: str,
) -> None:
    engine: AsyncEngine | None = None
    try:
        _run_alembic(db_url, "downgrade", "0046")
        _run_alembic(db_url, "upgrade", "0049")
        engine = create_engine_from(db_url)
        async with engine.begin() as connection:
            await _seed_need_and_case(
                connection,
                tenant_id=TENANT_A,
                need_id="need-stop-null-optionals",
                case_id="case-stop-null-optionals",
            )
            details = (
                {"stage": "provider"},
                {
                    "stage": "provider",
                    "query_index": None,
                    "provider_http_status": None,
                    "observed_count": None,
                    "configured_limit": None,
                },
            )
            for detail in details:
                await connection.execute(
                    text(
                        "UPDATE sourcing_cases SET stop_detail = CAST(:detail AS jsonb) "
                        "WHERE tenant_id = :tenant "
                        "AND case_id = 'case-stop-null-optionals'"
                    ),
                    {"tenant": TENANT_A, "detail": json.dumps(detail)},
                )
                stored = (
                    await connection.execute(
                        text(
                            "SELECT stop_detail FROM sourcing_cases "
                            "WHERE tenant_id = :tenant "
                            "AND case_id = 'case-stop-null-optionals'"
                        ),
                        {"tenant": TENANT_A},
                    )
                ).scalar_one()
                assert stored == detail
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "upgrade", "head")


async def test_product_checks_enforce_null_pairs_and_candidate_lifecycle_statuses(
    db_url: str,
) -> None:
    engine: AsyncEngine | None = None
    try:
        _run_alembic(db_url, "downgrade", "0046")
        _run_alembic(db_url, "upgrade", "0049")
        engine = create_engine_from(db_url)
        async with engine.begin() as connection:
            await _seed_tenant_evidence(connection, TENANT_A, ARTIFACT_A)
            rejected_values = [
                _product_values("product-candidate-null", pool="candidate"),
                _product_values(
                    "product-candidate-unknown",
                    pool="candidate",
                    candidate_status="unknown",
                ),
                _product_values(
                    "product-formal-with-candidate-status",
                    pool="formal",
                    candidate_status="source_only",
                ),
                _product_values(
                    "product-capability-with-candidate-status",
                    pool="capability",
                    candidate_status="partial",
                ),
            ]
            full_cost = {
                "cost_amount": "1.25",
                "cost_currency": "USD",
                "cost_basis": "supplier invoice",
                "cost_unit": "piece",
                "cost_source": ARTIFACT_A,
            }
            for missing in full_cost:
                missing_values = full_cost | {missing: None}
                rejected_values.append(
                    _product_values(
                        f"product-cost-missing-{missing}", **missing_values
                    )
                )
            rejected_values.extend(
                [
                    _product_values(
                        "product-min-missing-amount",
                        min_amount=None,
                        min_currency="USD",
                    ),
                    _product_values(
                        "product-min-missing-currency",
                        min_amount="2.00",
                        min_currency=None,
                    ),
                    _product_values(
                        "product-max-missing-amount",
                        max_amount=None,
                        max_currency="USD",
                    ),
                    _product_values(
                        "product-max-missing-currency",
                        max_amount="3.00",
                        max_currency=None,
                    ),
                    _product_values(
                        "product-lead-missing-min", lead_min=None, lead_max=10
                    ),
                    _product_values(
                        "product-lead-missing-max", lead_min=5, lead_max=None
                    ),
                ]
            )
            for rejected in rejected_values:
                await _expect_integrity(connection, _INSERT_PRODUCT, rejected)

            for candidate_status in ("source_only", "partial", "not_approved"):
                await connection.execute(
                    text(_INSERT_PRODUCT),
                    _product_values(
                        f"product-{candidate_status}",
                        pool="candidate",
                        candidate_status=candidate_status,
                    ),
                )
            statuses = (
                await connection.execute(
                    text(
                        "SELECT candidate_status FROM products "
                        "WHERE tenant_id = :tenant AND pool = 'candidate' "
                        "ORDER BY candidate_status"
                    ),
                    {"tenant": TENANT_A},
                )
            ).scalars()
            assert list(statuses) == ["not_approved", "partial", "source_only"]
            await connection.execute(
                text(_INSERT_PRODUCT),
                _product_values(
                    "product-complete-cost",
                    cost_amount="0.123456789012",
                    cost_currency="USD",
                    cost_basis="supplier invoice",
                    cost_unit="piece",
                    cost_source=ARTIFACT_A,
                ),
            )
            stored_cost = (
                await connection.execute(
                    text(
                        "SELECT internal_cost_amount, internal_cost_currency, "
                        "internal_cost_basis, internal_cost_unit, internal_cost_source_ref "
                        "FROM products WHERE tenant_id = :tenant "
                        "AND product_id = 'product-complete-cost'"
                    ),
                    {"tenant": TENANT_A},
                )
            ).one()
            assert str(stored_cost.internal_cost_amount) == "0.123456789012"
            assert stored_cost.internal_cost_currency == "USD"
            assert stored_cost.internal_cost_basis == "supplier invoice"
            assert stored_cost.internal_cost_unit == "piece"
            assert stored_cost.internal_cost_source_ref == ARTIFACT_A
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "upgrade", "head")


async def test_database_rejects_cross_tenant_evidence_and_malformed_json(
    db_url: str,
) -> None:
    engine: AsyncEngine | None = None
    try:
        _run_alembic(db_url, "downgrade", "0046")
        _run_alembic(db_url, "upgrade", "0049")
        engine = create_engine_from(db_url)
        async with engine.begin() as connection:
            await _seed_tenant_evidence(connection, TENANT_A, ARTIFACT_A)
            await _seed_tenant_evidence(connection, TENANT_B, ARTIFACT_B)
            await _seed_need_and_case(
                connection, tenant_id=TENANT_A, need_id="need-a", case_id="case-a"
            )
            await connection.execute(
                text(
                    "INSERT INTO sourcing_candidates "
                    "(tenant_id, candidate_id, case_id, supplier_name, product_title, "
                    "observed_facts, supplier_claims, match_inferences, verified_specs, "
                    "indicative_price_tiers, rejection_reasons, rejected, created_at) "
                    "VALUES (:tenant, 'candidate-a', 'case-a', 'Supplier', 'Hinge', "
                    "'{}', '{}', '{}', '[]', "
                    "CAST(:tiers AS jsonb), '[]', false, now())"
                ),
                {
                    "tenant": TENANT_A,
                    "tiers": json.dumps(
                        [
                            {
                                "minimum_quantity": 100,
                                "amount": "1.250000",
                                "currency": "USD",
                                "unit": "piece",
                                "provenance": {
                                    "source_type": "web_page",
                                    "source_id": "page-a",
                                    "extracted_by": "human",
                                    "extracted_at": "2026-08-30T10:00:00Z",
                                    "confirmed_by": None,
                                    "confirmed_at": None,
                                },
                                "evidence_ref": ARTIFACT_A,
                            }
                        ]
                    ),
                },
            )
            await _expect_integrity(
                connection,
                "INSERT INTO sourcing_candidate_evidence "
                "(tenant_id, candidate_id, artifact_id, url, observed_at, content_hash) "
                "VALUES (:tenant, 'candidate-a', :artifact, 'https://example.test/a', now(), :hash)",
                {"tenant": TENANT_A, "artifact": ARTIFACT_B, "hash": "e" * 64},
            )
            await _expect_integrity(
                connection,
                "UPDATE sourcing_cases SET sealed_candidate_ids = "
                "'[\"candidate-a\"]'::jsonb WHERE tenant_id = :tenant "
                "AND case_id = 'case-a'",
                {"tenant": TENANT_A},
            )
            await _expect_integrity(
                connection,
                "INSERT INTO sourcing_candidates "
                "(tenant_id, candidate_id, case_id, supplier_name, product_title, observed_facts, "
                "supplier_claims, match_inferences, verified_specs, indicative_price_tiers, "
                "rejection_reasons, rejected, created_at) "
                "VALUES (:tenant, 'candidate-b', 'case-a', 'Supplier', 'Hinge', '[]', '{}', '{}', "
                "'[]', '[]', '[]', false, now())",
                {"tenant": TENANT_A},
            )
            await _expect_integrity(
                connection,
                "INSERT INTO sourcing_candidates "
                "(tenant_id, candidate_id, case_id, supplier_name, product_title, observed_facts, "
                "supplier_claims, match_inferences, verified_specs, indicative_price_tiers, "
                "rejection_reasons, rejected, created_at) "
                "VALUES (:tenant, 'candidate-c', 'case-a', 'Supplier', 'Hinge', '{}', '{}', '{}', "
                "'[]', CAST(:tiers AS jsonb), '[]', false, now())",
                {
                    "tenant": TENANT_A,
                    "tiers": json.dumps(
                        [
                            {
                                "minimum_quantity": 1,
                                "amount": 1.25,
                                "currency": "USD",
                                "unit": "piece",
                            }
                        ]
                    ),
                },
            )
            await _expect_integrity(
                connection,
                "INSERT INTO sourcing_candidates "
                "(tenant_id, candidate_id, case_id, supplier_name, product_title, observed_facts, "
                "supplier_claims, match_inferences, verified_specs, indicative_price_tiers, "
                "rejection_reasons, rejected, created_at) "
                "VALUES (:tenant, 'candidate-d', 'case-a', 'Supplier', 'Hinge', '{}', '{}', '{}', "
                "'[]', CAST(:tiers AS jsonb), '[]', false, now())",
                {
                    "tenant": TENANT_A,
                    "tiers": json.dumps(
                        [
                            {
                                "minimum_quantity": 1,
                                "amount": "1.25",
                                "currency": "USD",
                                "unit": "piece",
                            }
                        ]
                    ),
                },
            )
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "upgrade", "head")


async def test_active_case_plan_review_and_cost_origin_constraints(db_url: str) -> None:
    engine: AsyncEngine | None = None
    try:
        _run_alembic(db_url, "downgrade", "0046")
        _run_alembic(db_url, "upgrade", "0051")
        engine = create_engine_from(db_url)
        async with engine.begin() as connection:
            await _seed_need_and_case(
                connection, tenant_id=TENANT_A, need_id="need-a", case_id="case-a"
            )
            await _expect_integrity(
                connection,
                "INSERT INTO sourcing_cases "
                "(tenant_id, case_id, need_id, workflow_version, trigger_key, need_snapshot, "
                "need_snapshot_hash, state, version, opened_at, state_changed_at) "
                "VALUES (:tenant, 'case-b', 'need-a', 2, 'other-trigger', '{}', :hash, "
                "'discovering', 1, now(), now())",
                {"tenant": TENANT_A, "hash": "b" * 64},
            )
            await _expect_integrity(
                connection,
                "INSERT INTO sourcing_public_plans "
                "(tenant_id, plan_id, case_id, target_countries, product_category, queries, "
                "max_search_queries, max_pages_read, provider, search_depth, usage_credits_remaining, "
                "worst_case_credits, version, expected_case_version, plan_hash, status, created_at, confirmed_by) "
                "VALUES (:tenant, 'plan-a', 'case-a', '[\"US\"]', 'hinges', '[\"hinge factory\"]', "
                "1, 1, 'tavily', 'basic', 10, 1, 1, 1, :hash, 'authorized', now(), 'boss-a')",
                {"tenant": TENANT_A, "hash": "c" * 64},
            )
            await connection.execute(
                text(
                    "INSERT INTO raw_artifacts "
                    "(tenant_id, artifact_id, kind, content_hash, size_bytes, "
                    "mime_type, object_key, uploaded_at) VALUES "
                    "(:tenant, :artifact, 'web_snapshot', :hash, 1, 'text/html', "
                    ":object_key, now()) ON CONFLICT (tenant_id, artifact_id) DO NOTHING"
                ),
                {
                    "tenant": TENANT_A,
                    "artifact": ARTIFACT_A,
                    "hash": "e" * 64,
                    "object_key": f"raw/{TENANT_A}/{ARTIFACT_A}",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO products "
                    "(tenant_id, product_id, pool, name_zh, name_en, category, normalized_category, "
                    "sellable_markets, selling_points, known_issues, customizable, "
                    "internal_cost_amount, internal_cost_currency, internal_cost_basis, "
                    "internal_cost_unit, internal_cost_source_ref, moq, created_at) "
                    "VALUES (:tenant, 'product-a', 'formal', '铰链', 'Hinge', 'hinges', 'hinges', "
                    "'[]', '[]', '[]', false, 1.25, 'USD', 'supplier basis', "
                    "'piece', :artifact, 100, now())"
                ),
                {"tenant": TENANT_A, "artifact": ARTIFACT_A},
            )
            await connection.execute(
                text(
                    "INSERT INTO sourcing_supply_options "
                    "(tenant_id, option_id, case_id, source, product_id, is_qualified, created_at) "
                    "VALUES (:tenant, 'option-a', 'case-a', 'existing_product', 'product-a', true, now())"
                ),
                {"tenant": TENANT_A},
            )
            await _expect_integrity(
                connection,
                "INSERT INTO sourcing_reviews "
                "(tenant_id, review_id, case_id, primary_option_id, primary_selection, "
                "alternate_option_ids, reason, expected_case_version, submitted_by, submitted_at) "
                "VALUES (:tenant, 'review-a', 'case-a', 'option-a', '[]', '[]', 'best fit', 1, 'employee-a', now())",
                {"tenant": TENANT_A},
            )

            await connection.execute(
                text(
                    "INSERT INTO opportunities "
                    "(opportunity_id, tenant_id, account_id, account_name, country, need_id, product_category) "
                    "VALUES ('opp-a', :tenant, 'account-a', 'Acme', 'US', 'cost-need-a', 'hinges') "
                    "ON CONFLICT (opportunity_id) DO NOTHING"
                ),
                {"tenant": TENANT_A},
            )
            cost_values: dict[str, object] = {
                "tenant": TENANT_A,
                "case": "case-a",
                "option": "option-a",
                "product": "product-a",
            }
            await _expect_integrity(
                connection,
                "INSERT INTO cost_sheets "
                "(tenant_id, cost_sheet_id, opportunity_id, version_type, version_number, quantity, "
                "base_currency, quote_currency, created_at, source_sourcing_case_id, source_option_id) "
                "VALUES (:tenant, 'cost-incomplete', 'opp-a', 'estimated', 1, 100, 'USD', 'USD', now(), :case, :option)",
                cost_values,
            )
            await connection.execute(
                text(
                    "INSERT INTO cost_sheets "
                    "(tenant_id, cost_sheet_id, opportunity_id, version_type, version_number, quantity, "
                    "base_currency, quote_currency, created_at, source_sourcing_case_id, source_option_id, "
                    "source_product_id, source_tier_minimum_quantity, source_unit) "
                    "VALUES (:tenant, 'cost-a', 'opp-a', 'estimated', 1, 100, 'USD', "
                    "'USD', now(), :case, :option, :product, 100, 'piece')"
                ),
                cost_values,
            )
            await _expect_integrity(
                connection,
                "INSERT INTO cost_sheets "
                "(tenant_id, cost_sheet_id, opportunity_id, version_type, version_number, quantity, "
                    "base_currency, quote_currency, created_at, source_sourcing_case_id, source_option_id, "
                    "source_product_id, source_tier_minimum_quantity, source_unit) "
                    "VALUES (:tenant, 'cost-b', 'opp-a', 'estimated', 2, 100, 'USD', "
                    "'USD', now(), :case, :option, :product, 100, 'piece')",
                cost_values,
            )
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "upgrade", "head")


async def test_ladder_checks_reject_jumps_updates_and_deletes(db_url: str) -> None:
    engine: AsyncEngine | None = None
    insert_ladder_check = (
        "INSERT INTO sourcing_ladder_checks "
        "(tenant_id, check_id, case_id, sequence_number, rung, input_snapshot, "
        "input_snapshot_hash, outcome, conclusion, spec_comparisons, evidence_refs, checked_by, checked_at) "
        "VALUES (:tenant, :check, :case, :rung, :rung, '{}', :hash, :outcome, "
        "'no match', '[]', '[]', 'employee-a', now())"
    )
    try:
        _run_alembic(db_url, "downgrade", "0046")
        _run_alembic(db_url, "upgrade", "0049")
        engine = create_engine_from(db_url)
        async with engine.begin() as connection:
            await _seed_need_and_case(
                connection,
                tenant_id=TENANT_A,
                need_id="need-ladder-audit",
                case_id="case-ladder-audit",
            )
            values = {
                "tenant": TENANT_A,
                "case": "case-ladder-audit",
                "check": "ladder-check-2",
                "rung": 2,
                "hash": "d" * 64,
                "outcome": "no_qualified_supply",
            }
            await _expect_integrity(connection, insert_ladder_check, values)
            await connection.execute(
                text(insert_ladder_check),
                values | {"check": "ladder-check-1", "rung": 1},
            )
            await connection.execute(text(insert_ladder_check), values)
            persisted_rungs = (
                await connection.execute(
                    text(
                        "SELECT rung FROM sourcing_ladder_checks "
                        "WHERE tenant_id = :tenant AND case_id = :case ORDER BY rung"
                    ),
                    {"tenant": TENANT_A, "case": "case-ladder-audit"},
                )
            ).scalars()
            assert list(persisted_rungs) == [1, 2]
            await _seed_need_and_case(
                connection,
                tenant_id=TENANT_A,
                need_id="need-ladder-hit",
                case_id="case-ladder-hit",
            )
            hit = values | {
                "case": "case-ladder-hit",
                "check": "ladder-hit-1",
                "rung": 1,
                "outcome": "qualified_supply_found",
            }
            await connection.execute(text(insert_ladder_check), hit)
            await _expect_integrity(
                connection,
                insert_ladder_check,
                hit
                | {
                    "check": "ladder-hit-2",
                    "rung": 2,
                    "outcome": "no_qualified_supply",
                },
            )
            await _expect_integrity(
                connection,
                "UPDATE sourcing_ladder_checks SET conclusion = 'changed' "
                "WHERE tenant_id = :tenant AND check_id = 'ladder-check-1'",
                {"tenant": TENANT_A},
            )
            await _expect_integrity(
                connection,
                "DELETE FROM sourcing_ladder_checks "
                "WHERE tenant_id = :tenant AND check_id = 'ladder-check-1'",
                {"tenant": TENANT_A},
            )
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "upgrade", "head")


async def test_reconciliation_audit_rejects_updates_and_deletes(db_url: str) -> None:
    engine: AsyncEngine | None = None
    try:
        _run_alembic(db_url, "downgrade", "0046")
        _run_alembic(db_url, "upgrade", "0049")
        engine = create_engine_from(db_url)
        async with engine.begin() as connection:
            await _seed_public_search_execution(
                connection,
                tenant_id=TENANT_A,
                need_id="need-reconciliation-audit",
                case_id="case-reconciliation-audit",
                plan_id="plan-reconciliation-audit",
                run_id="run-reconciliation-audit",
                execution_id="execution-reconciliation-audit",
            )
            await _seed_tenant_evidence(connection, TENANT_A, ARTIFACT_A)
            await connection.execute(
                text(
                    "INSERT INTO sourcing_search_reconciliations "
                    "(tenant_id, reconciliation_id, execution_id, status, reason, "
                    "provider_usage_artifact_ref, created_at) "
                    "VALUES (:tenant, 'reconciliation-audit', :execution, 'required', "
                    "'provider result uncertain', :artifact, now())"
                ),
                {
                    "tenant": TENANT_A,
                    "execution": "execution-reconciliation-audit",
                    "artifact": ARTIFACT_A,
                },
            )
            await _expect_integrity(
                connection,
                "UPDATE sourcing_search_reconciliations "
                "SET status = 'confirmed_consumed', reconciled_by = 'employee-a', "
                "reconciled_at = now() WHERE tenant_id = :tenant "
                "AND reconciliation_id = 'reconciliation-audit'",
                {"tenant": TENANT_A},
            )
            await _expect_integrity(
                connection,
                "DELETE FROM sourcing_search_reconciliations WHERE tenant_id = :tenant "
                "AND reconciliation_id = 'reconciliation-audit'",
                {"tenant": TENANT_A},
            )
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "upgrade", "head")


async def test_review_alternates_are_unique_and_bound_to_case_and_tenant(
    db_url: str,
) -> None:
    engine: AsyncEngine | None = None
    insert_review = (
        "INSERT INTO sourcing_reviews "
        "(tenant_id, review_id, case_id, primary_option_id, primary_selection, "
        "alternate_option_ids, reason, expected_case_version, submitted_by, submitted_at) "
        "VALUES (:tenant, 'review-option-guards', :case, :primary, '{}', "
        "CAST(:alternates AS jsonb), 'best fit', 1, 'employee-a', now())"
    )
    try:
        _run_alembic(db_url, "downgrade", "0046")
        _run_alembic(db_url, "upgrade", "0049")
        engine = create_engine_from(db_url)
        async with engine.begin() as connection:
            await _seed_need_and_case(
                connection,
                tenant_id=TENANT_A,
                need_id="need-review-main",
                case_id="case-review-main",
            )
            await _seed_need_and_case(
                connection,
                tenant_id=TENANT_A,
                need_id="need-review-other",
                case_id="case-review-other",
            )
            await _seed_need_and_case(
                connection,
                tenant_id=TENANT_B,
                need_id="need-review-tenant",
                case_id="case-review-tenant",
            )
            await connection.execute(
                text(_INSERT_PRODUCT), _product_values("product-review-a")
            )
            await connection.execute(
                text(_INSERT_PRODUCT), _product_values("product-review-alt")
            )
            await connection.execute(
                text(_INSERT_PRODUCT),
                _product_values("product-review-b", tenant=TENANT_B),
            )
            for option_id, tenant_id, case_id, product_id in (
                (
                    "option-review-primary",
                    TENANT_A,
                    "case-review-main",
                    "product-review-a",
                ),
                (
                    "option-review-alt",
                    TENANT_A,
                    "case-review-main",
                    "product-review-alt",
                ),
                (
                    "option-review-other",
                    TENANT_A,
                    "case-review-other",
                    "product-review-a",
                ),
                (
                    "option-review-tenant",
                    TENANT_B,
                    "case-review-tenant",
                    "product-review-b",
                ),
            ):
                await connection.execute(
                    text(
                        "INSERT INTO sourcing_supply_options "
                        "(tenant_id, option_id, case_id, source, product_id, "
                        "is_qualified, created_at) VALUES (:tenant, :option, :case, "
                        "'existing_product', :product, true, now())"
                    ),
                    {
                        "tenant": tenant_id,
                        "option": option_id,
                        "case": case_id,
                        "product": product_id,
                    },
                )

            base_values: dict[str, object] = {
                "tenant": TENANT_A,
                "case": "case-review-main",
                "primary": "option-review-primary",
            }
            for alternates in (
                ["option-review-alt", "option-review-alt"],
                ["option-review-primary"],
                ["option-review-other"],
                ["option-review-tenant"],
            ):
                await _expect_integrity(
                    connection,
                    insert_review,
                    base_values | {"alternates": json.dumps(alternates)},
                )
            await connection.execute(
                text(insert_review),
                base_values | {"alternates": json.dumps(["option-review-alt"])},
            )
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "upgrade", "head")


async def test_supplier_price_history_is_append_only(db_url: str) -> None:
    engine: AsyncEngine | None = None
    try:
        _run_alembic(db_url, "downgrade", "0046")
        _run_alembic(db_url, "upgrade", "0049")
        engine = create_engine_from(db_url)
        async with engine.begin() as connection:
            await _seed_tenant_evidence(connection, TENANT_A, ARTIFACT_A)
            await connection.execute(
                text(
                    "INSERT INTO suppliers "
                    "(tenant_id, supplier_id, name, normalized_name, platform_refs, "
                    "capability_tags, verification, created_at) "
                    "VALUES (:tenant, 'supplier-a', 'Supplier A', 'supplier a', '[]', "
                    "'[\"hinges\"]', 'basic_checked', now())"
                ),
                {"tenant": TENANT_A},
            )
            await connection.execute(
                text(
                    "INSERT INTO supplier_price_records "
                    "(tenant_id, price_record_id, supplier_id, product_desc, quantity_tier, "
                    "unit_amount, currency, basis, artifact_id, observed_at) "
                    "VALUES (:tenant, 'price-a', 'supplier-a', 'steel hinge', 100, "
                    "1.25, 'USD', 'indicative', :artifact, now())"
                ),
                {"tenant": TENANT_A, "artifact": ARTIFACT_A},
            )
            await _expect_integrity(
                connection,
                "UPDATE supplier_price_records SET unit_amount = 1.30 "
                "WHERE tenant_id = :tenant AND price_record_id = 'price-a'",
                {"tenant": TENANT_A},
            )
            await _expect_integrity(
                connection,
                "DELETE FROM supplier_price_records "
                "WHERE tenant_id = :tenant AND price_record_id = 'price-a'",
                {"tenant": TENANT_A},
            )
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "upgrade", "head")


async def test_0049_upgrade_preserves_legacy_cost_sheet_created_at_0048(
    db_url: str,
) -> None:
    engine: AsyncEngine | None = None
    try:
        _run_alembic(db_url, "downgrade", "0048")
        engine = create_engine_from(db_url)
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM cost_sheets WHERE tenant_id = :tenant "
                    "AND cost_sheet_id = 'cost-legacy-before-0049'"
                ),
                {"tenant": TENANT_A},
            )
            await connection.execute(
                text(
                    "DELETE FROM opportunities WHERE tenant_id = :tenant "
                    "AND opportunity_id = 'opp-legacy-before-0049'"
                ),
                {"tenant": TENANT_A},
            )
            await connection.execute(
                text(
                    "INSERT INTO opportunities "
                    "(opportunity_id, tenant_id, account_id, account_name, country, "
                    "need_id, product_category) VALUES ('opp-legacy-before-0049', :tenant, "
                    "'account-legacy', 'Legacy buyer', 'US', 'need-legacy-0049', 'hinges')"
                ),
                {"tenant": TENANT_A},
            )
            await connection.execute(
                text(
                    "INSERT INTO cost_sheets "
                    "(tenant_id, cost_sheet_id, opportunity_id, version_type, "
                    "version_number, quantity, base_currency, quote_currency, created_at) "
                    "VALUES (:tenant, 'cost-legacy-before-0049', 'opp-legacy-before-0049', "
                    "'estimated', 1, 250, 'USD', 'USD', now())"
                ),
                {"tenant": TENANT_A},
            )
        await engine.dispose()
        engine = None

        _run_alembic(db_url, "upgrade", "0049")
        engine = create_engine_from(db_url)
        async with engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        "SELECT quantity, source_sourcing_case_id, source_option_id, "
                        "source_candidate_id FROM cost_sheets WHERE tenant_id = :tenant "
                        "AND cost_sheet_id = 'cost-legacy-before-0049'"
                    ),
                    {"tenant": TENANT_A},
                )
            ).one()
            assert row.quantity == 250
            assert row.source_sourcing_case_id is None
            assert row.source_option_id is None
            assert row.source_candidate_id is None
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "upgrade", "head")


async def _seed_0050_cost_origin(
    connection: AsyncConnection,
    *,
    tenant_id: str,
    suffix: str,
    source: str,
    raw_unit: str,
) -> dict[str, str | None]:
    need_id = f"need-0051-{suffix}"
    case_id = f"case-0051-{suffix}"
    candidate_id = f"candidate-0051-{suffix}" if source == "supplier_candidate" else None
    product_id = f"product-0051-{suffix}"
    option_id = f"option-0051-{suffix}"
    opportunity_id = f"opp-0051-{suffix}"
    cost_sheet_id = f"cost-0051-{suffix}"
    artifact_id = ARTIFACT_B if candidate_id is not None else ARTIFACT_A
    await _seed_need_and_case(
        connection, tenant_id=tenant_id, need_id=need_id, case_id=case_id
    )
    await _seed_tenant_evidence(connection, tenant_id, artifact_id)
    if candidate_id is None:
        await connection.execute(
            text(
                "INSERT INTO products "
                "(tenant_id, product_id, pool, name_zh, name_en, category, "
                "normalized_category, sellable_markets, selling_points, known_issues, "
                "customizable, internal_cost_amount, internal_cost_currency, "
                "internal_cost_basis, internal_cost_unit, internal_cost_source_ref, "
                "moq, created_at) VALUES (:tenant, :product, 'formal', '铰链', "
                "'Hinge', 'hinges', 'hinges', '[]', '[]', '[]', false, 1.25, "
                "'USD', 'supplier basis', :unit, :artifact, 100, now())"
            ),
            {
                "tenant": tenant_id,
                "product": product_id,
                "unit": raw_unit,
                "artifact": artifact_id,
            },
        )
    else:
        tiers = json.dumps(
            [
                {
                    "minimum_quantity": 100,
                    "amount": "1.250000000000",
                    "currency": "USD",
                    "unit": raw_unit,
                    "provenance": {},
                    "evidence_ref": artifact_id,
                }
            ]
        )
        await connection.execute(
            text(
                "INSERT INTO sourcing_candidates "
                "(tenant_id, candidate_id, case_id, supplier_name, product_title, "
                "observed_facts, supplier_claims, match_inferences, verified_specs, "
                "indicative_price_tiers, rejection_reasons, rejected, created_at) "
                "VALUES (:tenant, :candidate, :case, 'Supplier', 'Hinge', '{}', "
                "'{}', '{}', '[]', CAST(:tiers AS jsonb), '[]', false, now())"
            ),
            {
                "tenant": tenant_id,
                "candidate": candidate_id,
                "case": case_id,
                "tiers": tiers,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO products "
                "(tenant_id, product_id, pool, candidate_status, name_zh, name_en, "
                "category, normalized_category, moq, sellable_markets, customizable, "
                "selling_points, known_issues, created_at) VALUES "
                "(:tenant, :product, 'candidate', 'source_only', '铰链', 'Hinge', "
                "'hinges', 'hinges', 100, '[]', false, '[]', '[]', now())"
            ),
            {"tenant": tenant_id, "product": product_id},
        )
        await connection.execute(
            text(
                "INSERT INTO product_candidate_sources "
                "(tenant_id, product_id, sourcing_case_id, supplier_candidate_id, "
                "created_at) VALUES (:tenant, :product, :case, :candidate, now())"
            ),
            {
                "tenant": tenant_id,
                "product": product_id,
                "case": case_id,
                "candidate": candidate_id,
            },
        )
        await connection.execute(
            text(
                "INSERT INTO product_candidate_price_refs "
                "(tenant_id, product_id, minimum_quantity, unit_amount, currency, "
                "unit, artifact_id) VALUES (:tenant, :product, 100, 1.25, 'USD', "
                ":unit, :artifact)"
            ),
            {
                "tenant": tenant_id,
                "product": product_id,
                "unit": raw_unit,
                "artifact": artifact_id,
            },
        )
    await connection.execute(
        text(
            "INSERT INTO sourcing_supply_options "
            "(tenant_id, option_id, case_id, source, product_id, "
            "supplier_candidate_id, is_qualified, created_at) VALUES "
            "(:tenant, :option, :case, :source, :product, :candidate, true, now())"
        ),
        {
            "tenant": tenant_id,
            "option": option_id,
            "case": case_id,
            "source": source,
            "product": product_id,
            "candidate": candidate_id,
        },
    )
    await connection.execute(
        text(
            "INSERT INTO opportunities "
            "(opportunity_id, tenant_id, account_id, account_name, country, need_id, "
            "product_category) VALUES (:opportunity, :tenant, 'account-0051', "
            "'Migration buyer', 'US', :need, 'hinges')"
        ),
        {
            "tenant": tenant_id,
            "opportunity": opportunity_id,
            "need": need_id,
        },
    )
    await connection.execute(
        text(
            "INSERT INTO cost_sheets "
            "(tenant_id, cost_sheet_id, opportunity_id, version_type, version_number, "
            "quantity, base_currency, quote_currency, created_at, "
            "source_sourcing_case_id, source_option_id, source_candidate_id) VALUES "
            "(:tenant, :cost, :opportunity, 'estimated', 1, 100, 'USD', 'USD', "
            "now(), :case, :option, :candidate)"
        ),
        {
            "tenant": tenant_id,
            "cost": cost_sheet_id,
            "opportunity": opportunity_id,
            "case": case_id,
            "option": option_id,
            "candidate": candidate_id,
        },
    )
    await connection.execute(
        text(
            "INSERT INTO cost_items "
            "(tenant_id, cost_sheet_id, item_sequence, item_type, amount, currency, "
            "price_basis, is_per_unit, source_ref) VALUES "
            "(:tenant, :cost, 1, 'product_purchase', 1.25, 'USD', 'indicative', "
            "true, :artifact)"
        ),
        {"tenant": tenant_id, "cost": cost_sheet_id, "artifact": artifact_id},
    )
    return {
        "case_id": case_id,
        "candidate_id": candidate_id,
        "product_id": product_id,
        "option_id": option_id,
        "opportunity_id": opportunity_id,
        "cost_sheet_id": cost_sheet_id,
        "artifact_id": artifact_id,
        "raw_unit": raw_unit,
    }


async def _delete_0050_cost_origin(
    connection: AsyncConnection, *, tenant_id: str, origin: dict[str, str | None]
) -> None:
    values = {
        "tenant": tenant_id,
        "cost": origin["cost_sheet_id"],
        "opportunity": origin["opportunity_id"],
        "option": origin["option_id"],
        "product": origin["product_id"],
        "candidate": origin["candidate_id"],
        "case": origin["case_id"],
        "need": f"need-0051-{str(origin['case_id']).removeprefix('case-0051-')}",
    }
    for statement in (
        "DELETE FROM cost_items WHERE tenant_id=:tenant AND cost_sheet_id=:cost",
        "DELETE FROM cost_sheets WHERE tenant_id=:tenant AND cost_sheet_id=:cost",
        "DELETE FROM opportunities WHERE tenant_id=:tenant AND opportunity_id=:opportunity",
        "DELETE FROM sourcing_supply_options WHERE tenant_id=:tenant AND option_id=:option",
        "DELETE FROM product_candidate_price_refs WHERE tenant_id=:tenant AND product_id=:product",
        "DELETE FROM product_candidate_sources WHERE tenant_id=:tenant AND product_id=:product",
        "DELETE FROM products WHERE tenant_id=:tenant AND product_id=:product",
        "DELETE FROM sourcing_candidates WHERE tenant_id=:tenant AND candidate_id=:candidate",
        "DELETE FROM sourcing_cases WHERE tenant_id=:tenant AND case_id=:case",
        "DELETE FROM validated_needs WHERE tenant_id=:tenant AND need_id=:need",
    ):
        await connection.execute(text(statement), values)


def test_0051_source_unit_canonicalizer_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    migration = importlib.import_module(
        "migrations.versions.0051_sourcing_cost_product_origin"
    )

    for unsafe in ("\u2003", "ﬃ" * 17, None):
        with pytest.raises(RuntimeError, match="0051 cannot canonicalize source unit"):
            migration._canonical_source_unit(unsafe)

    def _cannot_normalize(_form: str, _value: str) -> str:
        raise UnicodeError("secret legacy value")

    monkeypatch.setattr(migration, "_normalize_unicode", _cannot_normalize)
    with pytest.raises(RuntimeError, match="0051 cannot canonicalize source unit") as exc:
        migration._canonical_source_unit("kg")
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


@pytest.mark.parametrize(
    ("source", "raw_unit"),
    (("existing_product", "\u2003"), ("supplier_candidate", "ﬃ" * 17)),
)
async def test_0051_upgrade_fails_closed_on_unsafe_legacy_unit(
    db_url: str, source: str, raw_unit: str
) -> None:
    engine: AsyncEngine | None = None
    origin: dict[str, str | None] | None = None
    suffix = f"x{uuid4().hex[:8]}"
    try:
        _run_alembic(db_url, "downgrade", "0050")
        engine = create_engine_from(db_url)
        async with engine.begin() as connection:
            origin = await _seed_0050_cost_origin(
                connection,
                tenant_id=TENANT_B,
                suffix=suffix,
                source=source,
                raw_unit=raw_unit,
            )
        await engine.dispose()
        engine = None

        result = _alembic_result(db_url, "upgrade", "0051")
        assert result.returncode != 0, "0051 必须拒绝不安全的历史 source_unit"
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "downgrade", "0050")
        if origin is not None:
            engine = create_engine_from(db_url)
            async with engine.begin() as connection:
                await _delete_0050_cost_origin(
                    connection, tenant_id=TENANT_B, origin=origin
                )
            await engine.dispose()
            engine = None
        _run_alembic(db_url, "upgrade", "head")


async def test_0051_backfills_unicode_units_hydrates_replays_and_roundtrips(
    db_url: str,
) -> None:
    """0051 必须按新写算法迁移两类来源，且升级后可精确重放。"""

    engine: AsyncEngine | None = None
    suffix = uuid4().hex[:8]
    try:
        _run_alembic(db_url, "downgrade", "0050")
        engine = create_engine_from(db_url)
        async with engine.begin() as connection:
            origins = (
                await _seed_0050_cost_origin(
                    connection,
                    tenant_id=TENANT_A,
                    suffix=f"e{suffix}",
                    source="existing_product",
                    raw_unit=" ㎏ ",
                ),
                await _seed_0050_cost_origin(
                    connection,
                    tenant_id=TENANT_A,
                    suffix=f"s{suffix}",
                    source="supplier_candidate",
                    raw_unit="Straße",
                ),
            )
        await engine.dispose()
        engine = None

        _run_alembic(db_url, "upgrade", "0051")
        engine = create_engine_from(db_url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        tenant = TenantId(TENANT_A)
        service = CostingServiceImpl(
            _CostingUowFactory(factory),
            Phase1CostingAuthorizer(tenant),
        )
        system_actor = CostingActor(
            "system:sourcing-cost", "system", CostingScope.SYSTEM, tenant
        )
        reader_actor = CostingActor(
            "employee-finance", "finance", CostingScope.TENANT
        )
        for origin, expected_unit in zip(origins, ("kg", "strasse"), strict=True):
            cost_sheet_id = CostSheetId(str(origin["cost_sheet_id"]))
            view = await service.get_sheet(tenant, cost_sheet_id, actor=reader_actor)
            assert view.source_unit == expected_unit
            assert view.source_tier_minimum_quantity == 100
            command = SourcingEstimateCreate(
                sourcing_case_id=SourcingCaseId(str(origin["case_id"])),
                primary_option_id=SourcingSupplyOptionId(str(origin["option_id"])),
                supplier_candidate_id=(
                    SupplierCandidateId(str(origin["candidate_id"]))
                    if origin["candidate_id"] is not None
                    else None
                ),
                product_id=ProductId(str(origin["product_id"])),
                opportunity_id=OpportunityId(str(origin["opportunity_id"])),
                quantity=100,
                minimum_quantity=100,
                unit_amount=Decimal("1.250000000000"),
                currency="USD",
                unit=str(origin["raw_unit"]),
                evidence_ref=ArtifactId(str(origin["artifact_id"])),
            )
            assert (
                await service.create_sourcing_estimate(
                    tenant, command, actor=system_actor
                )
                == cost_sheet_id
            )
        await engine.dispose()
        engine = None

        _run_alembic(db_url, "downgrade", "0050")
        engine = create_engine_from(db_url)
        async with engine.connect() as connection:
            for origin in origins:
                row = (
                    await connection.execute(
                        text(
                            "SELECT source_sourcing_case_id, source_option_id "
                            "FROM cost_sheets WHERE tenant_id = :tenant "
                            "AND cost_sheet_id = :cost"
                        ),
                        {"tenant": TENANT_A, "cost": origin["cost_sheet_id"]},
                    )
                ).one()
                assert (row.source_sourcing_case_id, row.source_option_id) == (
                    origin["case_id"],
                    origin["option_id"],
                )
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "upgrade", "head")


async def test_0050_downgrade_normalizes_all_new_page_categories(db_url: str) -> None:
    engine: AsyncEngine | None = None
    tenant = "tn_page_category_downgrade"
    categories = ("page_access_forbidden", "login_or_captcha", "unsafe_redirect")
    try:
        _run_alembic(db_url, "upgrade", "head")
        engine = create_engine_from(db_url)
        async with engine.begin() as connection:
            for index, category in enumerate(categories):
                call_id = f"tcl_page_category_{index}"
                await connection.execute(
                    text(
                        "INSERT INTO tool_calls (tenant_id,tool_call_id,tool_id,tool_version,risk_level,cost_class,idempotency_key,request_fingerprint,fingerprint_version,status,attempt_count,user_id,error_category,created_at,updated_at,completed_at) "
                        "VALUES (:tenant,:call,'web.page.read','v1','low','free',:key,repeat(:digit,64),'v1','failed_permanent',1,'usr_test',:category,now(),now(),now())"
                    ),
                    {
                        "tenant": tenant,
                        "call": call_id,
                        "key": f"page-category-{index}",
                        "digit": str(index + 1),
                        "category": category,
                    },
                )
                await connection.execute(
                    text(
                        "INSERT INTO tool_call_events (tenant_id,event_id,tool_call_id,stage,outcome,actor_id,occurred_at,duration_ms,category) "
                        "VALUES (:tenant,:event,:call,'handler','failed','usr_test',now(),0,:category)"
                    ),
                    {
                        "tenant": tenant,
                        "event": f"tce_page_category_{index}",
                        "call": call_id,
                        "category": category,
                    },
                )
        await engine.dispose()
        engine = None
        _run_alembic(db_url, "downgrade", "0049")
        engine = create_engine_from(db_url)
        async with engine.connect() as connection:
            call_values = set(
                (
                    await connection.execute(
                        text(
                            "SELECT error_category FROM tool_calls WHERE tenant_id=:tenant"
                        ),
                        {"tenant": tenant},
                    )
                ).scalars()
            )
            event_values = set(
                (
                    await connection.execute(
                        text(
                            "SELECT category FROM tool_call_events WHERE tenant_id=:tenant"
                        ),
                        {"tenant": tenant},
                    )
                ).scalars()
            )
        assert call_values == event_values == {"provider_permanent"}
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "upgrade", "head")
