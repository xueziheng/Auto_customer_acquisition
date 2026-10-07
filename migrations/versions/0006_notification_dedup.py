"""0006 notification_deliveries 迁移（S3-9 通知去重持久化）。

逐列对齐 docs/superpowers/plans/2026-08-08-slice3-opportunity-board-handoff.md
Schema 合同附录 0006，落地硬边界 8：
- ``notification_deliveries``：durable per-channel 投递去重状态（delivery_id PK、
  tenant_id、dedup_key VARCHAR(200)、channel_name VARCHAR(64)、status VARCHAR(16)
  NOT NULL DEFAULT 'pending'、attempts INT NOT NULL DEFAULT 0、claim_token VARCHAR(32) NULL、
  next_attempt_at TIMESTAMPTZ NULL、last_error TEXT NULL、delivered_at TIMESTAMPTZ
  NULL）。全表带 ``tenant_id``（硬边界 8）。
- ``UNIQUE(tenant_id, dedup_key, channel_name)``：同一通知同一渠道只允许一行——
  部分渠道失败可 durable resume（仅续投失败渠道，不重复投递已成功渠道）。
- ``status``/``attempts`` 走 server_default：pending / 0。
- downgrade 直接删表，round-trip 回 0005。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """按 Schema 附录建 notification_deliveries（单表，无 FK/触发器）。"""
    op.create_table(
        "notification_deliveries",
        sa.Column("delivery_id", sa.String(32), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("dedup_key", sa.String(200), nullable=False),
        sa.Column("channel_name", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("claim_token", sa.String(32), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint(
            "tenant_id",
            "dedup_key",
            "channel_name",
            name="uq_notification_deliveries_tenant_dedup_channel",
        ),
    )


def downgrade() -> None:
    """逆序删除：notification_deliveries 表消失（0006 无列/约束可单独回退）。"""
    op.drop_table("notification_deliveries")
