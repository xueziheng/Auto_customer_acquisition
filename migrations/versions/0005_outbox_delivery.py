"""0005 outbox 投递持久化迁移（S3-8；0005 自管 guard，不修改已发布 0002）。

- ``outbox_events`` **加列**：``next_attempt_at``（下次可重试时间）、
  ``last_error``（脱敏错误）；加 ``UNIQUE(tenant_id, event_id)``
  （``outbox_deliveries`` 复合 FK 的前置契约）。
- 0005 **自管**兼容修复（F3/R7）：DROP 原 ``ck_outbox_status`` CHECK 后重建
  （白名单含 ``'pending'/'delivered'/'dead'``）；``CREATE OR REPLACE`` guard
  函数，只允许 ``status/delivered_at/attempt/next_attempt_at/last_error`` 变化。
- 新建 ``outbox_deliveries``：durable per-handler 投递状态，带
  ``UNIQUE(tenant_id, event_id, handler_name)`` + 复合 FK
  ``(tenant_id, event_id) → outbox_events``（禁止跨租户引用，硬边界 8）。
- **不修改已发布的 ``0002_opportunities``**；downgrade 恢复 0002 精确语义
  （加列/新表消失、status CHECK 回到仅 pending/delivered、guard 回到仅
  status/delivered_at 可变）；有存量 ``dead`` 行时先确定性映射为 ``pending``
  （0002 无 dead 态），再恢复 0002 CHECK，保证 downgrade 不因存量 dead 行失败。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """outbox_events 加列/加唯一 → 重建 status CHECK/guard → 建 outbox_deliveries。"""
    # ---- outbox_events 加列（投递重试与脱敏错误，0005 新增）-----------------
    op.add_column(
        "outbox_events",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "outbox_events",
        sa.Column("last_error", sa.Text(), nullable=True),
    )
    # ---- UNIQUE(tenant_id, event_id)：outbox_deliveries 复合 FK 前置契约 ----
    op.create_unique_constraint(
        "uq_outbox_events_tenant_event",
        "outbox_events",
        ["tenant_id", "event_id"],
    )
    # ---- 自管：DROP 重建 status CHECK（0002 白名单 → 含 'dead'）------------
    op.drop_constraint("ck_outbox_status", "outbox_events", type_="check")
    op.create_check_constraint(
        "ck_outbox_status",
        "outbox_events",
        "status IN ('pending', 'delivered', 'dead')",
    )
    # ---- 自管：CREATE OR REPLACE guard ------------------------------------
    # 允许 status/delivered_at/attempt/next_attempt_at/last_error 变化；
    # event_id/tenant_id/event_type/event_payload/published_at/trace_id/
    # run_id/occurred_at 仍只读。触发器等沿用 0002，不重建。
    op.execute(
        """
CREATE OR REPLACE FUNCTION outbox_events_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.event_id IS DISTINCT FROM OLD.event_id
       OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.event_type IS DISTINCT FROM OLD.event_type
       OR NEW.event_payload IS DISTINCT FROM OLD.event_payload
       OR NEW.published_at IS DISTINCT FROM OLD.published_at
       OR NEW.trace_id IS DISTINCT FROM OLD.trace_id
       OR NEW.run_id IS DISTINCT FROM OLD.run_id
       OR NEW.occurred_at IS DISTINCT FROM OLD.occurred_at
    THEN
        RAISE EXCEPTION 'outbox_events: only status/delivered_at/attempt/next_attempt_at/last_error may change';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""
    )
    # ---- outbox_deliveries（durable per-handler；复合 FK → outbox_events）---
    op.create_table(
        "outbox_deliveries",
        sa.Column("delivery_id", sa.String(32), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("event_id", sa.String(32), nullable=False),
        sa.Column("handler_name", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'dead')",
            name="ck_outbox_deliveries_status",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "event_id",
            "handler_name",
            name="uq_outbox_deliveries_tenant_event_handler",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "event_id"],
            ["outbox_events.tenant_id", "outbox_events.event_id"],
            name="fk_outbox_deliveries_event",
        ),
    )


def downgrade() -> None:
    """逆序回滚：dead 行确定性映射 → 删子表 → 恢复 0002 status CHECK/guard →
    删 UNIQUE → 删列。"""
    # 先确定性映射：'dead' 是 0005 新增状态，0002 无对应。存量 dead 行统一映射为
    # 'pending'（dead 事件从未投递成功，回退为待投递是唯一语义无损的选择）。必须
    # 在恢复 0002 CHECK 之前执行——0002 CHECK 只允许 pending/delivered，直接重建
    # 会因存量 dead 行违反新 CHECK 而失败。当前 0005 guard 允许 status 变化，此
    # UPDATE 可正常通过。
    op.execute(
        sa.text("UPDATE outbox_events SET status = 'pending' WHERE status = 'dead'")
    )
    op.drop_table("outbox_deliveries")
    # 恢复 0002 status CHECK（仅 pending/delivered）
    op.drop_constraint("ck_outbox_status", "outbox_events", type_="check")
    op.create_check_constraint(
        "ck_outbox_status",
        "outbox_events",
        "status IN ('pending', 'delivered')",
    )
    # 恢复 0002 guard 精确语义（仅 status/delivered_at 可变；attempt 仍只读）
    op.execute(
        """
CREATE OR REPLACE FUNCTION outbox_events_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.event_id IS DISTINCT FROM OLD.event_id
       OR NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.event_type IS DISTINCT FROM OLD.event_type
       OR NEW.event_payload IS DISTINCT FROM OLD.event_payload
       OR NEW.attempt IS DISTINCT FROM OLD.attempt
       OR NEW.published_at IS DISTINCT FROM OLD.published_at
       OR NEW.trace_id IS DISTINCT FROM OLD.trace_id
       OR NEW.run_id IS DISTINCT FROM OLD.run_id
       OR NEW.occurred_at IS DISTINCT FROM OLD.occurred_at
    THEN
        RAISE EXCEPTION 'outbox_events: only status/delivered_at may change';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""
    )
    op.drop_constraint("uq_outbox_events_tenant_event", "outbox_events", type_="unique")
    op.drop_column("outbox_events", "next_attempt_at")
    op.drop_column("outbox_events", "last_error")
