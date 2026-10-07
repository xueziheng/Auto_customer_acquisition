"""0002 opportunities 持久化六表迁移（Schema 附录逐列）。

逐列对齐 docs/superpowers/plans/2026-08-08-slice2-opportunities-persistence.md
的 Schema 合同附录，落地硬边界 2/4/5/8：
- 全表 ``tenant_id`` NOT NULL；主键为带前缀字符串 ID（VARCHAR(32)）。
- 金额 ``NUMERIC(18,2)`` + ``CHAR(3)`` 成对（CHECK 强制）。
- ``handoffs``/``loss_records`` 用复合 FK ``(tenant_id, opportunity_id)``
  ``REFERENCES opportunities`` ``ON DELETE RESTRICT``（禁止跨租户引用）。
- ``score_snapshots.opportunity_id`` **无 FK**：失败门槛快照可悬空保留。
- 三张审计表（score_snapshots/loss_records/provenance_records）由
  ``BEFORE UPDATE OR DELETE`` 触发器强制只增。
- ``outbox_events`` **非只增**：另设 ``BEFORE UPDATE`` guard，只允许
  ``status``/``delivered_at`` 变化；``attempt >= 1``、``status`` 白名单用 CHECK。
- 时间一律 TIMESTAMPTZ；``now()``/状态默认值用 server_default。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """按 Schema 附录建六表 + 约束 + 触发器（先表后触发器函数）。"""
    # ---- opportunities ---------------------------------------------------
    op.create_table(
        "opportunities",
        sa.Column("opportunity_id", sa.String(32), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("account_id", sa.String(32), nullable=False),
        sa.Column("account_name", sa.String(200), nullable=False),
        sa.Column("country", sa.String(100), nullable=False),
        sa.Column("need_id", sa.String(32), nullable=False),
        sa.Column("product_category", sa.String(100), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default=sa.text("'qualified'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("quantity", sa.Integer(), nullable=True),
        sa.Column("spec_summary", sa.Text(), nullable=True),
        sa.Column("application", sa.Text(), nullable=True),
        sa.Column("destination", sa.String(100), nullable=True),
        sa.Column("required_by", sa.Date(), nullable=True),
        sa.Column("target_price_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("target_price_currency", sa.CHAR(3), nullable=True),
        sa.Column("decision_maker", sa.Text(), nullable=True),
        sa.Column("current_supply_solution", sa.Text(), nullable=True),
        sa.Column("current_supply_problem", sa.Text(), nullable=True),
        sa.Column("can_source", sa.Boolean(), nullable=True),
        sa.Column("estimated_cost_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("estimated_cost_currency", sa.CHAR(3), nullable=True),
        sa.Column("estimated_profit_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("estimated_profit_currency", sa.CHAR(3), nullable=True),
        sa.Column("owner", sa.String(32), nullable=True),
        sa.Column("assigned_by", sa.String(32), nullable=True),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_action", sa.Text(), nullable=True),
        sa.Column("next_action_due", sa.DateTime(timezone=True), nullable=True),
        sa.Column("loss_reason", sa.String(32), nullable=True),
        sa.Column("died_at_state", sa.String(20), nullable=True),
        sa.Column("closed_by", sa.String(32), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "need_id", name="uq_opportunities_tenant_need"),
        sa.UniqueConstraint("tenant_id", "opportunity_id", name="uq_opportunities_tenant_opp"),
        sa.CheckConstraint(
            "(target_price_amount IS NULL AND target_price_currency IS NULL) OR "
            "(target_price_amount IS NOT NULL AND target_price_currency IS NOT NULL)",
            name="ck_opportunities_target_price_pair",
        ),
        sa.CheckConstraint(
            "(estimated_cost_amount IS NULL AND estimated_cost_currency IS NULL) OR "
            "(estimated_cost_amount IS NOT NULL AND estimated_cost_currency IS NOT NULL)",
            name="ck_opportunities_estimated_cost_pair",
        ),
        sa.CheckConstraint(
            "(estimated_profit_amount IS NULL AND estimated_profit_currency IS NULL) OR "
            "(estimated_profit_amount IS NOT NULL AND estimated_profit_currency IS NOT NULL)",
            name="ck_opportunities_estimated_profit_pair",
        ),
        sa.CheckConstraint(
            "state <> 'lost' OR (loss_reason IS NOT NULL AND died_at_state IS NOT NULL "
            "AND closed_by IS NOT NULL AND closed_at IS NOT NULL)",
            name="ck_opportunities_lost_closed",
        ),
        sa.CheckConstraint(
            "state <> 'won' OR (closed_by IS NOT NULL AND closed_at IS NOT NULL)",
            name="ck_opportunities_won_closed",
        ),
        sa.CheckConstraint(
            "closed_at IS NULL OR state IN ('won', 'lost')",
            name="ck_opportunities_closed_only_terminal",
        ),
    )
    op.create_index("ix_opportunities_tenant_state", "opportunities", ["tenant_id", "state"])
    op.create_index("ix_opportunities_tenant_owner_state", "opportunities", ["tenant_id", "owner", "state"])

    # ---- score_snapshots（只增；opportunity_id 无 FK，悬空依据见计划附录）---
    op.create_table(
        "score_snapshots",
        sa.Column("snapshot_id", sa.String(32), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("opportunity_id", sa.String(32), nullable=False),  # 无 FK：失败门槛快照可悬空
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scorer_version", sa.String(32), nullable=False),
        sa.Column("passed_gates", postgresql.JSONB(), nullable=False),
        sa.Column("failed_gates", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_tier", sa.String(32), nullable=True),
        sa.Column("estimated_value_amount", sa.Numeric(18, 2), nullable=True),
        sa.Column("estimated_value_currency", sa.CHAR(3), nullable=True),
        sa.Column("supply_available", sa.Boolean(), nullable=True),
        sa.Column("sort_evidence_rank", sa.Integer(), nullable=False),
        sa.Column("sort_value_band", sa.Integer(), nullable=False),
        sa.Column("sort_supply_rank", sa.Integer(), nullable=False),
        sa.Column("gate_reasons", postgresql.JSONB(), nullable=False),
        sa.Column("rank_bucket", sa.String(10), nullable=False),
        sa.CheckConstraint(
            "(estimated_value_amount IS NULL AND estimated_value_currency IS NULL) OR "
            "(estimated_value_amount IS NOT NULL AND estimated_value_currency IS NOT NULL)",
            name="ck_score_snapshots_value_pair",
        ),
    )
    op.create_index("ix_score_snapshots_tenant_opp_scored", "score_snapshots", ["tenant_id", "opportunity_id", "scored_at"])

    # ---- handoffs（复合 FK → opportunities）-------------------------------
    op.create_table(
        "handoffs",
        sa.Column("handoff_id", sa.String(32), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("opportunity_id", sa.String(32), nullable=False),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default=sa.text("'requested'")),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("account_name", sa.String(200), nullable=False),
        sa.Column("country", sa.String(100), nullable=False),
        sa.Column("why_valuable", sa.Text(), nullable=False),
        sa.Column("customer_verbatim", sa.Text(), nullable=False),
        sa.Column("assigned_to", sa.String(32), nullable=True),
        sa.Column("manager", sa.String(32), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_by", sa.String(32), nullable=True),
        sa.Column("how_we_found_them", sa.Text(), nullable=True),
        sa.Column("validated_need_summary", sa.Text(), nullable=True),
        sa.Column("missing_information", postgresql.JSONB(), nullable=True),
        sa.Column("already_sent", postgresql.JSONB(), nullable=True),
        sa.Column("commitments_made", postgresql.JSONB(), nullable=True),
        sa.Column("evidence_links", postgresql.JSONB(), nullable=True),
        sa.Column("conversation_summary", sa.Text(), nullable=True),
        sa.Column("suggested_next_step", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "opportunity_id"],
            ["opportunities.tenant_id", "opportunities.opportunity_id"],
            ondelete="RESTRICT",
            name="fk_handoffs_opportunity",
        ),
    )
    op.create_index("ix_handoffs_tenant_state_requested", "handoffs", ["tenant_id", "state", "requested_at"])

    # ---- loss_records（复合 FK → opportunities；只增；人工确认必留痕）------
    op.create_table(
        "loss_records",
        sa.Column("loss_record_id", sa.String(32), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("opportunity_id", sa.String(32), nullable=False),
        sa.Column("loss_reason", sa.String(32), nullable=False),
        sa.Column("died_at_state", sa.String(20), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("evidence_tier", sa.String(32), nullable=True),
        sa.Column("confirmed_by", sa.String(32), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "opportunity_id"],
            ["opportunities.tenant_id", "opportunities.opportunity_id"],
            ondelete="RESTRICT",
            name="fk_loss_records_opportunity",
        ),
    )
    op.create_index("ix_loss_records_tenant_reason_state", "loss_records", ["tenant_id", "loss_reason", "died_at_state"])

    # ---- provenance_records（多态 entity，无 FK；只增；同字段多版本历史）----
    op.create_table(
        "provenance_records",
        sa.Column("provenance_id", sa.String(32), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(32), nullable=False),
        sa.Column("field_name", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(200), nullable=False),
        sa.Column("extracted_by", sa.String(64), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_by", sa.String(32), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_url", sa.String(2000), nullable=True),
        sa.Column("page_hash", sa.String(200), nullable=True),
    )
    op.create_index("ix_provenance_lookup", "provenance_records", ["tenant_id", "entity_type", "entity_id", "field_name", "extracted_at"])

    # ---- outbox_events（非只增；更新白名单由 guard 触发器强制）-------------
    op.create_table(
        "outbox_events",
        sa.Column("event_id", sa.String(32), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("event_payload", postgresql.JSONB(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trace_id", sa.String(32), nullable=False),
        sa.Column("run_id", sa.String(32), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt >= 1", name="ck_outbox_attempt_min"),
        sa.CheckConstraint("status IN ('pending', 'delivered')", name="ck_outbox_status"),
    )
    op.create_index("ix_outbox_tenant_status", "outbox_events", ["tenant_id", "status"])

    # ---- 只增触发器：三张审计表（BEFORE UPDATE OR DELETE 一律抛错）---------
    op.execute(
        """
