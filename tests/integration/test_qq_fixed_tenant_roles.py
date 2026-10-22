"""QQ 新迁移的真实受限登录身份、证据约束及非空往返验收。"""
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import insert, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.qq_trial_tables import QQSentAliasRow, QQTrialEventRow, QQTrialRow
from infra.db.session import create_engine_from
from infra.db.tables import RawArtifactRow, ToolCallRow
from infra.db.tenant_security import (
    _POLICIES_QUERY,
    _POLICY_NAMES,
    _PREDICATE,
    _ROLE_QUERY,
    _TABLES_QUERY,
    _check_role,
    _check_table_privileges,
    _expression_tokens,
    assert_tenant_database_isolation,
    tenant_database_role,
)
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import new_id
from tests.integration.test_qq_inbound_scope import seed
from tests.integration.test_reply_review_migration import migrate
from tests.runtime_database_fixtures import runtime_database_scope

ROWS = (QQSentAliasRow, QQTrialRow, QQTrialEventRow)
TABLES = tuple(row.__tablename__ for row in ROWS)


def evidence(name, value):
    directory = os.environ.get("QQ_COMPAT_EVIDENCE_DIR")
    if directory:
        target = Path(directory) / f"{name}.json"
        target.write_text(json.dumps(value, ensure_ascii=False, indent=2))


async def seed_qq(engine, tenant):
    """全部合成业务行经当前企业角色写入；管理员只负责迁移与授予既有受限角色。"""
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    route, _owner, user, header, attempt, _account, _point = await seed(sessions, tenant)
    artifact, call, alias, trial, event = [new_id(prefix) for prefix in ("art", "tcl", "qae", "qqt", "qqe")]
    now = datetime.now(UTC)
    digest = hashlib.sha256(b"synthetic QQ migration evidence").hexdigest()
    values = [
        {"tenant_id":tenant, "evidence_id":alias, "identity_id":route.configured_identity_id,
         "attempt_id":attempt, "alias_digest":digest, "provider_header":f"<{new_id('ext')}@example.test>",
         "canonical_header":header, "artifact_id":artifact, "artifact_hash":digest,
         "tool_call_id":call, "created_at":now},
        {"tenant_id":tenant, "trial_id":trial, "mailbox_alias":"qq-role-"+uuid4().hex[:12],
         "attempt_id":attempt, "alias_evidence_id":alias, "scope":{"fixture": True}, "state":"active",
         "sessions":0, "searches":0, "headers":0, "bodies":0, "classifications":0,
         "provider_requests":0, "input_bytes":0, "output_tokens":0, "provider_pending":False},
        {"tenant_id":tenant, "event_id":event, "trial_id":trial, "kind":"fixture", "created_at":now, "input_bytes":0},
    ]
    async with sessions.begin() as session:
        session.add(RawArtifactRow(tenant_id=tenant, artifact_id=artifact, kind="email_raw",
            mime_type="message/rfc822", content_hash=digest, size_bytes=31,
            object_key=f"raw/{tenant}/{artifact}", uploaded_at=now))
        session.add(ToolCallRow(tenant_id=tenant, tool_call_id=call, tool_id="email.qq.sent_alias.read",
            tool_version="offline-role-fixture", risk_level="low", cost_class="free", status="succeeded",
            attempt_count=1, user_id=user, idempotency_key=call, request_fingerprint=digest,
            fingerprint_version="v1", provider_ref=artifact, message_attempt_id=attempt,
            created_at=now, updated_at=now, completed_at=now))
        await session.flush()
        for model, data in zip(ROWS, values, strict=True):
            session.add(model(**data))
            await session.flush()
    return values


@pytest_asyncio.fixture
async def qq_roles(db_url):
    engines = []
    async with runtime_database_scope(db_url) as provision:
        try:
            records = []
            for _ in range(2):
                tenant = new_id("tn")
                engine = create_engine_from(await provision(tenant))
                engines.append(engine)
                await assert_tenant_database_isolation(engine, tenant)
                records.append((tenant, engine, await seed_qq(engine, tenant)))
            yield records
        finally:
            for engine in engines:
                await engine.dispose()


