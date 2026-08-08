"""0003 employees 持久化四表迁移（Schema 附录逐列）。

逐列对齐 docs/superpowers/plans/2026-08-08-slice3-opportunity-board-handoff.md
Schema 合同附录，落地硬边界 8：
- 全表 ``tenant_id`` NOT NULL；主键为带前缀字符串 ID（VARCHAR(32)）。
- ``employees`` 有 ``UNIQUE(tenant_id, employee_id)`` 供子表复合 FK 引用。
- ``territory_assignments`` 的 employee_id/manager_id/backup_employee_id、
  ``ownership_transfer_history`` 的 from_owner/to_owner/transferred_by 均用
  复合 FK ``(tenant_id, ...) REFERENCES employees(tenant_id, employee_id)``
  （禁止跨租户引用）。
- ``ownership_locks`` 用 ``UNIQUE(tenant_id, account_id)`` 真正支撑并发 try_lock。
- ``ownership_transfer_history`` **只增**：``BEFORE UPDATE OR DELETE`` 触发器拒绝，
  ``reason`` 用 ``CHECK btrim(reason) <> ''``（去空白后非空）。
- TEXT[] 数组列带 ``DEFAULT '{}'``；时间一律 TIMESTAMPTZ。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """按 Schema 附录建四表 + 约束 + 触发器（先表后触发器函数）。"""
    # ---- employees --------------------------------------------------------
    op.create_table(
        "employees",
        sa.Column("employee_id", sa.String(32), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("user_id", sa.String(32), nullable=True),
        sa.Column("team_id", sa.String(32), nullable=True),
        sa.Column("manager_id", sa.String(32), nullable=True),
        sa.Column("languages", postgresql.ARRAY(sa.String()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("max_active_accounts", sa.Integer(), nullable=True),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_employees_tenant_user"),
        sa.UniqueConstraint("tenant_id", "employee_id", name="uq_employees_tenant_employee"),
    )
    op.create_index("ix_employees_tenant_role", "employees", ["tenant_id", "role"])
    op.create_index("ix_employees_tenant_active", "employees", ["tenant_id", "is_active"])

    # ---- territory_assignments（复合 FK → employees）-----------------------
    op.create_table(
        "territory_assignments",
        sa.Column("assignment_id", sa.String(32), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("employee_id", sa.String(32), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("countries", postgresql.ARRAY(sa.String()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("product_categories", postgresql.ARRAY(sa.String()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("need_categories", postgresql.ARRAY(sa.String()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("buyer_types", postgresql.ARRAY(sa.String()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("languages", postgresql.ARRAY(sa.String()), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("manager_id", sa.String(32), nullable=True),
        sa.Column("backup_employee_id", sa.String(32), nullable=True),
        sa.Column("effective_until", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "employee_id"],
            ["employees.tenant_id", "employees.employee_id"],
            name="fk_territory_employee",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "manager_id"],
            ["employees.tenant_id", "employees.employee_id"],
            name="fk_territory_manager",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "backup_employee_id"],
            ["employees.tenant_id", "employees.employee_id"],
            name="fk_territory_backup",
        ),
    )
    op.create_index("ix_territory_tenant_priority", "territory_assignments", ["tenant_id", "priority"])

    # ---- ownership_locks（UNIQUE(tenant_id, account_id) 支撑并发 try_lock）-
    op.create_table(
        "ownership_locks",
        sa.Column("lock_id", sa.String(32), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("account_id", sa.String(32), nullable=False),
        sa.Column("owner", sa.String(32), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_by_rule", sa.String(64), nullable=False),
        sa.UniqueConstraint("tenant_id", "account_id", name="uq_ownership_locks_tenant_account"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "owner"],
            ["employees.tenant_id", "employees.employee_id"],
            name="fk_ownership_locks_owner",
        ),
    )

    # ---- ownership_transfer_history（只增；reason 非空 CHECK）--------------
    op.create_table(
        "ownership_transfer_history",
        sa.Column("transfer_id", sa.String(32), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("account_id", sa.String(32), nullable=False),
        sa.Column("from_owner", sa.String(32), nullable=True),
        sa.Column("to_owner", sa.String(32), nullable=False),
        sa.Column("transferred_by", sa.String(32), nullable=False),
        sa.Column("transferred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.CheckConstraint("btrim(reason) <> ''", name="ck_transfer_reason_nonblank"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "from_owner"],
            ["employees.tenant_id", "employees.employee_id"],
            name="fk_transfer_from_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "to_owner"],
            ["employees.tenant_id", "employees.employee_id"],
            name="fk_transfer_to_owner",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "transferred_by"],
            ["employees.tenant_id", "employees.employee_id"],
            name="fk_transfer_transferred_by",
        ),
    )
    op.create_index("ix_transfer_tenant_account", "ownership_transfer_history", ["tenant_id", "account_id"])

    # ---- 只增触发器：ownership_transfer_history（BEFORE UPDATE OR DELETE 一律抛错）
    op.execute(
        """
CREATE FUNCTION ownership_transfer_history_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'ownership_transfer_history is append-only';
END;
$$ LANGUAGE plpgsql;
"""
    )
    op.execute(
        """
CREATE TRIGGER ownership_transfer_history_append_only
BEFORE UPDATE OR DELETE ON ownership_transfer_history
FOR EACH ROW EXECUTE FUNCTION ownership_transfer_history_append_only();
"""
    )


def downgrade() -> None:
    """逆序删除：子表先于父表 → 触发器函数。"""
    op.drop_index("ix_transfer_tenant_account", table_name="ownership_transfer_history")
    op.drop_table("ownership_transfer_history")
    op.drop_table("ownership_locks")
    op.drop_index("ix_territory_tenant_priority", table_name="territory_assignments")
    op.drop_table("territory_assignments")
    op.drop_index("ix_employees_tenant_active", table_name="employees")
    op.drop_index("ix_employees_tenant_role", table_name="employees")
    op.drop_table("employees")
    op.execute("DROP FUNCTION ownership_transfer_history_append_only();")