CREATE FUNCTION score_snapshots_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'score_snapshots is append-only';
END;
$$ LANGUAGE plpgsql;
"""
    )
    op.execute(
        """
CREATE TRIGGER score_snapshots_append_only
BEFORE UPDATE OR DELETE ON score_snapshots
FOR EACH ROW EXECUTE FUNCTION score_snapshots_append_only();
"""
    )
    op.execute(
        """
CREATE FUNCTION loss_records_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'loss_records is append-only';
END;
$$ LANGUAGE plpgsql;
"""
    )
    op.execute(
        """
CREATE TRIGGER loss_records_append_only
BEFORE UPDATE OR DELETE ON loss_records
FOR EACH ROW EXECUTE FUNCTION loss_records_append_only();
"""
    )
    op.execute(
        """
CREATE FUNCTION provenance_records_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'provenance_records is append-only';
END;
$$ LANGUAGE plpgsql;
"""
    )
    op.execute(
        """
CREATE TRIGGER provenance_records_append_only
BEFORE UPDATE OR DELETE ON provenance_records
FOR EACH ROW EXECUTE FUNCTION provenance_records_append_only();
"""
    )

    # ---- outbox 更新白名单 guard（非只增；仅 status/delivered_at 可变）------
    op.execute(
        """
CREATE FUNCTION outbox_events_guard() RETURNS trigger AS $$
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
    op.execute(
        """
CREATE TRIGGER outbox_events_guard
BEFORE UPDATE ON outbox_events
FOR EACH ROW EXECUTE FUNCTION outbox_events_guard();
"""
    )


def downgrade() -> None:
    """逆序删除：索引 → 子表先于父表 → 触发器函数。"""
    op.drop_index("ix_outbox_tenant_status", table_name="outbox_events")
    op.drop_index("ix_provenance_lookup", table_name="provenance_records")
    op.drop_index("ix_loss_records_tenant_reason_state", table_name="loss_records")
    op.drop_index("ix_handoffs_tenant_state_requested", table_name="handoffs")
    op.drop_index("ix_score_snapshots_tenant_opp_scored", table_name="score_snapshots")
    op.drop_index("ix_opportunities_tenant_owner_state", table_name="opportunities")
    op.drop_index("ix_opportunities_tenant_state", table_name="opportunities")
    op.drop_table("outbox_events")
    op.drop_table("provenance_records")
    op.drop_table("loss_records")
    op.drop_table("handoffs")
    op.drop_table("score_snapshots")
    op.drop_table("opportunities")
    op.execute("DROP FUNCTION outbox_events_guard();")
    op.execute("DROP FUNCTION provenance_records_append_only();")
    op.execute("DROP FUNCTION loss_records_append_only();")
    op.execute("DROP FUNCTION score_snapshots_append_only();")