async def rejected(engine, statement, parameters=None, *, state):
    with pytest.raises(DBAPIError) as caught:
        async with engine.begin() as connection:
            await connection.execute(statement, parameters or {})
    assert caught.value.orig.sqlstate == state


async def test_qq_fixed_roles_ignore_forged_guc_and_deny_cross_tenant(qq_roles):
    observations = []
    for index, (tenant, engine, values) in enumerate(qq_roles):
        other, _other_engine, foreign = qq_roles[1-index]
        async with engine.begin() as connection:
            role = dict((await connection.execute(text(
                "SELECT current_user::text AS current_role, rolsuper, rolinherit, rolbypassrls, "
                "rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname=current_user"
            ))).mappings().one())
            assert role.pop("current_role") == tenant_database_role(tenant)
            assert not any(role.values())
            for guc in ("app.tenant_id", "tradeos.tenant_id"):
                await connection.execute(text("SELECT set_config(:guc, :other, true)"), {"guc": guc, "other": other})
            visible = {}
            for table in TABLES:
                visible[table] = list((await connection.execute(text(f"SELECT tenant_id FROM {table}"))).scalars())
                assert visible[table] == [tenant]
                for action in (f"UPDATE {table} SET tenant_id=tenant_id", f"DELETE FROM {table}"):
                    result = await connection.execute(text(action+" WHERE tenant_id=:other RETURNING tenant_id"), {"other": other})
                    assert list(result.scalars()) == []
        for model, data, key in zip(ROWS, foreign, ("evidence_id", "trial_id", "event_id"), strict=True):
            await rejected(engine, insert(model).values(**{**data, key: new_id("new")}), state="42501")
        for sql in (f'SET ROLE "{tenant_database_role(other)}"', "TRUNCATE qq_reply_trials",
                    "ALTER TABLE qq_reply_trials DISABLE ROW LEVEL SECURITY"):
            await rejected(engine, text(sql), state="42501")
        observations.append({"tenant": tenant, "role": tenant_database_role(tenant), "role_flags": role,
                             "visible_after_forged_guc": visible, "cross_tenant_insert_sqlstate": "42501",
                             "cross_tenant_update_delete_rows": 0})
    evidence("qq-fixed-role-isolation", observations)


async def test_qq_constraints_still_apply_to_real_runtime_role(qq_roles):
    tenant, engine, values = qq_roles[0]
    _other, _foreign_engine, foreign = qq_roles[1]
    for table in ("qq_sent_aliases", "qq_reply_trial_events"):
        await rejected(engine, text(f"UPDATE {table} SET tenant_id=tenant_id"), state="P0001")
        await rejected(engine, text(f"DELETE FROM {table}"), state="P0001")
    await rejected(engine, text("DELETE FROM qq_reply_trials"), state="P0001")
    await rejected(engine, text("UPDATE qq_reply_trials SET scope='{}'::jsonb"), state="P0001")
    for field, above in (("sessions",21),("searches",21),("headers",21),("bodies",2),
                         ("classifications",2),("provider_requests",5),("input_bytes",32769),("output_tokens",513)):
        await rejected(engine, text(f"UPDATE qq_reply_trials SET {field}=:above"), {"above": above}, state="23514")
    await rejected(engine, text("UPDATE qq_reply_trials SET state='invalid'"), state="23514")
    async with engine.begin() as connection:
        await connection.execute(text("UPDATE qq_reply_trials SET sessions=1, state='received', watermark='{}'::jsonb"))
    for assignment in ("sessions=0", "state='active'"):
        await rejected(engine, text("UPDATE qq_reply_trials SET "+assignment), state="P0001")
    await rejected(engine, text("UPDATE qq_reply_trials SET watermark=CAST(:watermark AS jsonb)"),
                   {"watermark": json.dumps({"changed": True})}, state="P0001")
    await rejected(engine, insert(QQTrialRow).values(**{
        **values[1], "trial_id":new_id("qqt"), "mailbox_alias":"fk-"+uuid4().hex[:12],
        "attempt_id":new_id("mat"), "alias_evidence_id":foreign[0]["evidence_id"],
    }), state="23503")
    await rejected(engine, insert(QQTrialEventRow).values(**{
        **values[2], "event_id":new_id("qqe"), "trial_id":foreign[1]["trial_id"],
    }), state="23503")
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT sessions FROM qq_reply_trials")) == 1
        assert await connection.scalar(text("SELECT state FROM qq_reply_trials")) == "received"
        assert await connection.scalar(text("SELECT count(*) FROM qq_sent_aliases")) == 1
        assert await connection.scalar(text("SELECT count(*) FROM qq_reply_trial_events")) == 1
    evidence("qq-runtime-constraints", {"tenant":tenant, "immutable_sqlstate":"P0001",
        "budget_checks_sqlstate":"23514", "same_tenant_foreign_keys_sqlstate":"23503",
        "monotonic_state_and_evidence_preserved":True})


