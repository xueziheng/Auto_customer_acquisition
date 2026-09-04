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


async def _insert_catalog_approval(
    connection,
    *,
    tenant: str,
    approval_id: str,
    namespace: str,
    policy_id: str,
    content_hash: str = "a" * 64,
    proposal_id: str | None = None,
    facts_hash: str | None = None,
    request_hash: str = "d" * 64,
    state: str = "approved",
) -> None:
    if namespace == "catalog-policy-v1":
        approval_type = "catalog_proposal_policy_change"
        payload = {
            "schema_version": namespace,
            "tenant_id": tenant,
            "approval_type": approval_type,
            "policy_version_id": policy_id,
            "content_hash": content_hash,
            "request_hash": request_hash,
        }
        change_set = f"catalog-policy:{policy_id}:{content_hash}"
        expires = _NOW + timedelta(days=7)
    else:
        assert proposal_id is not None and facts_hash is not None
        approval_type = "catalog_product_cultivation"
        payload = {
            "schema_version": namespace,
            "tenant_id": tenant,
            "approval_type": approval_type,
            "proposal_id": proposal_id,
            "policy_version_id": policy_id,
            "facts_hash": facts_hash,
            "request_hash": request_hash,
        }
        change_set = f"catalog-cultivation:{proposal_id}:{policy_id}:{facts_hash}"
        expires = _NOW + timedelta(days=3)
    decided_at = _NOW if state in {"approved", "rejected"} else None
    decided_by = "emp_catalog_reviewer" if decided_at is not None else None
    await connection.execute(
        text(
            "INSERT INTO approval_packages "
            "(tenant_id,approval_id,approval_type,title,proposed_change,reason,"
            "blast_radius,created_at,expires_at,state,proposed_by_employee,"
            "evidence_refs,change_set_ref,owner_employee,contract_namespace,"
            "request_hash,expires_at_limit,decided_at,decided_by) VALUES "
            "(:tenant,:approval,:approval_type,'Catalog internal approval',"
            "CAST(:payload AS jsonb),'internal only','{}'::jsonb,:now,:expires,"
            ":state,:employee,'[]'::jsonb,:change_set,:employee,:namespace,"
            ":request_hash,:expires,:decided_at,:decided_by)"
        ),
        {
            "tenant": tenant,
            "approval": approval_id,
            "approval_type": approval_type,
            "payload": json.dumps(payload),
            "now": _NOW,
            "expires": expires,
            "state": state,
            "employee": tenant.replace("tn_", "emp_"),
            "change_set": change_set,
            "namespace": namespace,
            "request_hash": request_hash,
            "decided_at": decided_at,
            "decided_by": decided_by,
        },
    )


async def _activate_policy(
    connection,
    *,
    tenant: str,
    suffix: str,
    policy_id: str | None = None,
    creation_key: str | None = None,
    activated_at: datetime = _NOW,
) -> str:
    policy = policy_id or f"cpv_{suffix}"
    values = _policy_values(tenant=tenant, suffix=suffix, policy=policy)
    if creation_key is not None:
        values["key"] = creation_key
    await connection.execute(_INSERT_POLICY, values)
    approval_id = f"apr_policy_{suffix}"
    await _insert_catalog_approval(
        connection,
        tenant=tenant,
        approval_id=approval_id,
        namespace="catalog-policy-v1",
        policy_id=policy,
        state="approved",
    )
    await connection.execute(
        text(
            "UPDATE catalog_proposal_policy_versions SET approval_id=:approval,"
            "state='active',activated_at=:activated WHERE tenant_id=:tenant "
            "AND policy_version_id=:policy"
        ),
        {
            "approval": approval_id,
            "activated": activated_at,
            "tenant": tenant,
            "policy": policy,
        },
    )
    return policy


async def _insert_evaluation_and_proposal(
    connection,
    *,
    tenant: str,
    suffix: str,
    policy_id: str,
    facts_hash: str = "c" * 64,
    overall_passed: bool = True,
) -> tuple[str, str]:
    evaluation_id = f"cpe_{suffix}"
    proposal_id = f"cpr_{suffix}"
    await connection.execute(
        text(
            "INSERT INTO catalog_proposal_evaluations "
            "(tenant_id,evaluation_id,cluster_id,policy_version_id,facts_hash,"
            "facts,rule_results,overall_passed,blocked_reason,proposed_by_run,created_at) "
            "VALUES (:tenant,:evaluation,:cluster,:policy,:hash,'{}'::jsonb,"
            "'[]'::jsonb,:passed,:blocked,:run,:now)"
        ),
        {
            "tenant": tenant,
            "evaluation": evaluation_id,
            "cluster": f"ncl_{suffix}",
            "policy": policy_id,
            "hash": facts_hash,
            "passed": overall_passed,
            "blocked": None if overall_passed else "minimum_accounts_failed",
            "run": f"run_{suffix}",
            "now": _NOW,
        },
    )
    await connection.execute(
        text(
            "INSERT INTO catalog_product_proposals "
            "(tenant_id,proposal_id,evaluation_id,cluster_id,policy_version_id,"
            "facts_hash,owner_employee,proposed_by_run,approval_id,"
            "approval_request_hash,state,created_at,updated_at) VALUES "
            "(:tenant,:proposal,:evaluation,:cluster,:policy,:hash,:employee,"
            ":run,NULL,NULL,'awaiting_approval_submission',:now,:now)"
        ),
        {
            "tenant": tenant,
            "proposal": proposal_id,
            "evaluation": evaluation_id,
            "cluster": f"ncl_{suffix}",
            "policy": policy_id,
            "hash": facts_hash,
            "employee": f"emp_{suffix}",
            "run": f"run_{suffix}",
            "now": _NOW,
        },
    )
    return evaluation_id, proposal_id


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


