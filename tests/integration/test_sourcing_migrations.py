"""Sourcing V2、三个供给池与成本来源的数据库迁移合同。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from infra.db.session import create_engine_from
from infra.db.tables import Base

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
    "sourcing_search_reconciliations",
}
SUPPLY_TABLES = {
    "products",
    "product_variants",
    "supply_capabilities",
    "product_candidate_sources",
    "product_candidate_price_refs",
    "suppliers",
    "supplier_price_records",
}
ALL_TABLES = SOURCING_TABLES | SUPPLY_TABLES

TENANT_A = "tn_0" + "A" * 25
TENANT_B = "tn_0" + "B" * 25
ARTIFACT_A = "art_0" + "C" * 25
ARTIFACT_B = "art_0" + "D" * 25


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
            "hash": "e" * 64,
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


async def test_sourcing_and_supply_schema_is_tenant_bound_and_uses_exact_amounts(
    db_url: str,
) -> None:
    engine: AsyncEngine | None = None
    try:
        _run_alembic(db_url, "downgrade", "0046")
        _run_alembic(db_url, "upgrade", "0049")
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
            "source_candidate_id",
        } <= set(Base.metadata.tables["cost_sheets"].columns.keys())

        candidate_fks = contract["sourcing_candidate_evidence"]["foreign_keys"]
        assert candidate_fks["fk_sourcing_candidate_evidence_artifact"] == (
            ("tenant_id", "artifact_id"),
            "raw_artifacts",
            ("tenant_id", "artifact_id"),
        )
        source_fks = contract["product_candidate_sources"]["foreign_keys"]
        assert source_fks["fk_product_candidate_sources_candidate"] == (
            ("tenant_id", "sourcing_case_id", "supplier_candidate_id"),
            "sourcing_candidates",
            ("tenant_id", "case_id", "candidate_id"),
        )
        cost_fks = contract["cost_sheets"]["foreign_keys"]
        assert cost_fks["fk_cost_sheets_sourcing_option"] == (
            ("tenant_id", "source_sourcing_case_id", "source_option_id"),
            "sourcing_supply_options",
            ("tenant_id", "case_id", "option_id"),
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
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "upgrade", "head")


async def test_active_case_plan_review_and_cost_origin_constraints(db_url: str) -> None:
    engine: AsyncEngine | None = None
    try:
        _run_alembic(db_url, "downgrade", "0046")
        _run_alembic(db_url, "upgrade", "0049")
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
                    "INSERT INTO products "
                    "(tenant_id, product_id, pool, name_zh, name_en, category, normalized_category, "
                    "sellable_markets, selling_points, known_issues, customizable, created_at) "
                    "VALUES (:tenant, 'product-a', 'formal', '铰链', 'Hinge', 'hinges', 'hinges', "
                    "'[]', '[]', '[]', false, now())"
                ),
                {"tenant": TENANT_A},
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
            cost_values = {
                "tenant": TENANT_A,
                "case": "case-a",
                "option": "option-a",
            }
            await connection.execute(
                text(
                    "INSERT INTO cost_sheets "
                    "(tenant_id, cost_sheet_id, opportunity_id, version_type, version_number, quantity, "
                    "base_currency, quote_currency, created_at, source_sourcing_case_id, source_option_id) "
                    "VALUES (:tenant, 'cost-a', 'opp-a', 'estimated', 1, 100, 'USD', 'USD', now(), :case, :option)"
                ),
                cost_values,
            )
            await _expect_integrity(
                connection,
                "INSERT INTO cost_sheets "
                "(tenant_id, cost_sheet_id, opportunity_id, version_type, version_number, quantity, "
                "base_currency, quote_currency, created_at, source_sourcing_case_id, source_option_id) "
                "VALUES (:tenant, 'cost-b', 'opp-a', 'estimated', 2, 100, 'USD', 'USD', now(), :case, :option)",
                cost_values,
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
    finally:
        if engine is not None:
            await engine.dispose()
        _run_alembic(db_url, "upgrade", "head")
