"""独立平台授权、企业目录及概览审计，延续固定数据库角色的双策略隔离。"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None

_TABLES = ("platform_admin_grants", "platform_enterprises", "platform_access_audits")
_PREDICATE = (
    "current_user::text = 'tradeos_t_' || "
    "pg_catalog.substr(pg_catalog.encode(pg_catalog.sha256("
    "pg_catalog.convert_to(tenant_id, 'UTF8')), 'hex'), 1, 48)"
)


def upgrade() -> None:
    """仅新增控制平面表，不改变企业员工角色或授予任何默认平台权限。"""
    op.create_table(
        "platform_admin_grants",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("employee_id", sa.String(32), nullable=False),
        sa.Column("user_id", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "employee_id", name="pk_platform_admin_grants"),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_platform_admin_grants_user"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"], ["employees.tenant_id", "employees.employee_id"],
            name="fk_platform_admin_grants_employee", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "user_id"], ["employees.tenant_id", "employees.user_id"],
            name="fk_platform_admin_grants_user", ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "platform_enterprises",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("enterprise_tenant_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "enterprise_tenant_id", name="pk_platform_enterprises"),
        sa.CheckConstraint("tenant_id <> enterprise_tenant_id", name="ck_platform_enterprises_distinct"),
        sa.CheckConstraint("btrim(name) <> ''", name="ck_platform_enterprises_name"),
    )
    op.create_table(
        "platform_access_audits",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("audit_id", sa.String(32), nullable=False),
        sa.Column("actor_employee_id", sa.String(32), nullable=False),
        sa.Column("actor_user_id", sa.String(32), nullable=False),
        sa.Column("target_tenant_id", sa.String(32), nullable=True),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "audit_id", name="pk_platform_access_audits"),
        sa.CheckConstraint("action = 'overview'", name="ck_platform_access_audits_action"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "actor_employee_id"], ["employees.tenant_id", "employees.employee_id"],
            name="fk_platform_access_audits_employee", ondelete="RESTRICT",
        ),
    )
    for name in _TABLES:
        table = 'public."' + name + '"'
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tradeos_tenant_allow ON {table} AS PERMISSIVE "
            f"FOR ALL TO PUBLIC USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )
        op.execute(
            f"CREATE POLICY tradeos_tenant_guard ON {table} AS RESTRICTIVE "
            f"FOR ALL TO PUBLIC USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )

    op.execute("""
        DO $block$
        DECLARE account record;
        BEGIN
            FOR account IN SELECT rolname FROM pg_catalog.pg_roles
                WHERE rolname ~ '^tradeos_t_[0-9a-f]{48}$'
            LOOP
                EXECUTE format(
                    'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.platform_admin_grants, public.platform_enterprises, public.platform_access_audits TO %I',
                    account.rolname
                );
            END LOOP;
        END
        $block$
    """)


def downgrade() -> None:
    """删除本迁移新增控制表；不修改既有企业表及其 RLS。"""
    for name in reversed(_TABLES):
        op.drop_table(name)
