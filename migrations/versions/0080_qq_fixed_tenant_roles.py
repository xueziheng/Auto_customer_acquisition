"""QQ 账本接入固定企业角色策略；保留 0079 和全部证据约束。"""
from alembic import op

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None

_TABLES = ("qq_sent_aliases", "qq_reply_trials", "qq_reply_trial_events")
_PREDICATE = (
    "current_user::text = 'tradeos_t_' || "
    "pg_catalog.substr(pg_catalog.encode(pg_catalog.sha256("
    "pg_catalog.convert_to(tenant_id, 'UTF8')), 'hex'), 1, 48)"
)


def _require_policies(name: str, policies: tuple[str, ...]) -> None:
    """未知策略状态拒绝迁移；不能静默遗留额外的放行策略。"""
    expected = ", ".join("'" + policy + "'" for policy in sorted(policies))
    op.execute(f"""
        DO $block$
        DECLARE actual text[];
        BEGIN
            SELECT array_agg(polname::text ORDER BY polname::text) INTO actual
            FROM pg_catalog.pg_policy WHERE polrelid = 'public.{name}'::regclass;
            IF actual IS DISTINCT FROM ARRAY[{expected}]::text[] THEN
                RAISE EXCEPTION 'unexpected QQ tenant policies';
            END IF;
        END $block$
    """)


def upgrade() -> None:
    """新库或既有 0079 库均逐步升级；不重建表、不改证据或业务角色属性。"""
    tables = ", ".join(f'public."{name}"' for name in _TABLES)
    op.execute(f"LOCK TABLE {tables} IN ACCESS EXCLUSIVE MODE")
    for name in _TABLES:
        _require_policies(name, (name + "_tenant",))
        table = f'public."{name}"'
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tradeos_tenant_guard ON {table} AS RESTRICTIVE "
            f"FOR ALL TO PUBLIC USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )
        op.execute(
            f"CREATE POLICY tradeos_tenant_allow ON {table} AS PERMISSIVE "
            f"FOR ALL TO PUBLIC USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )
        op.execute(f'DROP POLICY "{name}_tenant" ON {table}')
    # 与 0071/0072 的新增表授权相同；不授予 TRUNCATE、DDL、grant option 或角色特权。
    op.execute(f"""
        DO $block$
        DECLARE account record;
        BEGIN
            FOR account IN SELECT rolname FROM pg_catalog.pg_roles
                WHERE rolname ~ '^tradeos_t_[0-9a-f]{{48}}$'
            LOOP
                EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {tables} TO %I', account.rolname);
            END LOOP;
        END $block$
    """)


def downgrade() -> None:
    """保留数据；恢复旧策略前撤销三张表的非所有者授权，回退保持拒绝访问。"""
    tables = ", ".join(f'public."{name}"' for name in _TABLES)
    op.execute(f"LOCK TABLE {tables} IN ACCESS EXCLUSIVE MODE")
    for name in _TABLES:
        _require_policies(name, ("tradeos_tenant_allow", "tradeos_tenant_guard"))
        table = f'public."{name}"'
        # 包含 PUBLIC 与自定义直接授权；有未解决的授权依赖时由 PostgreSQL 拒绝并回滚。
        op.execute(f"""
            DO $block$
            DECLARE account record;
            BEGIN
                FOR account IN
                    SELECT DISTINCT acl.grantee, r.rolname
                    FROM pg_catalog.pg_class c
                    CROSS JOIN LATERAL pg_catalog.aclexplode(
                        COALESCE(c.relacl, pg_catalog.acldefault('r', c.relowner))
                    ) acl
                    LEFT JOIN pg_catalog.pg_roles r ON r.oid = acl.grantee
                    WHERE c.oid = 'public.{name}'::regclass AND acl.grantee <> c.relowner
                LOOP
                    IF account.grantee = 0 THEN
                        REVOKE ALL PRIVILEGES ON TABLE {table} FROM PUBLIC;
                    ELSE
                        EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE {table} FROM %I', account.rolname);
                    END IF;
                END LOOP;
            END $block$
        """)
        op.execute(f"DROP POLICY tradeos_tenant_allow ON {table}")
        op.execute(f"DROP POLICY tradeos_tenant_guard ON {table}")
        op.execute(
            f'CREATE POLICY "{name}_tenant" ON {table} '
            "USING (tenant_id = current_setting('app.tenant_id', true)) "
            "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"
        )
