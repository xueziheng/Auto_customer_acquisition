"""公司待分配客户和显式归属，保留历史负责人。"""

import sqlalchemy as sa
from alembic import op

revision = "0075"
down_revision = "0074"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """历史归属不变；新取得记录可为空，分配审计和客户映射按公司保存。"""
    op.alter_column("candidate_allocations", "employee_id", nullable=True)
    op.add_column(
        "candidate_allocations",
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "candidate_allocations", sa.Column("assigned_by", sa.String(40), nullable=True)
    )
    op.add_column(
        "candidate_allocations", sa.Column("account_id", sa.String(40), nullable=True)
    )
    op.create_foreign_key(
        "fk_allocation_account",
        "candidate_allocations",
        "prospect_accounts",
        ["tenant_id", "account_id"],
        ["tenant_id", "account_id"],
    )


def downgrade() -> None:
    """拒绝丢失待分配状态、分配审计或客户映射；仅旧契约数据可降级。"""
    changed = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM candidate_allocations "
            "WHERE employee_id IS NULL OR assigned_at IS NOT NULL "
            "OR assigned_by IS NOT NULL OR account_id IS NOT NULL)"
        )
    )
    if changed:
        raise RuntimeError("存在待分配客户、分配审计或客户映射，不能无损降级0075")
    op.drop_constraint(
        "fk_allocation_account", "candidate_allocations", type_="foreignkey"
    )
    op.drop_column("candidate_allocations", "account_id")
    op.drop_column("candidate_allocations", "assigned_by")
    op.drop_column("candidate_allocations", "assigned_at")
    op.alter_column("candidate_allocations", "employee_id", nullable=False)