async def schema(connection):
    """策略修复不应变更列、CHECK/FK 或只增/单调触发器。"""
    return {
        "columns": [tuple(row) for row in (await connection.execute(text(
            "SELECT table_name,column_name,data_type,is_nullable,character_maximum_length "
            "FROM information_schema.columns WHERE table_schema='public' AND table_name IN "
            "('qq_sent_aliases','qq_reply_trials','qq_reply_trial_events') ORDER BY 1,ordinal_position"
        ))).all()],
        "constraints": [tuple(row) for row in (await connection.execute(text(
            "SELECT conrelid::regclass::text,conname,pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid IN ('qq_sent_aliases'::regclass,'qq_reply_trials'::regclass,'qq_reply_trial_events'::regclass) ORDER BY 1,2"
        ))).all()],
        "triggers": [tuple(row) for row in (await connection.execute(text(
            "SELECT tgrelid::regclass::text,tgname,pg_get_triggerdef(oid) FROM pg_trigger WHERE NOT tgisinternal "
            "AND tgrelid IN ('qq_sent_aliases'::regclass,'qq_reply_trials'::regclass,'qq_reply_trial_events'::regclass) ORDER BY 1,2"
        ))).all()],
    }


async def assert_historical_0080_security(engine, tenant):
    """只按0080三张QQ表验证实际角色/策略，不用当前全库metadata校验旧schema。"""
    async with engine.begin() as connection:
        role = tenant_database_role(tenant)
        row = (await connection.execute(_ROLE_QUERY, {"role":role})).mappings().one()
        _check_role(row, role, runtime=True)
        tables = [dict(row) for row in (await connection.execute(_TABLES_QUERY)).mappings()
                  if row["relname"] in TABLES]
        assert {row["relname"] for row in tables} == set(TABLES)
        assert all(row[key] for row in tables for key in
                   ("attnotnull", "relrowsecurity", "relforcerowsecurity"))
        await _check_table_privileges(connection, role, tables)
        entries = [dict(row) for row in (await connection.execute(_POLICIES_QUERY)).mappings()
                   if row["relname"] in TABLES]
        predicate = _expression_tokens(_PREDICATE)
        for table in TABLES:
            policies = [entry for entry in entries if entry["relname"] == table]
            assert len(policies) == 2
            assert {entry["polname"] for entry in policies} == set(_POLICY_NAMES)
            for entry in policies:
                assert entry["polcmd"] == "*"
                assert entry["polpermissive"] == _POLICY_NAMES[entry["polname"]]
                assert entry["public_policy"]
                assert _expression_tokens(entry["using_expression"] or "") == predicate
                assert _expression_tokens(entry["check_expression"] or "") == predicate
        for guc in ("app.tenant_id", "tradeos.tenant_id"):
            await connection.execute(text("SELECT set_config(:guc,:foreign,true)"), {"guc":guc,"foreign":new_id("tn")})
        for table in TABLES:
            assert list((await connection.execute(text(f"SELECT tenant_id FROM {table}"))).scalars()) == [tenant]
        policies = [tuple(row) for row in (await connection.execute(text(
            "SELECT tablename,policyname,permissive,qual,with_check FROM pg_policies "
            "WHERE tablename IN ('qq_sent_aliases','qq_reply_trials','qq_reply_trial_events') ORDER BY 1,2"
        ))).all()]
        assert len(policies) == 6
        for table in TABLES:
            assert {(name, mode) for t,name,mode,_,_ in policies if t == table} == {
                ("tradeos_tenant_allow", "PERMISSIVE"), ("tradeos_tenant_guard", "RESTRICTIVE")}
        for _,_,_,using,check in policies:
            for predicate in (using,check):
                assert all(part in predicate for part in ("CURRENT_USER", "sha256", "tenant_id"))
                assert "current_setting" not in predicate
        rls = [tuple(row) for row in (await connection.execute(text(
            "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid IN "
            "('qq_sent_aliases'::regclass,'qq_reply_trials'::regclass,'qq_reply_trial_events'::regclass) ORDER BY 1"
        ))).all()]
        assert len(rls) == 3 and all(enabled and forced for _,enabled,forced in rls)
    return policies, rls


