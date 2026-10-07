"""按真实 PostgreSQL 登录角色强制隔离企业；本模块不记录连接或凭证。"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from shared.errors import TenantIsolationViolation

_POLICY_NAMES = {"tradeos_tenant_allow": True, "tradeos_tenant_guard": False}
_PREDICATE = (
    "current_user::text = 'tradeos_t_' || "
    "substr(encode(sha256(convert_to(tenant_id, 'UTF8')), 'hex'), 1, 48)"
)


def tenant_database_role(tenant_id: str) -> str:
    """由完整企业 ID 派生不可由请求配置改写的固定 PostgreSQL 角色名。"""
    if (
        not isinstance(tenant_id, str)
        or not tenant_id
        or tenant_id != tenant_id.strip()
        or "\x00" in tenant_id
    ):
        raise TenantIsolationViolation("数据库必须绑定有效企业")
    try:
        digest = hashlib.sha256(tenant_id.encode("utf-8")).hexdigest()
    except UnicodeError:
        raise TenantIsolationViolation("数据库必须绑定有效企业") from None
    return "tradeos_t_" + digest[:48]


def _identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _expression_tokens(expression: str) -> tuple[str, ...]:
    """只消除 PostgreSQL 的显示型 cast 和括号；额外运算/常量不能通过。"""
    expression = re.sub(r"::(?:text|name)\b", "", expression, flags=re.IGNORECASE)
    tokens = re.findall(r"'(?:[^']|'')*'|[a-zA-Z_][a-zA-Z_0-9.]*|[0-9]+|\|\||[^\s]", expression)
    return tuple(
        item if item.startswith("'") else item.lower().removeprefix("pg_catalog.")
        for item in tokens if item not in {"(", ")"}
    )


_ROLE_QUERY = text("""
SELECT r.rolname, r.rolsuper, r.rolinherit, r.rolcreaterole, r.rolcreatedb,
       r.rolcanlogin, r.rolreplication, r.rolbypassrls,
       current_user::text AS current_role, session_user::text AS login_role,
       EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m
               WHERE m.member = r.oid OR m.roleid = r.oid) AS membership,
       (EXISTS (SELECT 1 FROM pg_catalog.pg_class c WHERE c.relowner = r.oid)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_namespace n WHERE n.nspowner = r.oid)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_database d WHERE d.datdba = r.oid)
        OR EXISTS (SELECT 1 FROM pg_catalog.pg_proc p WHERE p.proowner = r.oid))
        AS owns_objects,
       pg_catalog.has_schema_privilege(r.oid, 'public', 'CREATE') AS schema_create,
       pg_catalog.has_database_privilege(r.oid, current_database(), 'CREATE')
        AS database_create
FROM pg_catalog.pg_roles r WHERE r.rolname = :role
""")

_TABLES_QUERY = text("""
SELECT c.oid, c.relname, a.attnotnull, c.relrowsecurity, c.relforcerowsecurity
FROM pg_catalog.pg_class c
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
  AND a.attname = 'tenant_id' AND NOT a.attisdropped
ORDER BY c.relname
""")

_POLICIES_QUERY = text("""
SELECT c.relname, p.polname, p.polcmd::text AS polcmd, p.polpermissive,
       p.polroles = ARRAY[0::oid] AS public_policy,
       pg_catalog.pg_get_expr(p.polqual, p.polrelid) AS using_expression,
       pg_catalog.pg_get_expr(p.polwithcheck, p.polrelid) AS check_expression
