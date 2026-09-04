"""Catalog Product Proposal 的 tenant-safe PostgreSQL schema 契约。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError

_ROOT = Path(__file__).resolve().parents[2]
_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
_TABLES = (
    "catalog_proposal_policy_versions",
    "catalog_proposal_evaluations",
    "catalog_product_proposals",
    "catalog_cultivation_cases",
)


def _alembic(db_url: str, *args: str, succeeds: bool = True) -> None:
    result = subprocess.run(
        [sys.executable, "scripts/run_alembic.py", *args],
        cwd=_ROOT,
        env={**os.environ, "DATABASE_URL": db_url},
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) is succeeds, (
        f"alembic {' '.join(args)} 返回状态不符合预期（输出已隐藏）"
    )


async def _contract(engine, table_name: str) -> dict[str, object]:
    async with engine.connect() as connection:
        return await connection.run_sync(
            lambda sync: {
                "columns": {
                    str(column["name"]): {
                        "nullable": bool(column["nullable"]),
                        "type": str(column["type"].compile(dialect=engine.dialect)),
                    }
                    for column in inspect(sync).get_columns(table_name)
                },
                "checks": {
                    str(item["name"])
                    for item in inspect(sync).get_check_constraints(table_name)
                },
                "fks": {
                    (
                        tuple(str(value) for value in item["constrained_columns"]),
                        str(item["referred_table"]),
                        tuple(str(value) for value in item["referred_columns"]),
                    )
                    for item in inspect(sync).get_foreign_keys(table_name)
                },
                "indexes": {
                    str(item["name"]): {
                        "unique": bool(item["unique"]),
                        "columns": tuple(str(value) for value in item["column_names"]),
                        "where": str(
                            item.get("dialect_options", {}).get("postgresql_where", "")
                        ),
                    }
                    for item in inspect(sync).get_indexes(table_name)
                },
                "uniques": {
                    str(item["name"]): tuple(
                        str(value) for value in item["column_names"]
                    )
                    for item in inspect(sync).get_unique_constraints(table_name)
                },
            }
        )


async def test_0057_upgrade_from_prior_head_has_tenant_bound_schema_and_orm_parity(
    db_url: str,
) -> None:
    """漏表、漏列、单列 FK 或 ORM 漂移都会破坏后续 tenant-bound repository。"""
    from infra.db.session import create_engine_from

    try:
        _alembic(db_url, "downgrade", "0056")
        engine = create_engine_from(db_url)
        try:
            async with engine.connect() as connection:
                names = set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))
                columns = {
                    str(item["name"])
                    for item in await connection.run_sync(
                        lambda sync: inspect(sync).get_columns("validated_needs")
                    )
                }
            assert not set(_TABLES) & names
            assert "recurring_requirement" not in columns
        finally:
            await engine.dispose()

        _alembic(db_url, "upgrade", "0057")
        engine = create_engine_from(db_url)
        try:
            expected_columns = {
                "catalog_proposal_policy_versions": {
                    "tenant_id", "policy_version_id", "content", "content_hash",
                    "base_active_version_id", "proposed_by", "creation_key",
                    "creation_request_hash", "approval_id", "state", "created_at",
                    "activated_at", "terminal_at",
                },
                "catalog_proposal_evaluations": {
                    "tenant_id", "evaluation_id", "cluster_id", "policy_version_id",
                    "facts_hash", "facts", "rule_results", "overall_passed",
                    "blocked_reason", "proposed_by_run", "created_at",
                },
                "catalog_product_proposals": {
                    "tenant_id", "proposal_id", "evaluation_id", "cluster_id",
                    "policy_version_id", "facts_hash", "owner_employee",
                    "proposed_by_run", "approval_id", "approval_request_hash",
                    "state", "created_at", "updated_at",
                },
                "catalog_cultivation_cases": {
                    "tenant_id", "cultivation_case_id", "proposal_id", "approval_id",
                    "cluster_id", "policy_version_id", "facts_hash", "evidence_refs",
                    "state", "queued_at",
                },
            }
            contracts = {table: await _contract(engine, table) for table in _TABLES}
            for table, columns in expected_columns.items():
                assert set(contracts[table]["columns"]) == columns
                assert "tenant_id" in columns

            need_contract = await _contract(engine, "validated_needs")
            recurring = need_contract["columns"]["recurring_requirement"]
            assert recurring == {"nullable": True, "type": "JSONB"}
            assert "ck_validated_needs_recurring_requirement_jsonb" in need_contract["checks"]

            assert (("tenant_id", "base_active_version_id"), "catalog_proposal_policy_versions", ("tenant_id", "policy_version_id")) in contracts["catalog_proposal_policy_versions"]["fks"]
            assert (("tenant_id", "approval_id"), "approval_packages", ("tenant_id", "approval_id")) in contracts["catalog_proposal_policy_versions"]["fks"]
            assert (("tenant_id", "cluster_id"), "need_clusters", ("tenant_id", "cluster_id")) in contracts["catalog_proposal_evaluations"]["fks"]
            assert (("tenant_id", "policy_version_id"), "catalog_proposal_policy_versions", ("tenant_id", "policy_version_id")) in contracts["catalog_proposal_evaluations"]["fks"]
            assert (("tenant_id", "proposed_by_run"), "workflow_runs", ("tenant_id", "run_id")) in contracts["catalog_proposal_evaluations"]["fks"]
            assert (("tenant_id", "owner_employee"), "employees", ("tenant_id", "employee_id")) in contracts["catalog_product_proposals"]["fks"]
            assert (("tenant_id", "approval_id"), "approval_packages", ("tenant_id", "approval_id")) in contracts["catalog_product_proposals"]["fks"]
            assert (("tenant_id", "proposal_id", "cluster_id", "policy_version_id", "facts_hash"), "catalog_product_proposals", ("tenant_id", "proposal_id", "cluster_id", "policy_version_id", "facts_hash")) in contracts["catalog_cultivation_cases"]["fks"]

            assert contracts["catalog_proposal_policy_versions"]["indexes"]["uq_catalog_policy_active"]["unique"] is True
            assert "active" in contracts["catalog_proposal_policy_versions"]["indexes"]["uq_catalog_policy_active"]["where"]
            assert contracts["catalog_proposal_policy_versions"]["indexes"]["uq_catalog_policy_approval"]["unique"] is True
            assert "approval_id" in contracts["catalog_proposal_policy_versions"]["indexes"]["uq_catalog_policy_approval"]["where"]
            assert contracts["catalog_product_proposals"]["indexes"]["uq_catalog_product_proposal_approval"]["unique"] is True
            assert "approval_id" in contracts["catalog_product_proposals"]["indexes"]["uq_catalog_product_proposal_approval"]["where"]
            assert contracts["catalog_proposal_policy_versions"]["uniques"]["uq_catalog_policy_creation_key"] == ("tenant_id", "proposed_by", "creation_key")
            assert contracts["catalog_proposal_evaluations"]["uniques"]["uq_catalog_evaluation_facts"] == ("tenant_id", "cluster_id", "policy_version_id", "facts_hash")
            assert contracts["catalog_product_proposals"]["uniques"]["uq_catalog_product_proposal_evaluation"] == ("tenant_id", "evaluation_id")
            assert contracts["catalog_cultivation_cases"]["uniques"]["uq_catalog_cultivation_proposal"] == ("tenant_id", "proposal_id")
            assert contracts["catalog_cultivation_cases"]["uniques"]["uq_catalog_cultivation_approval"] == ("tenant_id", "approval_id")

            from infra.db.tables import (
                CatalogCultivationCaseRow,
                CatalogProductProposalRow,
                CatalogProposalEvaluationRow,
                CatalogProposalPolicyVersionRow,
                ValidatedNeedRow,
            )
            rows = {
                "catalog_proposal_policy_versions": CatalogProposalPolicyVersionRow,
                "catalog_proposal_evaluations": CatalogProposalEvaluationRow,
                "catalog_product_proposals": CatalogProductProposalRow,
                "catalog_cultivation_cases": CatalogCultivationCaseRow,
            }
            for table, row in rows.items():
                assert set(row.__table__.columns.keys()) == expected_columns[table]
                assert all(
                    len(constraint.columns) >= 2
                    for constraint in row.__table__.constraints
                    if isinstance(constraint, ForeignKeyConstraint)
                )
            assert "recurring_requirement" in ValidatedNeedRow.__table__.columns
            assert any(
                isinstance(item, CheckConstraint)
                and item.name == "ck_validated_needs_recurring_requirement_jsonb"
                for item in ValidatedNeedRow.__table__.constraints
            )
        finally:
            await engine.dispose()
    finally:
        _alembic(db_url, "upgrade", "head")


async def _seed_parents(connection, *, tenant: str, suffix: str) -> None:
    await connection.execute(
        text("INSERT INTO employees (employee_id,tenant_id,name,role) VALUES (:employee,:tenant,'Owner','boss')"),
        {"employee": f"emp_{suffix}", "tenant": tenant},
    )
    await connection.execute(
        text("INSERT INTO need_clusters (tenant_id,cluster_id,category,keywords,countries,created_at,updated_at) VALUES (:tenant,:cluster,'hardware','[]'::jsonb,'[]'::jsonb,:now,:now)"),
        {"tenant": tenant, "cluster": f"ncl_{suffix}", "now": _NOW},
    )
    await connection.execute(
        text("INSERT INTO workflow_runs (run_id,tenant_id,workflow_type,workflow_version,subject_ref,current_step,status,context,idempotency_key) VALUES (:run,:tenant,'catalog_product_proposal',1,:cluster,'evaluate','running','{}'::jsonb,:key)"),
        {"run": f"run_{suffix}", "tenant": tenant, "cluster": f"ncl_{suffix}", "key": f"catalog-test:{suffix}"},
    )


def _policy_values(*, tenant: str, suffix: str, policy: str | None = None) -> dict[str, object]:
    return {
        "tenant": tenant,
        "policy": policy or f"cpv_{suffix}",
        "hash": "a" * 64,
        "employee": f"emp_{suffix}",
        "key": f"create-{suffix}",
        "request_hash": "b" * 64,
        "now": _NOW,
    }


_INSERT_POLICY = text(
    "INSERT INTO catalog_proposal_policy_versions "
    "(tenant_id,policy_version_id,content,content_hash,base_active_version_id,"
    "proposed_by,creation_key,creation_request_hash,approval_id,state,created_at,"
    "activated_at,terminal_at) VALUES "
    "(:tenant,:policy,'{}'::jsonb,:hash,NULL,:employee,:key,:request_hash,NULL,"
    "'pending_approval',:now,NULL,NULL)"
)


async def _delete_catalog_rows(connection, *, tenant: str, other: str | None = None) -> None:
    params = {"tenant": tenant, "other": other or tenant}
    triggers = {
        "catalog_cultivation_cases": "trg_catalog_cultivation_immutable",
        "catalog_product_proposals": "trg_catalog_product_proposal_guard",
        "catalog_proposal_evaluations": "trg_catalog_evaluation_immutable",
        "catalog_proposal_policy_versions": "trg_catalog_policy_version_guard",
    }
    for table in reversed(_TABLES):
        await connection.execute(text(f"ALTER TABLE {table} DISABLE TRIGGER {triggers[table]}"))
        await connection.execute(
            text(f"DELETE FROM {table} WHERE tenant_id IN (:tenant,:other)"), params
        )
        await connection.execute(text(f"ALTER TABLE {table} ENABLE TRIGGER {triggers[table]}"))


async def test_0057_rejects_malformed_shapes_cross_tenant_links_and_mutation(
    db_url: str,
) -> None:
    """坏 hash/JSON/state、跨租户引用和重写不可变 subject 必须在 DB 被拒。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    tenant = "tn_catalog_guard"
    other = "tn_catalog_other"
    try:
        async with engine.begin() as connection:
            await _seed_parents(connection, tenant=tenant, suffix="catalog_guard")
            await _seed_parents(connection, tenant=other, suffix="catalog_other")

        bad_values = _policy_values(tenant=tenant, suffix="catalog_guard") | {"hash": "BAD"}
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(_INSERT_POLICY, bad_values)

        async with engine.begin() as connection:
            await connection.execute(_INSERT_POLICY, _policy_values(tenant=tenant, suffix="catalog_guard"))

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("INSERT INTO catalog_proposal_evaluations (tenant_id,evaluation_id,cluster_id,policy_version_id,facts_hash,facts,rule_results,overall_passed,blocked_reason,proposed_by_run,created_at) VALUES (:tenant,'cpe_bad',:cluster,:policy,:hash,'[]'::jsonb,'{}'::jsonb,true,NULL,:run,:now)"),
                    {"tenant": tenant, "cluster": "ncl_catalog_guard", "policy": "cpv_catalog_guard", "hash": "c" * 64, "run": "run_catalog_guard", "now": _NOW},
                )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("INSERT INTO catalog_proposal_evaluations (tenant_id,evaluation_id,cluster_id,policy_version_id,facts_hash,facts,rule_results,overall_passed,blocked_reason,proposed_by_run,created_at) VALUES (:tenant,'cpe_cross',:cluster,:policy,:hash,'{}'::jsonb,'[]'::jsonb,true,NULL,:run,:now)"),
                    {"tenant": tenant, "cluster": "ncl_catalog_other", "policy": "cpv_catalog_guard", "hash": "c" * 64, "run": "run_catalog_guard", "now": _NOW},
                )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("INSERT INTO catalog_proposal_evaluations (tenant_id,evaluation_id,cluster_id,policy_version_id,facts_hash,facts,rule_results,overall_passed,blocked_reason,proposed_by_run,created_at) VALUES (:tenant,'cpe_fractional',:cluster,:policy,:hash,CAST(:facts AS jsonb),'[]'::jsonb,true,NULL,:run,:now)"),
                    {"tenant": tenant, "cluster": "ncl_catalog_guard", "policy": "cpv_catalog_guard", "hash": "f" * 64, "facts": json.dumps({"safe_total_quantity": 1.5}), "run": "run_catalog_guard", "now": _NOW},
                )

        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO catalog_proposal_evaluations (tenant_id,evaluation_id,cluster_id,policy_version_id,facts_hash,facts,rule_results,overall_passed,blocked_reason,proposed_by_run,created_at) VALUES (:tenant,'cpe_catalog_guard',:cluster,:policy,:hash,'{}'::jsonb,'[]'::jsonb,true,NULL,:run,:now)"),
                {"tenant": tenant, "cluster": "ncl_catalog_guard", "policy": "cpv_catalog_guard", "hash": "c" * 64, "run": "run_catalog_guard", "now": _NOW},
            )
            await connection.execute(
                text("INSERT INTO catalog_product_proposals (tenant_id,proposal_id,evaluation_id,cluster_id,policy_version_id,facts_hash,owner_employee,proposed_by_run,approval_id,approval_request_hash,state,created_at,updated_at) VALUES (:tenant,'cpr_catalog_guard','cpe_catalog_guard',:cluster,:policy,:hash,:employee,:run,NULL,NULL,'awaiting_approval_submission',:now,:now)"),
                {"tenant": tenant, "cluster": "ncl_catalog_guard", "policy": "cpv_catalog_guard", "hash": "c" * 64, "employee": "emp_catalog_guard", "run": "run_catalog_guard", "now": _NOW},
            )

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(text("UPDATE catalog_proposal_evaluations SET facts_hash=:hash WHERE tenant_id=:tenant AND evaluation_id='cpe_catalog_guard'"), {"hash": "d" * 64, "tenant": tenant})
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(text("UPDATE catalog_product_proposals SET cluster_id='ncl_catalog_other' WHERE tenant_id=:tenant AND proposal_id='cpr_catalog_guard'"), {"tenant": tenant})
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE catalog_proposal_policy_versions SET content=CAST(:content AS jsonb) WHERE tenant_id=:tenant AND policy_version_id='cpv_catalog_guard'"),
                    {"tenant": tenant, "content": json.dumps({"changed": True})},
                )
    finally:
        async with engine.begin() as connection:
            await _delete_catalog_rows(connection, tenant=tenant, other=other)
            await connection.execute(text("DELETE FROM workflow_runs WHERE tenant_id IN (:tenant,:other)"), {"tenant": tenant, "other": other})
            await connection.execute(text("DELETE FROM need_clusters WHERE tenant_id IN (:tenant,:other)"), {"tenant": tenant, "other": other})
            await connection.execute(text("DELETE FROM employees WHERE tenant_id IN (:tenant,:other)"), {"tenant": tenant, "other": other})
        await engine.dispose()


