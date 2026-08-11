"""0009 触达 Campaign、Enrollment、抑制、额度与发送审计八表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建触达域八表及不可变/单调数据库护栏。"""
    op.create_table(
        "outreach_campaigns",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("campaign_id", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("round_robin_cursor", sa.Integer(), nullable=False),
        sa.Column("approval_id", sa.String(32), nullable=True),
        sa.Column("approved_by", sa.String(32), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_reason", sa.String(200), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id", "campaign_id", name="pk_outreach_campaigns"
        ),
        sa.CheckConstraint(
            "state IN ('draft','pending_approval','active','paused','completed','cancelled')",
            name="ck_outreach_campaign_state",
        ),
        sa.CheckConstraint("current_version >= 1", name="ck_outreach_campaign_version"),
        sa.CheckConstraint(
            "round_robin_cursor >= -1", name="ck_outreach_campaign_cursor"
        ),
        sa.CheckConstraint(
            "(approval_id IS NULL AND approved_by IS NULL AND approved_at IS NULL) OR "
            "(approval_id IS NOT NULL AND approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_outreach_campaign_approval_tuple",
        ),
    )
    op.create_index(
        "ix_outreach_campaigns_tenant_state_created",
        "outreach_campaigns",
        ["tenant_id", "state", "created_at", "campaign_id"],
    )

    op.create_table(
        "outreach_campaign_versions",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("campaign_id", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("markets", postgresql.JSONB(), nullable=False),
        sa.Column("target_entity_types", postgresql.JSONB(), nullable=False),
        sa.Column("allowed_categories", postgresql.JSONB(), nullable=False),
        sa.Column("sender_identity_ids", postgresql.JSONB(), nullable=False),
        sa.Column("daily_new_contact_limit", sa.Integer(), nullable=False),
        sa.Column("daily_total_message_limit", sa.Integer(), nullable=False),
        sa.Column("handoff_triggers", postgresql.JSONB(), nullable=False),
        sa.Column("stop_on_reply", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "campaign_id",
            "version",
            name="pk_outreach_campaign_versions",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["outreach_campaigns.tenant_id", "outreach_campaigns.campaign_id"],
            ondelete="RESTRICT",
            name="fk_outreach_campaign_versions_campaign",
        ),
        sa.CheckConstraint("version >= 1", name="ck_outreach_version_number"),
        sa.CheckConstraint(
            "daily_new_contact_limit > 0 AND daily_total_message_limit > 0 "
            "AND daily_new_contact_limit <= daily_total_message_limit",
            name="ck_outreach_version_quotas",
        ),
        sa.CheckConstraint(
            "stop_on_reply IS TRUE", name="ck_outreach_version_stop_on_reply"
        ),
    )

    op.create_table(
        "outreach_sequence_steps",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("campaign_id", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column("intent", sa.String(32), nullable=False),
        sa.Column("wait_days", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "campaign_id",
            "version",
            "step_number",
            name="pk_outreach_sequence_steps",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id", "version"],
            [
                "outreach_campaign_versions.tenant_id",
                "outreach_campaign_versions.campaign_id",
                "outreach_campaign_versions.version",
            ],
            ondelete="RESTRICT",
            name="fk_outreach_steps_version",
        ),
        sa.CheckConstraint(
            "step_number BETWEEN 1 AND 5", name="ck_outreach_step_number"
        ),
        sa.CheckConstraint(
            "intent IN ('discovery','presentation','follow_up')",
            name="ck_outreach_step_intent",
        ),
        sa.CheckConstraint("wait_days >= 0", name="ck_outreach_step_wait"),
    )

    op.create_table(
        "outreach_enrollments",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("enrollment_id", sa.String(32), nullable=False),
        sa.Column("campaign_id", sa.String(32), nullable=False),
        sa.Column("campaign_version", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.String(32), nullable=False),
        sa.Column("contact_point_id", sa.String(32), nullable=False),
        sa.Column("sending_identity_id", sa.String(32), nullable=False),
        sa.Column("state", sa.String(40), nullable=False),
        sa.Column("current_step", sa.Integer(), nullable=False),
        sa.Column("next_send_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_reason", sa.String(32), nullable=True),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "enrollment_id", name="pk_outreach_enrollments"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_outreach_enrollments_tenant_key",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id", "campaign_version"],
            [
                "outreach_campaign_versions.tenant_id",
                "outreach_campaign_versions.campaign_id",
                "outreach_campaign_versions.version",
            ],
            ondelete="RESTRICT",
            name="fk_outreach_enrollments_version",
        ),
        sa.CheckConstraint(
            "state IN ('enrolled','in_sequence','replied','completed',"
            "'stopped_suppressed','stopped_bounced','stopped_manual',"
            "'stopped_identity_unavailable')",
            name="ck_outreach_enrollment_state",
        ),
        sa.CheckConstraint(
            "current_step BETWEEN 0 AND 5", name="ck_outreach_enrollment_step"
        ),
        sa.CheckConstraint(
            "(state IN ('enrolled','in_sequence') AND stopped_at IS NULL AND "
            "stop_reason IS NULL) OR "
            "(state='replied' AND stopped_at IS NOT NULL AND stop_reason='reply') OR "
            "(state='completed' AND stopped_at IS NOT NULL AND stop_reason IS NULL) OR "
            "(state='stopped_suppressed' AND stopped_at IS NOT NULL AND "
            "stop_reason='suppression') OR "
            "(state='stopped_bounced' AND stopped_at IS NOT NULL AND "
            "stop_reason='hard_bounce') OR "
            "(state='stopped_manual' AND stopped_at IS NOT NULL AND "
            "stop_reason='manual') OR "
            "(state='stopped_identity_unavailable' AND stopped_at IS NOT NULL AND "
            "stop_reason='identity_unavailable')",
            name="ck_outreach_enrollment_stop_fields",
        ),
    )
    op.create_index(
        "uq_outreach_enrollments_active_account",
        "outreach_enrollments",
        ["tenant_id", "account_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('enrolled','in_sequence')"),
    )
    op.create_index(
        "ix_outreach_enrollments_tenant_campaign_state",
        "outreach_enrollments",
        ["tenant_id", "campaign_id", "state", "enrolled_at", "enrollment_id"],
    )
    op.create_index(
        "ix_outreach_enrollments_tenant_contact_state",
        "outreach_enrollments",
        ["tenant_id", "contact_point_id", "state"],
    )

    op.create_table(
        "outreach_suppressions",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("suppression_id", sa.String(32), nullable=False),
        sa.Column("contact_point_id", sa.String(32), nullable=True),
        sa.Column("account_id", sa.String(32), nullable=True),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_ref", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "suppression_id", name="pk_outreach_suppressions"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_outreach_suppressions_tenant_key",
        ),
        sa.CheckConstraint(
            "(contact_point_id IS NOT NULL AND account_id IS NULL) OR "
            "(contact_point_id IS NULL AND account_id IS NOT NULL)",
            name="ck_outreach_suppression_exact_target",
        ),
        sa.CheckConstraint(
            "reason IN ('unsubscribe','complaint','hard_bounce','manual_block',"
            "'competitor','existing_customer_conflict')",
            name="ck_outreach_suppression_reason",
        ),
    )
    op.create_index(
        "ix_outreach_suppressions_tenant_contact",
        "outreach_suppressions",
        ["tenant_id", "contact_point_id", "occurred_at"],
        postgresql_where=sa.text("contact_point_id IS NOT NULL"),
    )
    op.create_index(
        "ix_outreach_suppressions_tenant_account",
        "outreach_suppressions",
        ["tenant_id", "account_id", "occurred_at"],
        postgresql_where=sa.text("account_id IS NOT NULL"),
    )

    op.create_table(
        "outreach_daily_quotas",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("campaign_id", sa.String(32), nullable=False),
        sa.Column("on_day", sa.Date(), nullable=False),
        sa.Column("new_contacts_reserved", sa.Integer(), nullable=False),
        sa.Column("messages_reserved", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "campaign_id",
            "on_day",
            name="pk_outreach_daily_quotas",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id"],
            ["outreach_campaigns.tenant_id", "outreach_campaigns.campaign_id"],
            ondelete="RESTRICT",
            name="fk_outreach_quotas_campaign",
        ),
        sa.CheckConstraint(
            "new_contacts_reserved >= 0 AND messages_reserved >= 0",
            name="ck_outreach_quota_nonnegative",
        ),
    )

    op.create_table(
        "outreach_message_attempts",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("attempt_id", sa.String(32), nullable=False),
        sa.Column("message_id", sa.String(32), nullable=False),
        sa.Column("campaign_id", sa.String(32), nullable=False),
        sa.Column("enrollment_id", sa.String(32), nullable=False),
        sa.Column("campaign_version", sa.Integer(), nullable=False),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column("sending_identity_id", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("provider_ref", sa.String(200), nullable=True),
        sa.Column("failure_category", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "attempt_id", name="pk_outreach_message_attempts"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_outreach_attempts_tenant_key",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "enrollment_id"],
            ["outreach_enrollments.tenant_id", "outreach_enrollments.enrollment_id"],
            ondelete="RESTRICT",
            name="fk_outreach_attempts_enrollment",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "campaign_id", "campaign_version"],
            [
                "outreach_campaign_versions.tenant_id",
                "outreach_campaign_versions.campaign_id",
                "outreach_campaign_versions.version",
            ],
            ondelete="RESTRICT",
            name="fk_outreach_attempts_version",
        ),
        sa.CheckConstraint(
            "step_number BETWEEN 1 AND 5", name="ck_outreach_attempt_step"
        ),
        sa.CheckConstraint(
            "(state='reserved' AND provider_ref IS NULL AND failure_category IS NULL) OR "
            "(state='sent' AND provider_ref IS NOT NULL AND failure_category IS NULL) OR "
            "(state='failed_transient' AND provider_ref IS NULL AND "
            "failure_category='provider_transient') OR "
            "(state='failed_permanent' AND provider_ref IS NULL AND "
            "failure_category='identity_unavailable')",
            name="ck_outreach_attempt_state_fields",
        ),
    )
    op.create_index(
        "ix_outreach_attempts_tenant_enrollment_created",
        "outreach_message_attempts",
        ["tenant_id", "enrollment_id", "created_at", "attempt_id"],
    )

    op.create_table(
        "outreach_actions",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("action_id", sa.String(32), nullable=False),
        sa.Column("action_key", sa.String(200), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "action_id", name="pk_outreach_actions"),
        sa.UniqueConstraint(
            "tenant_id", "action_key", name="uq_outreach_actions_tenant_key"
        ),
    )

    op.execute(
        """
CREATE FUNCTION outreach_append_only_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'outreach audit row is append-only';
END;
$$ LANGUAGE plpgsql;
"""
    )
    for table in (
        "outreach_campaign_versions",
        "outreach_sequence_steps",
        "outreach_suppressions",
        "outreach_actions",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION outreach_append_only_guard();"
        )
    op.execute(
        """
CREATE FUNCTION outreach_daily_quota_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'outreach daily quota is not deletable';
    END IF;
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.campaign_id IS DISTINCT FROM OLD.campaign_id
       OR NEW.on_day IS DISTINCT FROM OLD.on_day
       OR NEW.new_contacts_reserved < OLD.new_contacts_reserved
       OR NEW.messages_reserved < OLD.messages_reserved
    THEN
        RAISE EXCEPTION 'outreach quota keys are immutable and counters are monotonic';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""
    )
    op.execute(
        """
CREATE TRIGGER trg_outreach_daily_quotas_guard
BEFORE UPDATE OR DELETE ON outreach_daily_quotas
FOR EACH ROW EXECUTE FUNCTION outreach_daily_quota_guard();
"""
    )


def downgrade() -> None:
    """按 trigger→function→child→parent 顺序恢复 0008。"""
    op.execute("DROP TRIGGER trg_outreach_daily_quotas_guard ON outreach_daily_quotas;")
    for table in (
        "outreach_actions",
        "outreach_suppressions",
        "outreach_sequence_steps",
        "outreach_campaign_versions",
    ):
        op.execute(f"DROP TRIGGER trg_{table}_append_only ON {table};")
    op.execute("DROP FUNCTION outreach_daily_quota_guard();")
    op.execute("DROP FUNCTION outreach_append_only_guard();")
    op.drop_table("outreach_actions")
    op.drop_index(
        "ix_outreach_attempts_tenant_enrollment_created",
        table_name="outreach_message_attempts",
    )
    op.drop_table("outreach_message_attempts")
    op.drop_table("outreach_daily_quotas")
    op.drop_index(
        "ix_outreach_suppressions_tenant_account",
        table_name="outreach_suppressions",
    )
    op.drop_index(
        "ix_outreach_suppressions_tenant_contact",
        table_name="outreach_suppressions",
    )
    op.drop_table("outreach_suppressions")
    op.drop_index(
        "ix_outreach_enrollments_tenant_contact_state",
        table_name="outreach_enrollments",
    )
    op.drop_index(
        "ix_outreach_enrollments_tenant_campaign_state",
        table_name="outreach_enrollments",
    )
    op.drop_index(
        "uq_outreach_enrollments_active_account",
        table_name="outreach_enrollments",
    )
    op.drop_table("outreach_enrollments")
    op.drop_table("outreach_sequence_steps")
    op.drop_table("outreach_campaign_versions")
    op.drop_index(
        "ix_outreach_campaigns_tenant_state_created",
        table_name="outreach_campaigns",
    )
    op.drop_table("outreach_campaigns")