async def assert_historical_privilege_regressions(admin, engine, tenant):
    """真实撤授权限验证历史检查不会把缺少INSERT权限误判为RLS隔离。"""
    role = tenant_database_role(tenant)
    for table in TABLES:
        async with admin.begin() as connection:
            await connection.execute(text(f'REVOKE INSERT, UPDATE, DELETE ON {table} FROM "{role}"'))
        try:
            with pytest.raises(TenantIsolationViolation):
                await assert_historical_0080_security(engine, tenant)
        finally:
            async with admin.begin() as connection:
                await connection.execute(text(f'GRANT INSERT, UPDATE, DELETE ON {table} TO "{role}"'))
        for privilege in ("TRUNCATE", "REFERENCES", "TRIGGER", "SELECT WITH GRANT OPTION",
                          "INSERT WITH GRANT OPTION", "UPDATE WITH GRANT OPTION", "DELETE WITH GRANT OPTION"):
            async with admin.begin() as connection:
                action = privilege.removesuffix(" WITH GRANT OPTION")
                option = " WITH GRANT OPTION" if privilege.endswith(" WITH GRANT OPTION") else ""
                await connection.execute(text(f'GRANT {action} ON {table} TO "{role}"{option}'))
            try:
                with pytest.raises(TenantIsolationViolation):
                    await assert_historical_0080_security(engine, tenant)
            finally:
                revoke = ("GRANT OPTION FOR " + privilege.removesuffix(" WITH GRANT OPTION")
                          if privilege.endswith(" WITH GRANT OPTION") else privilege)
                async with admin.begin() as connection:
                    await connection.execute(text(f'REVOKE {revoke} ON {table} FROM "{role}"'))