async def test_0057_concurrent_unique_keys_converge_to_one_row(db_url: str) -> None:
    """两个真实事务竞争活动策略和 evaluation 业务键时各只能提交一个。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    tenant = "tn_catalog_concurrency"
    try:
        async with engine.begin() as connection:
            await _seed_parents(connection, tenant=tenant, suffix="catalog_concurrency")
            await connection.execute(_INSERT_POLICY, _policy_values(tenant=tenant, suffix="catalog_concurrency"))
            for label in ("a", "b"):
                policy = f"cpv_active_{label}"
                content_hash = "a" * 64
                request_hash = ("1" if label == "a" else "2") * 64
                payload = {
                    "schema_version": "catalog-policy-v1",
                    "tenant_id": tenant,
                    "approval_type": "catalog_proposal_policy_change",
                    "policy_version_id": policy,
                    "content_hash": content_hash,
                    "request_hash": request_hash,
                }
                await connection.execute(
                    text("INSERT INTO approval_packages (tenant_id,approval_id,approval_type,title,proposed_change,reason,blast_radius,created_at,expires_at,state,proposed_by_employee,evidence_refs,change_set_ref,owner_employee,contract_namespace,request_hash,expires_at_limit) VALUES (:tenant,:approval,'catalog_proposal_policy_change','Catalog policy',CAST(:payload AS jsonb),'internal only','{}'::jsonb,:now,:expires,'pending',:employee,'[]'::jsonb,:change_set,:employee,'catalog-policy-v1',:request_hash,:expires)"),
                    {"tenant": tenant, "approval": f"apr_active_{label}", "payload": json.dumps(payload), "now": _NOW, "expires": _NOW + timedelta(days=7), "employee": "emp_catalog_concurrency", "change_set": f"catalog-policy:{policy}:{content_hash}", "request_hash": request_hash},
                )

        async def insert_active(policy: str, key: str) -> bool:
            try:
                async with engine.begin() as connection:
                    label = policy[-1]
                    await connection.execute(
                        text("INSERT INTO catalog_proposal_policy_versions (tenant_id,policy_version_id,content,content_hash,base_active_version_id,proposed_by,creation_key,creation_request_hash,approval_id,state,created_at,activated_at,terminal_at) VALUES (:tenant,:policy,'{}'::jsonb,:hash,NULL,:employee,:key,:request_hash,:approval,'active',:now,:now,NULL)"),
                        {"tenant": tenant, "policy": policy, "hash": "a" * 64, "employee": "emp_catalog_concurrency", "key": key, "request_hash": ("1" if label == "a" else "2") * 64, "approval": f"apr_active_{label}", "now": _NOW},
                    )
                return True
            except IntegrityError:
                return False

        assert sorted(await asyncio.gather(insert_active("cpv_active_a", "active-a"), insert_active("cpv_active_b", "active-b"))) == [False, True]

        async def insert_evaluation(evaluation: str) -> bool:
            try:
                async with engine.begin() as connection:
                    await connection.execute(
                        text("INSERT INTO catalog_proposal_evaluations (tenant_id,evaluation_id,cluster_id,policy_version_id,facts_hash,facts,rule_results,overall_passed,blocked_reason,proposed_by_run,created_at) VALUES (:tenant,:evaluation,:cluster,:policy,:hash,'{}'::jsonb,'[]'::jsonb,true,NULL,:run,:now)"),
                        {"tenant": tenant, "evaluation": evaluation, "cluster": "ncl_catalog_concurrency", "policy": "cpv_catalog_concurrency", "hash": "e" * 64, "run": "run_catalog_concurrency", "now": _NOW},
                    )
                return True
            except IntegrityError:
                return False

        assert sorted(await asyncio.gather(insert_evaluation("cpe_concurrent_a"), insert_evaluation("cpe_concurrent_b"))) == [False, True]
    finally:
        async with engine.begin() as connection:
            await _delete_catalog_rows(connection, tenant=tenant)
            await connection.execute(text("DELETE FROM workflow_runs WHERE tenant_id=:tenant"), {"tenant": tenant})
            await connection.execute(text("DELETE FROM need_clusters WHERE tenant_id=:tenant"), {"tenant": tenant})
            await connection.execute(text("ALTER TABLE approval_packages DISABLE TRIGGER trg_approval_packages_guard"))
            await connection.execute(text("DELETE FROM approval_packages WHERE tenant_id=:tenant"), {"tenant": tenant})
            await connection.execute(text("ALTER TABLE approval_packages ENABLE TRIGGER trg_approval_packages_guard"))
            await connection.execute(text("DELETE FROM employees WHERE tenant_id=:tenant"), {"tenant": tenant})
        await engine.dispose()


async def test_0057_catalog_approval_namespaces_are_strict(db_url: str) -> None:
    """Catalog 审批只接受精确 namespace、type、subject、引用和 request hash 绑定。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    tenant = "tn_catalog_approval"
    policy = "cpv_catalog_approval"
    change_set = f"catalog-policy:{policy}:{'a' * 64}"
    payload = (
        '{"schema_version":"catalog-policy-v1","tenant_id":"' + tenant
        + '","approval_type":"catalog_proposal_policy_change","policy_version_id":"'
        + policy + '","content_hash":"' + "a" * 64 + '","request_hash":"'
        + "b" * 64 + '"}'
    )
    insert = text(
        "INSERT INTO approval_packages (tenant_id,approval_id,approval_type,title,proposed_change,reason,blast_radius,created_at,expires_at,state,proposed_by_employee,evidence_refs,change_set_ref,owner_employee,contract_namespace,request_hash,expires_at_limit) VALUES (:tenant,:approval,:approval_type,'Catalog policy',CAST(:payload AS jsonb),'internal only','{}'::jsonb,:now,:expires,'pending',:employee,'[]'::jsonb,:change_set,:employee,:namespace,:request_hash,:limit)"
    )
    values = {"tenant": tenant, "approval": "apr_catalog_policy", "approval_type": "catalog_proposal_policy_change", "payload": payload, "now": _NOW, "expires": _NOW + timedelta(days=7), "employee": "emp_catalog_approval", "change_set": change_set, "namespace": "catalog-policy-v1", "request_hash": "b" * 64, "limit": _NOW + timedelta(days=7)}
    try:
        async with engine.begin() as connection:
            await connection.execute(text("INSERT INTO employees (employee_id,tenant_id,name,role) VALUES (:employee,:tenant,'Owner','boss')"), {"employee": values["employee"], "tenant": tenant})
        for mutation in (
            {"namespace": "catalog-policy-v2"},
            {"approval_type": "catalog_product_cultivation"},
            {"change_set": change_set + ":wrong"},
            {"request_hash": "c" * 64},
        ):
            with pytest.raises(IntegrityError):
                async with engine.begin() as connection:
                    await connection.execute(insert, values | mutation | {"approval": f"apr_bad_{len(mutation)}_{mutation.get('namespace', mutation.get('approval_type', 'field'))}"})
        async with engine.begin() as connection:
            await connection.execute(insert, values)
            cultivation_payload = {
                "schema_version": "catalog-cultivation-v1",
                "tenant_id": tenant,
                "approval_type": "catalog_product_cultivation",
                "proposal_id": "cpr_catalog_approval",
                "policy_version_id": policy,
                "facts_hash": "c" * 64,
                "request_hash": "d" * 64,
            }
            await connection.execute(
                insert,
                values
                | {
                    "approval": "apr_catalog_cultivation",
                    "approval_type": "catalog_product_cultivation",
                    "payload": json.dumps(cultivation_payload),
                    "expires": _NOW + timedelta(days=3),
                    "change_set": f"catalog-cultivation:cpr_catalog_approval:{policy}:{'c' * 64}",
                    "namespace": "catalog-cultivation-v1",
                    "request_hash": "d" * 64,
                    "limit": _NOW + timedelta(days=3),
                },
            )
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("ALTER TABLE approval_packages DISABLE TRIGGER trg_approval_packages_guard"))
            await connection.execute(text("DELETE FROM approval_packages WHERE tenant_id=:tenant"), {"tenant": tenant})
            await connection.execute(text("ALTER TABLE approval_packages ENABLE TRIGGER trg_approval_packages_guard"))
            await connection.execute(text("DELETE FROM employees WHERE tenant_id=:tenant"), {"tenant": tenant})
        await engine.dispose()


