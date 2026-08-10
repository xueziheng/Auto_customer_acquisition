"""0008 发件身份七表、租户复合外键与不可变审计护栏。"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建域、身份、认证、信誉、计数、预留与动作七表。"""
    op.create_table(
        "sending_domains",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("domain", sa.String(253), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "domain", name="pk_sending_domains"),
    )
    op.create_table(
        "sending_identities",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("identity_id", sa.String(32), nullable=False),
        sa.Column("domain", sa.String(253), nullable=False),
        sa.Column("address", sa.String(320), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("connector_ref", sa.String(64), nullable=True),
        sa.Column("warmup_started_on", sa.Date(), nullable=True),
        sa.Column("target_daily_volume", sa.Integer(), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sendable_state_before_restriction", sa.String(32), nullable=True),
        sa.Column("suspension_category", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("throttle_hard_bounce_rate", sa.Numeric(9, 6), nullable=False),
        sa.Column("suspend_hard_bounce_rate", sa.Numeric(9, 6), nullable=False),
        sa.Column("throttle_complaint_rate", sa.Numeric(9, 6), nullable=False),
        sa.Column("suspend_complaint_rate", sa.Numeric(9, 6), nullable=False),
        sa.Column("suspend_on_spam_trap", sa.Boolean(), nullable=False),
        sa.Column("suspend_on_blocklist", sa.Boolean(), nullable=False),
        sa.Column("minimum_sample", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "identity_id", name="pk_sending_identities"),
        sa.UniqueConstraint(
            "tenant_id", "address", name="uq_sending_identities_tenant_address"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "domain"],
            ["sending_domains.tenant_id", "sending_domains.domain"],
            ondelete="RESTRICT",
            name="fk_sending_identities_domain",
        ),
        sa.CheckConstraint(
            "target_daily_volume IS NULL OR target_daily_volume BETWEEN 5 AND 100",
            name="ck_sending_identity_target_volume",
        ),
        sa.CheckConstraint("version >= 0", name="ck_sending_identity_version"),
        sa.CheckConstraint(
            "minimum_sample >= 0", name="ck_sending_identity_minimum_sample"
        ),
        sa.CheckConstraint(
            "(warmup_started_on IS NULL) = (target_daily_volume IS NULL)",
            name="ck_sending_identity_warmup_pair",
        ),
        sa.CheckConstraint(
            "split_part(address, '@', 2) = domain",
            name="ck_sending_identity_address_domain",
        ),
        sa.CheckConstraint(
            "(state IN ('throttled', 'suspended') AND "
            "sendable_state_before_restriction IN ('warming', 'active')) OR "
            "(state NOT IN ('throttled', 'suspended') AND "
            "sendable_state_before_restriction IS NULL)",
            name="ck_sending_identity_restriction_state",
        ),
    )
    op.create_table(
        "sending_auth_checks",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("auth_check_id", sa.String(32), nullable=False),
        sa.Column("identity_id", sa.String(32), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("spf_passed", sa.Boolean(), nullable=False),
        sa.Column("dkim_passed", sa.Boolean(), nullable=False),
        sa.Column("dmarc_passed", sa.Boolean(), nullable=False),
        sa.Column("failures", postgresql.JSONB(), nullable=False),
        sa.Column("check_ref", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "auth_check_id", name="pk_sending_auth_checks"),
        sa.UniqueConstraint(
            "tenant_id",
            "identity_id",
            "check_ref",
            name="uq_sending_auth_tenant_identity_ref",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "identity_id"],
            ["sending_identities.tenant_id", "sending_identities.identity_id"],
            ondelete="RESTRICT",
            name="fk_sending_auth_identity",
        ),
    )
    op.create_table(
        "sending_reputation_events",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("reputation_event_id", sa.String(32), nullable=False),
        sa.Column("identity_id", sa.String(32), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dedup_key", sa.String(200), nullable=False),
        sa.Column("source_ref", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "reputation_event_id", name="pk_sending_reputation_events"
        ),
        sa.UniqueConstraint(
            "tenant_id", "dedup_key", name="uq_sending_reputation_tenant_dedup"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "identity_id"],
            ["sending_identities.tenant_id", "sending_identities.identity_id"],
            ondelete="RESTRICT",
            name="fk_sending_reputation_identity",
        ),
    )
    op.create_index(
        "ix_sending_reputation_tenant_identity_occurred",
        "sending_reputation_events",
        ["tenant_id", "identity_id", "occurred_at"],
    )
    op.create_table(
        "sending_daily_counters",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("identity_id", sa.String(32), nullable=False),
        sa.Column("on_day", sa.Date(), nullable=False),
        sa.Column("sent_attempts", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "identity_id", "on_day", name="pk_sending_daily_counters"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "identity_id"],
            ["sending_identities.tenant_id", "sending_identities.identity_id"],
            ondelete="RESTRICT",
            name="fk_sending_counter_identity",
        ),
        sa.CheckConstraint(
            "sent_attempts >= 0", name="ck_sending_counter_nonnegative"
        ),
    )
    op.create_table(
        "sending_send_reservations",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("reservation_id", sa.String(32), nullable=False),
        sa.Column("identity_id", sa.String(32), nullable=False),
        sa.Column("reservation_key", sa.String(200), nullable=False),
        sa.Column("on_day", sa.Date(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "reservation_id", name="pk_sending_send_reservations"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "identity_id",
            "reservation_key",
            name="uq_sending_reservation_tenant_identity_key",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "identity_id",
            "on_day",
            "sequence",
            name="uq_sending_reservation_tenant_identity_day_sequence",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "identity_id"],
            ["sending_identities.tenant_id", "sending_identities.identity_id"],
            ondelete="RESTRICT",
            name="fk_sending_reservation_identity",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_sending_reservation_sequence"),
    )
    op.create_table(
        "sending_identity_actions",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("action_id", sa.String(32), nullable=False),
        sa.Column("identity_id", sa.String(32), nullable=False),
        sa.Column("action_key", sa.String(200), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("before_state", sa.String(32), nullable=True),
        sa.Column("after_state", sa.String(32), nullable=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("rule", sa.String(128), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "action_id", name="pk_sending_identity_actions"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "identity_id",
            "action_key",
            name="uq_sending_action_tenant_identity_key",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "identity_id"],
            ["sending_identities.tenant_id", "sending_identities.identity_id"],
            ondelete="RESTRICT",
            name="fk_sending_action_identity",
        ),
    )

    op.execute(
        """
CREATE FUNCTION sending_append_only_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'sending audit row is append-only';
END;
$$ LANGUAGE plpgsql;
"""
    )
    for table in (
        "sending_auth_checks",
        "sending_reputation_events",
        "sending_send_reservations",
        "sending_identity_actions",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION sending_append_only_guard();"
        )
    op.execute(
        """
CREATE FUNCTION sending_domains_immutable_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.domain IS DISTINCT FROM OLD.domain
       OR NEW.role IS DISTINCT FROM OLD.role
    THEN
        RAISE EXCEPTION 'sending domain identity and role are immutable';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""
    )
    op.execute(
        """
CREATE TRIGGER trg_sending_domains_immutable
BEFORE UPDATE ON sending_domains
FOR EACH ROW EXECUTE FUNCTION sending_domains_immutable_guard();
"""
    )
    op.execute(
        """
CREATE FUNCTION sending_daily_counters_guard() RETURNS trigger AS $$
BEGIN
    IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
       OR NEW.identity_id IS DISTINCT FROM OLD.identity_id
       OR NEW.on_day IS DISTINCT FROM OLD.on_day
       OR NEW.sent_attempts < OLD.sent_attempts
    THEN
        RAISE EXCEPTION 'sending daily counter keys are immutable and count is monotonic';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""
    )
    op.execute(
        """
CREATE TRIGGER trg_sending_daily_counters_guard
BEFORE UPDATE ON sending_daily_counters
FOR EACH ROW EXECUTE FUNCTION sending_daily_counters_guard();
"""
    )


def downgrade() -> None:
    """按 trigger→function→child table→parent table 恢复 0007。"""
    op.execute("DROP TRIGGER trg_sending_daily_counters_guard ON sending_daily_counters;")
    op.execute("DROP TRIGGER trg_sending_domains_immutable ON sending_domains;")
    for table in (
        "sending_identity_actions",
        "sending_send_reservations",
        "sending_reputation_events",
        "sending_auth_checks",
    ):
        op.execute(f"DROP TRIGGER trg_{table}_append_only ON {table};")
    op.execute("DROP FUNCTION sending_daily_counters_guard();")
    op.execute("DROP FUNCTION sending_domains_immutable_guard();")
    op.execute("DROP FUNCTION sending_append_only_guard();")
    op.drop_table("sending_identity_actions")
    op.drop_table("sending_send_reservations")
    op.drop_table("sending_daily_counters")
    op.drop_index(
        "ix_sending_reputation_tenant_identity_occurred",
        table_name="sending_reputation_events",
    )
    op.drop_table("sending_reputation_events")
    op.drop_table("sending_auth_checks")
    op.drop_table("sending_identities")
    op.drop_table("sending_domains")