async def test_0080_nonempty_roundtrip_retains_evidence_and_revokes_rollback_access(db_url):
    name = "qq_fixed_roles_"+uuid4().hex
    admin = create_engine_from(db_url)
    url = make_url(str(db_url)).set(database=name)
    created = False
    target = None
    try:
        async with admin.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
            created = True
        assert migrate(url, "upgrade", "0079") == 0
        target = create_engine_from(url.render_as_string(False))
        async with target.connect() as connection:
            before = await schema(connection)
        assert migrate(url, "upgrade", "0080") == 0
        # 当前provision只服务当前schema；先在head创建受限角色和合成原件，再回到待验0080。
        assert migrate(url, "upgrade", "head") == 0
        async with runtime_database_scope(url.render_as_string(False)) as provision:
            tenant = new_id("tn")
            engine = create_engine_from(await provision(tenant))
            try:
                values = await seed_qq(engine, tenant)
                await assert_tenant_database_isolation(engine, tenant)
                assert migrate(url, "downgrade", "0080") == 0
                await assert_historical_privilege_regressions(target, engine, tenant)
                policies_before, _ = await assert_historical_0080_security(engine, tenant)
                # 相同表和INSERT形状先证明本企业可写；回滚合成增量，保留原件行数断言。
                local_event = new_id("qqe")
                async with engine.begin() as connection:
                    result = await connection.execute(insert(QQTrialEventRow).values(**{
                        **values[2], "event_id":local_event,
                    }).returning(QQTrialEventRow.event_id))
                    assert result.scalar_one() == local_event
                    assert await connection.scalar(text("SELECT count(*) FROM qq_reply_trial_events")) == 2
                    await connection.rollback()
                await rejected(engine, insert(QQTrialEventRow).values(**{
                    **values[2], "tenant_id":new_id("tn"), "event_id":new_id("qqe"),
                }), state="42501")
                assert migrate(url, "downgrade", "0079") == 0
                for table in TABLES:
                    await rejected(engine, text(f"SELECT * FROM {table}"), state="42501")
                # 0079 自己的非空证据回退保护仍有效，不能删除数据强制通过。
                assert migrate(url, "downgrade", "0078") != 0
                assert migrate(url, "upgrade", "0080") == 0
                policies_after, _ = await assert_historical_0080_security(engine, tenant)
                assert policies_after == policies_before
                async with engine.connect() as connection:
                    assert [await connection.scalar(text(f"SELECT count(*) FROM {t}")) for t in TABLES] == [1,1,1]
                    assert await schema(connection) == before
                    policies = [tuple(row) for row in (await connection.execute(text(
                        "SELECT tablename,policyname,permissive,qual,with_check FROM pg_policies "
                        "WHERE tablename IN ('qq_sent_aliases','qq_reply_trials','qq_reply_trial_events') ORDER BY 1,2"
                    ))).all()]
                    rls = [tuple(row) for row in (await connection.execute(text(
                        "SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class WHERE oid IN "
                        "('qq_sent_aliases'::regclass,'qq_reply_trials'::regclass,'qq_reply_trial_events'::regclass) ORDER BY 1"
                    ))).all()]
                    assert all(enabled and forced for _, enabled, forced in rls)
                evidence("qq-0080-roundtrip", {"path":["empty","0079","0080","head_fixture_setup","0080","0079","0080"],
                    "nonempty_0078_downgrade_refused":True, "rollback_runtime_read_sqlstate":"42501",
                    "preserved_rows_per_table":[1,1,1], "columns_constraints_triggers":before,
                    "final_policies":policies, "final_rls":rls})
            finally:
                await engine.dispose()
    finally:
        if target is not None:
            await target.dispose()
        try:
            if created:
                async with admin.connect() as connection:
                    connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
                    await connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        finally:
            await admin.dispose()


async def test_0086_existing_runtime_role_retains_privileges_after_upgrade(db_url):
    """只测本次QQ实际前向路径；旧表授权不会因未来表曾经存在而残留。"""
    name = "qq_upgrade_roles_" + uuid4().hex
    admin = create_engine_from(db_url)
    url = make_url(str(db_url)).set(database=name)
    target = None
    created = False
    try:
        async with admin.connect() as connection:
            connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
            created = True
        assert migrate(url, "upgrade", "head") == 0
        target = create_engine_from(url.render_as_string(False))
        async with runtime_database_scope(url.render_as_string(False)) as provision:
            tenant = new_id("tn")
            engine = create_engine_from(await provision(tenant))
            try:
                await assert_tenant_database_isolation(engine, tenant)
                assert migrate(url, "downgrade", "0086") == 0
                async with engine.connect() as connection:
                    assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "0086"
                    for table in ("reply_classification_resumptions", "reply_classification_retries"):
                        assert await connection.scalar(text(
                            "SELECT has_table_privilege(current_user,:table,'SELECT') AND "
                            "has_table_privilege(current_user,:table,'INSERT') AND "
                            "has_table_privilege(current_user,:table,'UPDATE') AND "
                            "has_table_privilege(current_user,:table,'DELETE')"
                        ), {"table":table})
                    assert await connection.scalar(text("SELECT to_regclass('public.dialogue_executions')")) is None
                assert migrate(url, "upgrade", "head") == 0
                await assert_tenant_database_isolation(engine, tenant)
                evidence("qq-0086-forward-role", {"path":["head_role_fixture","0086","head"],
                    "current_role_and_all_table_privileges_verified":True})
            finally:
                await engine.dispose()
    finally:
        if target is not None:
            await target.dispose()
        try:
            if created:
                async with admin.connect() as connection:
                    connection = await connection.execution_options(isolation_level="AUTOCOMMIT")
                    await connection.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        finally:
            await admin.dispose()