async def _delete_approvals(connection, *, tenant: str) -> None:
    await connection.execute(
        text(
            "ALTER TABLE approval_packages DISABLE TRIGGER "
            "trg_approval_packages_guard"
        )
    )
    await connection.execute(
        text("DELETE FROM approval_packages WHERE tenant_id=:tenant"),
        {"tenant": tenant},
    )
    await connection.execute(
        text(
            "ALTER TABLE approval_packages ENABLE TRIGGER "
            "trg_approval_packages_guard"
        )
    )


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
            await _activate_policy(
                connection, tenant=tenant, suffix="catalog_guard"
            )

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
            await _delete_approvals(connection, tenant=tenant)
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
            for label in ("a", "b"):
                policy = f"cpv_active_{label}"
                await connection.execute(
                    _INSERT_POLICY,
                    _policy_values(
                        tenant=tenant,
                        suffix="catalog_concurrency",
                        policy=policy,
                    )
                    | {"key": f"active-{label}"},
                )
                await _insert_catalog_approval(
                    connection,
                    tenant=tenant,
                    approval_id=f"apr_active_{label}",
                    namespace="catalog-policy-v1",
                    policy_id=policy,
                )

        async def activate(policy: str) -> bool:
            try:
                async with engine.begin() as connection:
                    label = policy[-1]
                    await connection.execute(
                        text(
                            "UPDATE catalog_proposal_policy_versions SET "
                            "approval_id=:approval,state='active',activated_at=:now "
                            "WHERE tenant_id=:tenant AND policy_version_id=:policy"
                        ),
                        {
                            "tenant": tenant,
                            "policy": policy,
                            "approval": f"apr_active_{label}",
                            "now": _NOW,
                        },
                    )
                return True
            except IntegrityError:
                return False

        assert sorted(
            await asyncio.gather(activate("cpv_active_a"), activate("cpv_active_b"))
        ) == [False, True]
        async with engine.connect() as connection:
            winner = await connection.scalar(
                text(
                    "SELECT policy_version_id FROM catalog_proposal_policy_versions "
                    "WHERE tenant_id=:tenant AND state='active'"
                ),
                {"tenant": tenant},
            )
        assert winner in {"cpv_active_a", "cpv_active_b"}

        async def insert_evaluation(evaluation: str) -> bool:
            try:
                async with engine.begin() as connection:
                    await connection.execute(
                        text("INSERT INTO catalog_proposal_evaluations (tenant_id,evaluation_id,cluster_id,policy_version_id,facts_hash,facts,rule_results,overall_passed,blocked_reason,proposed_by_run,created_at) VALUES (:tenant,:evaluation,:cluster,:policy,:hash,'{}'::jsonb,'[]'::jsonb,true,NULL,:run,:now)"),
                        {"tenant": tenant, "evaluation": evaluation, "cluster": "ncl_catalog_concurrency", "policy": winner, "hash": "e" * 64, "run": "run_catalog_concurrency", "now": _NOW},
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
            await _delete_approvals(connection, tenant=tenant)
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


def test_0057_is_the_only_script_head() -> None:
    """新增 revision 不能制造第二个 Alembic head。"""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert result.stdout.strip().splitlines() == ["0057 (head)"]


async def test_0057_policy_binding_requires_exact_subject_and_approved_state(
    db_url: str,
) -> None:
    """同租户但 namespace/subject/hash/state 错误的审批不能激活策略。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    tenant = "tn_policy_binding"
    suffix = "policy_binding"
    policy = f"cpv_{suffix}"
    try:
        async with engine.begin() as connection:
            await _seed_parents(connection, tenant=tenant, suffix=suffix)
            await connection.execute(
                _INSERT_POLICY,
                _policy_values(tenant=tenant, suffix=suffix),
            )
            await connection.execute(
                text(
                    "INSERT INTO approval_packages "
                    "(tenant_id,approval_id,approval_type,title,proposed_change,"
                    "reason,blast_radius,created_at,expires_at,state,"
                    "proposed_by_employee,evidence_refs,change_set_ref,"
                    "owner_employee,contract_namespace,request_hash,"
                    "expires_at_limit,decided_at,decided_by) VALUES "
                    "(:tenant,'apr_policy_legacy','catalog_proposal_policy_change',"
                    "'Legacy','{}'::jsonb,'legacy','{}'::jsonb,:now,:expires,"
                    "'approved',:employee,'[]'::jsonb,'legacy-catalog',:employee,"
                    "NULL,NULL,NULL,:now,'emp_catalog_reviewer')"
                ),
                {
                    "tenant": tenant,
                    "now": _NOW,
                    "expires": _NOW + timedelta(days=1),
                    "employee": f"emp_{suffix}",
                },
            )
            await _insert_catalog_approval(
                connection,
                tenant=tenant,
                approval_id="apr_policy_wrong_subject",
                namespace="catalog-policy-v1",
                policy_id="cpv_other_subject",
            )
            await _insert_catalog_approval(
                connection,
                tenant=tenant,
                approval_id="apr_policy_wrong_hash",
                namespace="catalog-policy-v1",
                policy_id=policy,
                content_hash="e" * 64,
            )
            await _insert_catalog_approval(
                connection,
                tenant=tenant,
                approval_id="apr_policy_pending",
                namespace="catalog-policy-v1",
                policy_id=policy,
                state="pending",
            )
            await _insert_catalog_approval(
                connection,
                tenant=tenant,
                approval_id="apr_policy_exact",
                namespace="catalog-policy-v1",
                policy_id=policy,
            )

        for approval in (
            "apr_policy_legacy",
            "apr_policy_wrong_subject",
            "apr_policy_wrong_hash",
            "apr_policy_pending",
        ):
            with pytest.raises(DBAPIError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            "UPDATE catalog_proposal_policy_versions SET "
                            "approval_id=:approval,state='active',activated_at=:now "
                            "WHERE tenant_id=:tenant AND policy_version_id=:policy"
                        ),
                        {
                            "approval": approval,
                            "now": _NOW,
                            "tenant": tenant,
                            "policy": policy,
                        },
                    )

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE catalog_proposal_policy_versions SET "
                    "approval_id='apr_policy_exact',state='active',activated_at=:now "
                    "WHERE tenant_id=:tenant AND policy_version_id=:policy"
                ),
                {"now": _NOW, "tenant": tenant, "policy": policy},
            )
    finally:
        async with engine.begin() as connection:
            await _delete_catalog_rows(connection, tenant=tenant)
            await _delete_approvals(connection, tenant=tenant)
            await connection.execute(
                text("DELETE FROM workflow_runs WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
            await connection.execute(
                text("DELETE FROM need_clusters WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
            await connection.execute(
                text("DELETE FROM employees WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
        await engine.dispose()


async def test_0057_rejects_advanced_inserts_and_inactive_policy_consumers(
    db_url: str,
) -> None:
    """状态机只能从初态插入，evaluation/proposal 必须消费当前 active 策略。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    tenant = "tn_initial_state"
    suffix = "initial_state"
    policy = f"cpv_{suffix}"
    try:
        async with engine.begin() as connection:
            await _seed_parents(connection, tenant=tenant, suffix=suffix)
            await _insert_catalog_approval(
                connection,
                tenant=tenant,
                approval_id="apr_direct_active",
                namespace="catalog-policy-v1",
                policy_id="cpv_direct_active",
            )

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO catalog_proposal_policy_versions "
                        "(tenant_id,policy_version_id,content,content_hash,"
                        "base_active_version_id,proposed_by,creation_key,"
                        "creation_request_hash,approval_id,state,created_at,"
                        "activated_at,terminal_at) VALUES "
                        "(:tenant,'cpv_direct_active','{}'::jsonb,:hash,NULL,"
                        ":employee,'direct-active',:request_hash,"
                        "'apr_direct_active','active',:now,:now,NULL)"
                    ),
                    _policy_values(
                        tenant=tenant,
                        suffix=suffix,
                        policy="cpv_direct_active",
                    ),
                )

        async with engine.begin() as connection:
            await connection.execute(
                _INSERT_POLICY,
                _policy_values(tenant=tenant, suffix=suffix),
            )

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO catalog_proposal_evaluations "
                        "(tenant_id,evaluation_id,cluster_id,policy_version_id,"
                        "facts_hash,facts,rule_results,overall_passed,blocked_reason,"
                        "proposed_by_run,created_at) VALUES "
                        "(:tenant,'cpe_inactive',:cluster,:policy,:hash,'{}'::jsonb,"
                        "'[]'::jsonb,true,NULL,:run,:now)"
                    ),
                    {
                        "tenant": tenant,
                        "cluster": f"ncl_{suffix}",
                        "policy": policy,
                        "hash": "c" * 64,
                        "run": f"run_{suffix}",
                        "now": _NOW,
                    },
                )
    finally:
        async with engine.begin() as connection:
            await _delete_catalog_rows(connection, tenant=tenant)
            await _delete_approvals(connection, tenant=tenant)
            await connection.execute(
                text("DELETE FROM workflow_runs WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
            await connection.execute(
                text("DELETE FROM need_clusters WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
            await connection.execute(
                text("DELETE FROM employees WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
        await engine.dispose()


async def test_0057_proposal_and_cultivation_require_exact_approved_subject(
    db_url: str,
) -> None:
    """提案绑定与 queued Case 必须使用同一 subject 的有效培养审批。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    tenant = "tn_cultivation_guard"
    suffix = "cultivation_guard"
    facts_hash = "c" * 64
    request_hash = "d" * 64
    try:
        async with engine.begin() as connection:
            await _seed_parents(connection, tenant=tenant, suffix=suffix)
            policy = await _activate_policy(
                connection, tenant=tenant, suffix=suffix
            )
            _, proposal = await _insert_evaluation_and_proposal(
                connection,
                tenant=tenant,
                suffix=suffix,
                policy_id=policy,
                facts_hash=facts_hash,
            )
            await _insert_catalog_approval(
                connection,
                tenant=tenant,
                approval_id="apr_cult_wrong_subject",
                namespace="catalog-cultivation-v1",
                policy_id=policy,
                proposal_id="cpr_other_subject",
                facts_hash=facts_hash,
                request_hash=request_hash,
            )
            await _insert_catalog_approval(
                connection,
                tenant=tenant,
                approval_id="apr_cult_wrong_hash",
                namespace="catalog-cultivation-v1",
                policy_id=policy,
                proposal_id=proposal,
                facts_hash="e" * 64,
                request_hash=request_hash,
            )
            await _insert_catalog_approval(
                connection,
                tenant=tenant,
                approval_id="apr_cult_pending",
                namespace="catalog-cultivation-v1",
                policy_id=policy,
                proposal_id=proposal,
                facts_hash=facts_hash,
                request_hash=request_hash,
                state="pending",
            )
            await _insert_catalog_approval(
                connection,
                tenant=tenant,
                approval_id="apr_cult_exact",
                namespace="catalog-cultivation-v1",
                policy_id=policy,
                proposal_id=proposal,
                facts_hash=facts_hash,
                request_hash=request_hash,
            )

        for approval, stored_hash in (
            ("apr_cult_wrong_subject", request_hash),
            ("apr_cult_wrong_hash", request_hash),
            ("apr_cult_exact", "f" * 64),
        ):
            with pytest.raises(DBAPIError):
                async with engine.begin() as connection:
                    await connection.execute(
                        text(
                            "UPDATE catalog_product_proposals SET "
                            "approval_id=:approval,approval_request_hash=:request_hash,"
                            "state='pending_review',updated_at=:updated "
                            "WHERE tenant_id=:tenant AND proposal_id=:proposal"
                        ),
                        {
                            "approval": approval,
                            "request_hash": stored_hash,
                            "updated": _NOW,
                            "tenant": tenant,
                            "proposal": proposal,
                        },
                    )

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE catalog_product_proposals SET "
                    "approval_id='apr_cult_pending',approval_request_hash=:request_hash,"
                    "state='pending_review',updated_at=:updated "
                    "WHERE tenant_id=:tenant AND proposal_id=:proposal"
                ),
                {
                    "request_hash": request_hash,
                    "updated": _NOW,
                    "tenant": tenant,
                    "proposal": proposal,
                },
            )

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE catalog_product_proposals SET "
                        "state='cultivation_queued',updated_at=:updated "
                        "WHERE tenant_id=:tenant AND proposal_id=:proposal"
                    ),
                    {
                        "updated": _NOW + timedelta(seconds=1),
                        "tenant": tenant,
                        "proposal": proposal,
                    },
                )

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE approval_packages SET state='approved',decided_at=:now,"
                    "decided_by='emp_catalog_reviewer' WHERE tenant_id=:tenant "
                    "AND approval_id='apr_cult_pending'"
                ),
                {"now": _NOW, "tenant": tenant},
            )
            await connection.execute(
                text(
                    "UPDATE catalog_product_proposals SET "
                    "state='cultivation_queued',updated_at=:updated "
                    "WHERE tenant_id=:tenant AND proposal_id=:proposal"
                ),
                {
                    "updated": _NOW + timedelta(seconds=1),
                    "tenant": tenant,
                    "proposal": proposal,
                },
            )

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO catalog_cultivation_cases "
                        "(tenant_id,cultivation_case_id,proposal_id,approval_id,"
                        "cluster_id,policy_version_id,facts_hash,evidence_refs,state,"
                        "queued_at) VALUES (:tenant,'ccc_wrong_approval',:proposal,"
                        "'apr_cult_exact',:cluster,:policy,:hash,'[]'::jsonb,'queued',:now)"
                    ),
                    {
                        "tenant": tenant,
                        "proposal": proposal,
                        "cluster": f"ncl_{suffix}",
                        "policy": policy,
                        "hash": facts_hash,
                        "now": _NOW,
                    },
                )

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO catalog_cultivation_cases "
                    "(tenant_id,cultivation_case_id,proposal_id,approval_id,"
                    "cluster_id,policy_version_id,facts_hash,evidence_refs,state,"
                    "queued_at) VALUES (:tenant,'ccc_exact',:proposal,"
                    "'apr_cult_pending',:cluster,:policy,:hash,'[]'::jsonb,'queued',:now)"
                ),
                {
                    "tenant": tenant,
                    "proposal": proposal,
                    "cluster": f"ncl_{suffix}",
                    "policy": policy,
                    "hash": facts_hash,
                    "now": _NOW,
                },
            )
    finally:
        async with engine.begin() as connection:
            await _delete_catalog_rows(connection, tenant=tenant)
            await _delete_approvals(connection, tenant=tenant)
            await connection.execute(
                text("DELETE FROM workflow_runs WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
            await connection.execute(
                text("DELETE FROM need_clusters WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
            await connection.execute(
                text("DELETE FROM employees WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
        await engine.dispose()


async def test_0057_rejects_malformed_initial_states_and_json_shapes(
    db_url: str,
) -> None:
    """四张表的非法 JSON、跳级初态和失败评估消费都必须由数据库拒绝。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    tenant = "tn_catalog_shapes"
    suffix = "catalog_shapes"
    facts_hash = "c" * 64
    request_hash = "d" * 64
    try:
        async with engine.begin() as connection:
            await _seed_parents(connection, tenant=tenant, suffix=suffix)

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO validated_needs "
                        "(tenant_id,need_id,account_id,product_category,"
                        "source_message_id,status,created_at,recurring_requirement) "
                        "VALUES (:tenant,'vnd_bad_recurring','acc_shapes','{}'::jsonb,"
                        "'msg_shapes','validated',:now,'[]'::jsonb)"
                    ),
                    {"tenant": tenant, "now": _NOW},
                )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO catalog_proposal_policy_versions "
                        "(tenant_id,policy_version_id,content,content_hash,"
                        "base_active_version_id,proposed_by,creation_key,"
                        "creation_request_hash,approval_id,state,created_at,"
                        "activated_at,terminal_at) VALUES "
                        "(:tenant,'cpv_bad_json','[]'::jsonb,:hash,NULL,:employee,"
                        "'bad-json',:request_hash,NULL,'pending_approval',:now,NULL,NULL)"
                    ),
                    _policy_values(tenant=tenant, suffix=suffix),
                )

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    _INSERT_POLICY,
                    _policy_values(
                        tenant=tenant, suffix=suffix, policy="cpv_direct_stale"
                    )
                    | {"key": "direct-stale"},
                )
                await connection.execute(
                    text(
                        "UPDATE catalog_proposal_policy_versions SET state='stale',"
                        "terminal_at=:now WHERE tenant_id=:tenant AND "
                        "policy_version_id='cpv_direct_stale'"
                    ),
                    {"tenant": tenant, "now": _NOW},
                )

        async with engine.begin() as connection:
            policy = await _activate_policy(
                connection, tenant=tenant, suffix=suffix
            )
            await connection.execute(
                text(
                    "INSERT INTO catalog_proposal_evaluations "
                    "(tenant_id,evaluation_id,cluster_id,policy_version_id,"
                    "facts_hash,facts,rule_results,overall_passed,blocked_reason,"
                    "proposed_by_run,created_at) VALUES "
                    "(:tenant,'cpe_failed_shape',:cluster,:policy,:hash,'{}'::jsonb,"
                    "'[]'::jsonb,false,'minimum_accounts_failed',:run,:now)"
                ),
                {
                    "tenant": tenant,
                    "cluster": f"ncl_{suffix}",
                    "policy": policy,
                    "hash": "e" * 64,
                    "run": f"run_{suffix}",
                    "now": _NOW,
                },
            )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO catalog_proposal_evaluations "
                        "(tenant_id,evaluation_id,cluster_id,policy_version_id,"
                        "facts_hash,facts,rule_results,overall_passed,blocked_reason,"
                        "proposed_by_run,created_at) VALUES "
                        "(:tenant,'cpe_bad_result',:cluster,:policy,:hash,'{}'::jsonb,"
                        "'[]'::jsonb,true,'must_be_null',:run,:now)"
                    ),
                    {
                        "tenant": tenant,
                        "cluster": f"ncl_{suffix}",
                        "policy": policy,
                        "hash": "f" * 64,
                        "run": f"run_{suffix}",
                        "now": _NOW,
                    },
                )

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO catalog_product_proposals "
                        "(tenant_id,proposal_id,evaluation_id,cluster_id,"
                        "policy_version_id,facts_hash,owner_employee,proposed_by_run,"
                        "approval_id,approval_request_hash,state,created_at,updated_at) "
                        "VALUES (:tenant,'cpr_failed_shape','cpe_failed_shape',:cluster,"
                        ":policy,:hash,:employee,:run,NULL,NULL,"
                        "'awaiting_approval_submission',:now,:now)"
                    ),
                    {
                        "tenant": tenant,
                        "cluster": f"ncl_{suffix}",
                        "policy": policy,
                        "hash": "e" * 64,
                        "employee": f"emp_{suffix}",
                        "run": f"run_{suffix}",
                        "now": _NOW,
                    },
                )

        async with engine.begin() as connection:
            evaluation, proposal = await _insert_evaluation_and_proposal(
                connection,
                tenant=tenant,
                suffix=suffix,
                policy_id=policy,
                facts_hash=facts_hash,
            )
            await _insert_catalog_approval(
                connection,
                tenant=tenant,
                approval_id="apr_shapes",
                namespace="catalog-cultivation-v1",
                policy_id=policy,
                proposal_id=proposal,
                facts_hash=facts_hash,
                request_hash=request_hash,
            )

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO catalog_product_proposals "
                        "(tenant_id,proposal_id,evaluation_id,cluster_id,"
                        "policy_version_id,facts_hash,owner_employee,proposed_by_run,"
                        "approval_id,approval_request_hash,state,created_at,updated_at) "
                        "VALUES (:tenant,'cpr_direct_review',:evaluation,:cluster,"
                        ":policy,:hash,:employee,:run,'apr_shapes',:request_hash,"
                        "'pending_review',:now,:now)"
                    ),
                    {
                        "tenant": tenant,
                        "evaluation": evaluation,
                        "cluster": f"ncl_{suffix}",
                        "policy": policy,
                        "hash": facts_hash,
                        "employee": f"emp_{suffix}",
                        "run": f"run_{suffix}",
                        "request_hash": request_hash,
                        "now": _NOW,
                    },
                )

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE catalog_product_proposals SET approval_id='apr_shapes',"
                    "approval_request_hash=:request_hash,state='pending_review' "
                    "WHERE tenant_id=:tenant AND proposal_id=:proposal"
                ),
                {
                    "tenant": tenant,
                    "proposal": proposal,
                    "request_hash": request_hash,
                },
            )
            await connection.execute(
                text(
                    "UPDATE catalog_product_proposals SET state='cultivation_queued' "
                    "WHERE tenant_id=:tenant AND proposal_id=:proposal"
                ),
                {"tenant": tenant, "proposal": proposal},
            )

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE catalog_product_proposals SET state='rejected' "
                        "WHERE tenant_id=:tenant AND proposal_id=:proposal"
                    ),
                    {"tenant": tenant, "proposal": proposal},
                )

        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO catalog_cultivation_cases "
                        "(tenant_id,cultivation_case_id,proposal_id,approval_id,"
                        "cluster_id,policy_version_id,facts_hash,evidence_refs,state,"
                        "queued_at) VALUES (:tenant,'ccc_bad_state',:proposal,"
                        "'apr_shapes',:cluster,:policy,:hash,'[]'::jsonb,'done',:now)"
                    ),
                    {
                        "tenant": tenant,
                        "proposal": proposal,
                        "cluster": f"ncl_{suffix}",
                        "policy": policy,
                        "hash": facts_hash,
                        "now": _NOW,
                    },
                )

        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "INSERT INTO catalog_cultivation_cases "
                        "(tenant_id,cultivation_case_id,proposal_id,approval_id,"
                        "cluster_id,policy_version_id,facts_hash,evidence_refs,state,"
                        "queued_at) VALUES (:tenant,'ccc_bad_evidence',:proposal,"
                        "'apr_shapes',:cluster,:policy,:hash,'{}'::jsonb,'queued',:now)"
                    ),
                    {
                        "tenant": tenant,
                        "proposal": proposal,
                        "cluster": f"ncl_{suffix}",
                        "policy": policy,
                        "hash": facts_hash,
                        "now": _NOW,
                    },
                )
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE catalog_proposal_policy_versions SET "
                        "state='rejected',terminal_at=:terminal "
                        "WHERE tenant_id=:tenant AND policy_version_id=:policy"
                    ),
                    {
                        "terminal": _NOW + timedelta(days=1),
                        "tenant": tenant,
                        "policy": policy,
                    },
                )
    finally:
        async with engine.begin() as connection:
            await _delete_catalog_rows(connection, tenant=tenant)
            await _delete_approvals(connection, tenant=tenant)
            await connection.execute(
                text("DELETE FROM workflow_runs WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
            await connection.execute(
                text("DELETE FROM need_clusters WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
            await connection.execute(
                text("DELETE FROM employees WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
        await engine.dispose()


async def test_0057_concurrent_creation_proposal_and_cultivation_keys(
    db_url: str,
) -> None:
    """真实并发事务下 creation、proposal、case 业务键都只允许一个赢家。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    tenant = "tn_catalog_key_races"
    suffix = "catalog_key_races"
    facts_hash = "c" * 64

    async def attempt(statement, values: dict[str, object]) -> bool:
        try:
            async with engine.begin() as connection:
                await connection.execute(statement, values)
            return True
        except IntegrityError:
            return False

    try:
        async with engine.begin() as connection:
            await _seed_parents(connection, tenant=tenant, suffix=suffix)
            policy = await _activate_policy(
                connection, tenant=tenant, suffix=suffix
            )

        creation_results = await asyncio.gather(
            *(
                attempt(
                    _INSERT_POLICY,
                    _policy_values(
                        tenant=tenant,
                        suffix=suffix,
                        policy=f"cpv_creation_{label}",
                    )
                    | {"key": "same-creation-key"},
                )
                for label in ("a", "b")
            )
        )
        assert sorted(creation_results) == [False, True]

        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO catalog_proposal_evaluations "
                    "(tenant_id,evaluation_id,cluster_id,policy_version_id,"
                    "facts_hash,facts,rule_results,overall_passed,blocked_reason,"
                    "proposed_by_run,created_at) VALUES "
                    "(:tenant,'cpe_key_race',:cluster,:policy,:hash,'{}'::jsonb,"
                    "'[]'::jsonb,true,NULL,:run,:now)"
                ),
                {
                    "tenant": tenant,
                    "cluster": f"ncl_{suffix}",
                    "policy": policy,
                    "hash": facts_hash,
                    "run": f"run_{suffix}",
                    "now": _NOW,
                },
            )

        proposal_insert = text(
            "INSERT INTO catalog_product_proposals "
            "(tenant_id,proposal_id,evaluation_id,cluster_id,policy_version_id,"
            "facts_hash,owner_employee,proposed_by_run,approval_id,"
            "approval_request_hash,state,created_at,updated_at) VALUES "
            "(:tenant,:proposal,'cpe_key_race',:cluster,:policy,:hash,:employee,"
            ":run,NULL,NULL,'awaiting_approval_submission',:now,:now)"
        )
        common = {
            "tenant": tenant,
            "cluster": f"ncl_{suffix}",
            "policy": policy,
            "hash": facts_hash,
            "employee": f"emp_{suffix}",
            "run": f"run_{suffix}",
            "now": _NOW,
        }
        proposal_results = await asyncio.gather(
            attempt(proposal_insert, common | {"proposal": "cpr_key_race_a"}),
            attempt(proposal_insert, common | {"proposal": "cpr_key_race_b"}),
        )
        assert sorted(proposal_results) == [False, True]
        async with engine.begin() as connection:
            proposal = await connection.scalar(
                text(
                    "SELECT proposal_id FROM catalog_product_proposals "
                    "WHERE tenant_id=:tenant AND evaluation_id='cpe_key_race'"
                ),
                {"tenant": tenant},
            )
            assert proposal in {"cpr_key_race_a", "cpr_key_race_b"}
            await _insert_catalog_approval(
                connection,
                tenant=tenant,
                approval_id="apr_key_race",
                namespace="catalog-cultivation-v1",
                policy_id=policy,
                proposal_id=proposal,
                facts_hash=facts_hash,
            )
            await connection.execute(
                text(
                    "UPDATE catalog_product_proposals SET "
                    "approval_id='apr_key_race',approval_request_hash=:request_hash,"
                    "state='pending_review' WHERE tenant_id=:tenant AND "
                    "proposal_id=:proposal"
                ),
                {"tenant": tenant, "proposal": proposal, "request_hash": "d" * 64},
            )
            await connection.execute(
                text(
                    "UPDATE catalog_product_proposals SET state='cultivation_queued' "
                    "WHERE tenant_id=:tenant AND proposal_id=:proposal"
                ),
                {"tenant": tenant, "proposal": proposal},
            )

        case_insert = text(
            "INSERT INTO catalog_cultivation_cases "
            "(tenant_id,cultivation_case_id,proposal_id,approval_id,cluster_id,"
            "policy_version_id,facts_hash,evidence_refs,state,queued_at) VALUES "
            "(:tenant,:case,:proposal,'apr_key_race',:cluster,:policy,:hash,"
            "'[]'::jsonb,'queued',:now)"
        )
        case_results = await asyncio.gather(
            attempt(case_insert, common | {"proposal": proposal, "case": "ccc_key_a"}),
            attempt(case_insert, common | {"proposal": proposal, "case": "ccc_key_b"}),
        )
        assert sorted(case_results) == [False, True]
    finally:
        async with engine.begin() as connection:
            await _delete_catalog_rows(connection, tenant=tenant)
            await _delete_approvals(connection, tenant=tenant)
            await connection.execute(
                text("DELETE FROM workflow_runs WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
            await connection.execute(
                text("DELETE FROM need_clusters WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
            await connection.execute(
                text("DELETE FROM employees WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
        await engine.dispose()


async def test_0057_superseded_terminal_cannot_precede_activation(db_url: str) -> None:
    """superseded 的终止时间不能早于同一版本的激活时间。"""
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    tenant = "tn_policy_time"
    suffix = "policy_time"
    activated = _NOW + timedelta(days=2)
    try:
        async with engine.begin() as connection:
            await _seed_parents(connection, tenant=tenant, suffix=suffix)
            policy = await _activate_policy(
                connection,
                tenant=tenant,
                suffix=suffix,
                activated_at=activated,
            )
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE catalog_proposal_policy_versions SET "
                        "state='superseded',terminal_at=:terminal "
                        "WHERE tenant_id=:tenant AND policy_version_id=:policy"
                    ),
                    {
                        "terminal": _NOW + timedelta(days=1),
                        "tenant": tenant,
                        "policy": policy,
                    },
                )
    finally:
        async with engine.begin() as connection:
            await _delete_catalog_rows(connection, tenant=tenant)
            await _delete_approvals(connection, tenant=tenant)
            await connection.execute(
                text("DELETE FROM workflow_runs WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
            await connection.execute(
                text("DELETE FROM need_clusters WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
            await connection.execute(
                text("DELETE FROM employees WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
        await engine.dispose()


async def test_0057_preserves_existing_legacy_catalog_like_approval_but_blocks_new(
    db_url: str,
) -> None:
    """升级保留原 CHECK 允许的历史字符串，仅新 INSERT 必须使用严格 namespace。"""
    from infra.db.session import create_engine_from

    tenant = "tn_legacy_catalog_like"
    engine = create_engine_from(db_url)
    legacy_insert = text(
        "INSERT INTO approval_packages "
        "(tenant_id,approval_id,approval_type,title,proposed_change,reason,"
        "blast_radius,created_at,expires_at,state,proposed_by_employee,evidence_refs,"
        "change_set_ref,owner_employee,contract_namespace,request_hash,"
        "expires_at_limit) VALUES (:tenant,:approval,'legacy_internal','Legacy',"
        "CAST(:payload AS jsonb),'legacy','{}'::jsonb,:now,:expires,'pending',"
        "'emp_legacy','[]'::jsonb,'catalog-policy:legacy-history','emp_legacy',"
        "NULL,NULL,NULL)"
    )
    try:
        _alembic(db_url, "downgrade", "0056")
        async with engine.begin() as connection:
            await connection.execute(
                legacy_insert,
                {
                    "tenant": tenant,
                    "approval": "apr_legacy_before_0057",
                    "payload": json.dumps({"schema_version": "legacy-v1"}),
                    "now": _NOW,
                    "expires": _NOW + timedelta(days=1),
                },
            )
        _alembic(db_url, "upgrade", "0057")
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "UPDATE approval_packages SET state='approved',decided_at=:now,"
                    "decided_by='emp_reviewer' WHERE tenant_id=:tenant AND "
                    "approval_id='apr_legacy_before_0057'"
                ),
                {"now": _NOW, "tenant": tenant},
            )
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    legacy_insert,
                    {
                        "tenant": tenant,
                        "approval": "apr_legacy_after_0057",
                        "payload": json.dumps({"schema_version": "legacy-v1"}),
                        "now": _NOW,
                        "expires": _NOW + timedelta(days=1),
                    },
                )
    finally:
        async with engine.begin() as connection:
            await _delete_approvals(connection, tenant=tenant)
        await engine.dispose()
        _alembic(db_url, "upgrade", "head")


async def test_0057_downgrade_refuses_catalog_approval_before_any_ddl(
    db_url: str,
) -> None:
    """即使四张 Catalog 表为空，严格 Catalog 审批仍必须在 DDL 前阻止降级。"""
    from infra.db.session import create_engine_from

    tenant = "tn_catalog_approval_downgrade"
    suffix = "catalog_approval_downgrade"
    engine = create_engine_from(db_url)
    try:
        async with engine.begin() as connection:
            await _seed_parents(connection, tenant=tenant, suffix=suffix)
            await _insert_catalog_approval(
                connection,
                tenant=tenant,
                approval_id="apr_catalog_downgrade",
                namespace="catalog-policy-v1",
                policy_id=f"cpv_{suffix}",
                state="pending",
            )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "LOCK TABLE catalog_cultivation_cases "
                    "IN ACCESS SHARE MODE"
                )
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "scripts/run_alembic.py",
                "downgrade",
                "0056",
                cwd=_ROOT,
                env={**os.environ, "DATABASE_URL": db_url},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(process.wait(), timeout=1.5)
            except TimeoutError:
                process.kill()
                await process.wait()
                pytest.fail("downgrade 未在 DDL 锁前被 Catalog approval preflight 拒绝")
            assert process.returncode != 0
        async with engine.connect() as connection:
            names = set(
                await connection.run_sync(lambda sync: inspect(sync).get_table_names())
            )
            recurring = {
                str(item["name"])
                for item in await connection.run_sync(
                    lambda sync: inspect(sync).get_columns("validated_needs")
                )
            }
            revision = await connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
        assert set(_TABLES) <= names
        assert "recurring_requirement" in recurring
        assert revision == "0057"
    finally:
        async with engine.begin() as connection:
            await _delete_approvals(connection, tenant=tenant)
            await connection.execute(
                text("DELETE FROM workflow_runs WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
            await connection.execute(
                text("DELETE FROM need_clusters WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
            await connection.execute(
                text("DELETE FROM employees WHERE tenant_id=:tenant"),
                {"tenant": tenant},
            )
        await engine.dispose()
        _alembic(db_url, "upgrade", "head")