FROM pg_catalog.pg_policy p
JOIN pg_catalog.pg_class c ON c.oid = p.polrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' ORDER BY c.relname, p.polname
""")


def _reject(message: str = "数据库企业隔离配置不完整") -> None:
    raise TenantIsolationViolation(message)


def _check_role(row: Mapping[str, Any], role: str, *, runtime: bool) -> None:
    if row["rolname"] != role or not row["rolcanlogin"]:
        _reject()
    if any(row[name] for name in (
        "rolsuper", "rolinherit", "rolcreaterole", "rolcreatedb", "rolreplication",
        "rolbypassrls", "membership", "owns_objects", "schema_create", "database_create",
    )):
        _reject()
    if runtime and (row["current_role"] != role or row["login_role"] != role):
        _reject("数据库连接未使用本企业专用登录身份")


def _tenant_table_names() -> set[str]:
    """完整注册分文件 ORM 映射，不能依赖进程恰好已加载某个业务入口。"""
    from infra.db.mailbox_tables import MailboxAccountRow, MailboxMessageRow
    from infra.db.tables import Base

    if any(
        model.__table__.metadata is not Base.metadata
        for model in (MailboxAccountRow, MailboxMessageRow)
    ):
        _reject()
    return set(Base.metadata.tables)


async def _check_tables(connection: AsyncConnection) -> list[Mapping[str, Any]]:
    expected = _tenant_table_names()
    rows = (await connection.execute(_TABLES_QUERY)).mappings().all()
    if {row["relname"] for row in rows} != expected:
        _reject()
    if any(not all(row[key] for key in (
        "attnotnull", "relrowsecurity", "relforcerowsecurity",
    )) for row in rows):
        _reject()
    policies = (await connection.execute(_POLICIES_QUERY)).mappings().all()
    predicate = _expression_tokens(_PREDICATE)
    by_table: dict[str, list[Mapping[str, Any]]] = {}
    for policy in policies:
        if policy["relname"] in expected:
            by_table.setdefault(policy["relname"], []).append(policy)
    for table in expected:
        entries = by_table.get(table, [])
        if len(entries) != 2 or {p["polname"] for p in entries} != set(_POLICY_NAMES):
            _reject()
        for policy in entries:
            if (
                policy["polcmd"] != "*"
                or policy["polpermissive"] != _POLICY_NAMES[policy["polname"]]
                or not policy["public_policy"]
                or _expression_tokens(policy["using_expression"] or "") != predicate
                or _expression_tokens(policy["check_expression"] or "") != predicate
            ):
                _reject()
    return list(rows)


async def _check_table_privileges(
    connection: AsyncConnection, role: str, tables: list[Mapping[str, Any]],
) -> None:
    """一次读取全部业务表的权限；漏行、额外行或任何提升权限都失败关闭。"""
    expected_oids = {table["oid"] for table in tables}
    if not expected_oids:
        _reject()
    rows = (await connection.execute(text("""
        SELECT c.oid,
            pg_catalog.has_table_privilege(CAST(:role AS name), c.oid, 'SELECT')
            AND pg_catalog.has_table_privilege(CAST(:role AS name), c.oid, 'INSERT')
            AND pg_catalog.has_table_privilege(CAST(:role AS name), c.oid, 'UPDATE')
            AND pg_catalog.has_table_privilege(CAST(:role AS name), c.oid, 'DELETE') AS dml,
            pg_catalog.has_table_privilege(CAST(:role AS name), c.oid, 'TRUNCATE')
            OR pg_catalog.has_table_privilege(CAST(:role AS name), c.oid, 'REFERENCES')
            OR pg_catalog.has_table_privilege(CAST(:role AS name), c.oid, 'TRIGGER')
            OR pg_catalog.has_table_privilege(CAST(:role AS name), c.oid,
                'SELECT WITH GRANT OPTION,INSERT WITH GRANT OPTION,UPDATE WITH GRANT OPTION,DELETE WITH GRANT OPTION') AS unsafe
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.oid = ANY(CAST(:oids AS oid[]))
    """), {"role": role, "oids": sorted(expected_oids)})).mappings().all()
    if (
        len(rows) != len(expected_oids)
        or {row["oid"] for row in rows} != expected_oids
        or any(not row["dml"] or row["unsafe"] for row in rows)
    ):
        _reject()


async def assert_tenant_database_isolation(engine: AsyncEngine, tenant_id: str) -> None:
    """启动门禁：真实登录身份、无提升权限、全部表策略及 DML 权限必须成立。"""
    role = tenant_database_role(tenant_id)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("SET LOCAL search_path = pg_catalog, public"))
            row = (await connection.execute(_ROLE_QUERY, {"role": role})).mappings().one_or_none()
            if row is None:
                _reject()
            _check_role(row, role, runtime=True)
            tables = await _check_tables(connection)
            await _check_table_privileges(connection, role, tables)
            version = (await connection.execute(text("""
                SELECT pg_catalog.has_table_privilege(CAST(:role AS name), 'public.alembic_version', 'SELECT') AS readable,
                       pg_catalog.has_table_privilege(CAST(:role AS name), 'public.alembic_version',
                           'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') AS mutable
            """), {"role": role})).mappings().one()
            if not version["readable"] or version["mutable"]:
                _reject()
    except TenantIsolationViolation:
        raise
    except Exception:  # noqa: BLE001 -- 数据库异常可能携带凭证参数，必须固定脱敏
        raise TenantIsolationViolation("数据库企业隔离验证失败") from None


_ROLE_FUNCTION = """
CREATE OR REPLACE FUNCTION pg_temp.tradeos_configure_tenant_role(
    role_name text, role_password text, already_exists boolean
) RETURNS void LANGUAGE plpgsql SECURITY INVOKER AS $function$
BEGIN
    IF role_name !~ '^tradeos_t_[0-9a-f]{48}$' THEN
        RAISE EXCEPTION 'invalid tenant role';
    END IF;
    IF NOT already_exists THEN
        EXECUTE format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS', role_name);
    END IF;
    EXECUTE format('ALTER ROLE %I PASSWORD %L', role_name, role_password);
END
$function$
"""


async def provision_tenant_role(
    connection: AsyncConnection, tenant_id: str, password: SecretStr,
    *, create_only: bool = False,
) -> None:
    """可信迁移连接准备最小运行角色；密码绑定传给临时函数，不进入 SQL 模板或日志。"""
    role = tenant_database_role(tenant_id)
    if not isinstance(password, SecretStr):
        _reject("数据库角色密码无效")
    secret = password.get_secret_value()
    if not secret or "\x00" in secret:
        _reject("数据库角色密码无效")
    try:
        await connection.execute(text("SET LOCAL search_path = pg_catalog, public"))
        tables = await _check_tables(connection)
        row = (await connection.execute(_ROLE_QUERY, {"role": role})).mappings().one_or_none()
        if row is not None:
            if create_only:
                _reject("数据库企业运行角色已存在")
            _check_role(row, role, runtime=False)
        await connection.execute(text(_ROLE_FUNCTION))
        await connection.execute(text(
            "SELECT pg_temp.tradeos_configure_tenant_role(:role, :password, :exists)"
        ), {"role": role, "password": secret, "exists": row is not None})
        await connection.execute(text(
            "DROP FUNCTION pg_temp.tradeos_configure_tenant_role(text, text, boolean)"
        ))
        quoted_role = _identifier(role)
        await connection.execute(text(f"GRANT USAGE ON SCHEMA public TO {quoted_role}"))
        quoted_tables = ", ".join(
            "public." + _identifier(table["relname"]) for table in tables
        )
        await connection.execute(text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {quoted_tables} TO {quoted_role}"
        ))
        await connection.execute(text(
            f"GRANT SELECT ON TABLE public.alembic_version TO {quoted_role}"
        ))
    except TenantIsolationViolation:
        raise
    except Exception:  # noqa: BLE001 -- 数据库异常可能携带凭证参数，必须固定脱敏
        raise TenantIsolationViolation("数据库企业运行角色准备失败") from None