async def test_0057_downgrade_refuses_catalog_or_recurring_fact_data(db_url: str) -> None:
    """降级不得静默删除 Catalog 行或已核实的复购事实。"""
    from infra.db.session import create_engine_from

    tenant = "tn_catalog_downgrade"
    engine = create_engine_from(db_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("INSERT INTO validated_needs (tenant_id,need_id,account_id,product_category,source_message_id,status,created_at,recurring_requirement) VALUES (:tenant,'vnd_catalog_downgrade','acc_catalog','{}'::jsonb,'msg_catalog','validated',:now,CAST(:recurring AS jsonb))"),
                {"tenant": tenant, "now": _NOW, "recurring": json.dumps({"value": True})},
            )
        _alembic(db_url, "downgrade", "0056", succeeds=False)
        async with engine.begin() as connection:
            await connection.execute(text("DELETE FROM validated_needs WHERE tenant_id=:tenant"), {"tenant": tenant})
            await _seed_parents(connection, tenant=tenant, suffix="catalog_downgrade")
            await connection.execute(_INSERT_POLICY, _policy_values(tenant=tenant, suffix="catalog_downgrade"))
        _alembic(db_url, "downgrade", "0056", succeeds=False)
        async with engine.begin() as connection:
            await _delete_catalog_rows(connection, tenant=tenant)
            await connection.execute(text("DELETE FROM workflow_runs WHERE tenant_id=:tenant"), {"tenant": tenant})
            await connection.execute(text("DELETE FROM need_clusters WHERE tenant_id=:tenant"), {"tenant": tenant})
            await connection.execute(text("DELETE FROM employees WHERE tenant_id=:tenant"), {"tenant": tenant})
        await engine.dispose()
        _alembic(db_url, "downgrade", "0056")
        _alembic(db_url, "upgrade", "0057")
    finally:
        await engine.dispose()
        _alembic(db_url, "upgrade", "head")
