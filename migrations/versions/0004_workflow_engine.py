"""0004 workflow 引擎两表迁移（Schema 附录逐列）。

逐列对齐 docs/superpowers/plans/2026-08-08-slice3-opportunity-board-handoff.md
的 Schema 合同附录 0004，落地硬边界 8：
- 全表 ``tenant_id`` NOT NULL；主键为带前缀字符串 ID（VARCHAR(32)）。
- ``workflow_runs`` 有 ``UNIQUE(tenant_id, idempotency_key)``（start 幂等）
  与 ``UNIQUE(tenant_id, run_id)``（供子表复合 FK 引用）。
- ``workflow_steps`` 用复合 FK ``(tenant_id, run_id)
  REFERENCES workflow_runs(tenant_id, run_id) ON DELETE CASCADE``
  （禁止跨租户引用；删除 run 级联清理步骤）。
- 两个调度索引：``workflow_runs(tenant_id, status, next_poll_at)`` 与
  ``workflow_steps(tenant_id, status, due_at)``，供 ``poll_due`` 扫描。
- ``context``/``data`` 为 JSONB NOT NULL；时间一律 TIMESTAMPTZ；
  ``now()``/状态默认值用 server_default。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """按 Schema 附录建两表 + 约束 + 索引（先父表后子表）。"""
    # ---- workflow_runs ----------------------------------------------------
    op.create_table(
        "workflow_runs",
        sa.Column("run_id", sa.String(32), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("workflow_type", sa.String(64), nullable=False),
        sa.Column("workflow_version", sa.Integer(), nullable=False),
        sa.Column("subject_ref", sa.String(64), nullable=False),
        sa.Column("current_step", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'running'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("context", postgresql.JSONB(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_workflow_runs_tenant_key"),
        sa.UniqueConstraint("tenant_id", "run_id", name="uq_workflow_runs_tenant_run"),
    )
    op.create_index(
        "ix_workflow_runs_tenant_status_poll",
        "workflow_runs",
        ["tenant_id", "status", "next_poll_at"],
    )

    # ---- workflow_steps（复合 FK → workflow_runs）--------------------------
    op.create_table(
        "workflow_steps",
        sa.Column("step_id", sa.String(32), primary_key=True, nullable=False),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("step_name", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("data", postgresql.JSONB(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["workflow_runs.tenant_id", "workflow_runs.run_id"],
            ondelete="CASCADE",
            name="fk_workflow_steps_run",
        ),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_workflow_steps_tenant_key"),
    )
    op.create_index(
        "ix_workflow_steps_tenant_status_due",
        "workflow_steps",
        ["tenant_id", "status", "due_at"],
    )


def downgrade() -> None:
    """逆序删除：子表先于父表 → 索引。"""
    op.drop_index("ix_workflow_steps_tenant_status_due", table_name="workflow_steps")
    op.drop_table("workflow_steps")
    op.drop_index("ix_workflow_runs_tenant_status_poll", table_name="workflow_runs")
    op.drop_table("workflow_runs")
