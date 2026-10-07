"""0010 Tool Gateway durable invocation ledger 与只增事件。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUSES = (
    "received",
    "claimed",
    "executing",
    "succeeded",
    "rejected",
    "duplicate",
    "failed_transient",
    "failed_permanent",
)
_ERROR_CATEGORIES = (
    "validation",
    "permission_denied",
    "suppressed",
    "approval_required",
    "idempotency_conflict",
    "in_progress",
    "rate_limited",
    "provider_auth_required",
    "provider_permanent",
    "provider_transient",
    "reconciliation_required",
    "unexpected",
)


def _sql_values(values: tuple[str, ...]) -> str:
    return ",".join(f"'{value}'" for value in values)


def upgrade() -> None:
    """创建 tenant-scoped invocation 与 append-only event 两表。"""
    op.create_table(
        "tool_calls",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("tool_call_id", sa.String(32), nullable=False),
        sa.Column("tool_id", sa.String(100), nullable=False),
        sa.Column("tool_version", sa.String(100), nullable=False),
        sa.Column("risk_level", sa.String(16), nullable=False),
        sa.Column("cost_class", sa.String(16), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=True),
        sa.Column("fingerprint_version", sa.String(100), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("duplicate_of", sa.String(32), nullable=True),
        sa.Column("lease_owner", sa.String(100), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(32), nullable=True),
        sa.Column("user_id", sa.String(32), nullable=False),
        sa.Column("campaign_id", sa.String(32), nullable=True),
        sa.Column("message_attempt_id", sa.String(32), nullable=True),
        sa.Column("provider_ref", sa.String(200), nullable=True),
        sa.Column("error_category", sa.String(32), nullable=True),
        sa.Column("retry_after_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "tool_call_id", name="pk_tool_calls"),
        sa.UniqueConstraint(
            "tenant_id", "tool_call_id", name="uq_tool_calls_tenant_call"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "duplicate_of"],
            ["tool_calls.tenant_id", "tool_calls.tool_call_id"],
            deferrable=True,
            initially="DEFERRED",
            name="fk_tool_calls_duplicate",
        ),
        sa.CheckConstraint(
            f"status IN ({_sql_values(_STATUSES)})", name="ck_tool_calls_status"
        ),
        sa.CheckConstraint(
            "risk_level IN ('low','medium','high')", name="ck_tool_calls_risk"
        ),
        sa.CheckConstraint(
            "cost_class IN ('free','low','medium','high')", name="ck_tool_calls_cost"
        ),
        sa.CheckConstraint(
            "tool_id ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$' AND "
            "tool_version ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$' AND "
            "user_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$' AND "
            "(idempotency_key IS NULL OR idempotency_key ~ "
            "'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$') AND "
            "(lease_owner IS NULL OR lease_owner ~ "
            "'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$') AND "
            "(run_id IS NULL OR run_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$') "
            "AND (campaign_id IS NULL OR campaign_id ~ "
            "'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$') AND "
            "(message_attempt_id IS NULL OR message_attempt_id ~ "
            "'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$') AND "
            "lower(tool_version) !~ "
            "'(^|[_.:-])(bearer|token|secret|password|authorization)([_.:-]|$)' "
            "AND (idempotency_key IS NULL OR lower(idempotency_key) !~ "
            "'(^|[_.:-])(bearer|token|secret|password|authorization)([_.:-]|$)') "
            "AND (lease_owner IS NULL OR lower(lease_owner) !~ "
            "'(^|[_.:-])(bearer|token|secret|password|authorization)([_.:-]|$)')",
            name="ck_tool_calls_safe_labels",
        ),
        sa.CheckConstraint(
            "error_category IS NULL OR error_category IN "
            f"({_sql_values(_ERROR_CATEGORIES)})",
            name="ck_tool_calls_error_category",
        ),
        sa.CheckConstraint(
            "(request_fingerprint IS NULL AND fingerprint_version IS NULL) OR "
            "(request_fingerprint ~ '^[0-9a-f]{64}$' AND "
            "fingerprint_version IS NOT NULL)",
            name="ck_tool_calls_fingerprint_pair",
        ),
        sa.CheckConstraint(
            "(status IN ('claimed','executing','succeeded','failed_transient',"
            "'failed_permanent') AND idempotency_key IS NOT NULL AND "
            "request_fingerprint IS NOT NULL AND fingerprint_version IS NOT NULL) OR "
            "(status IN ('received','rejected','duplicate') AND "
            "idempotency_key IS NULL)",
            name="ck_tool_calls_canonical_fields",
        ),
        sa.CheckConstraint(
            "(status='duplicate' AND duplicate_of IS NOT NULL) OR "
            "(status<>'duplicate' AND duplicate_of IS NULL)",
            name="ck_tool_calls_duplicate_fields",
        ),
        sa.CheckConstraint(
            "(status IN ('claimed','executing','failed_transient') AND "
            "lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status NOT IN ('claimed','executing','failed_transient') AND "
            "lease_owner IS NULL AND lease_expires_at IS NULL)",
            name="ck_tool_calls_lease_fields",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND "
            "((idempotency_key IS NOT NULL AND attempt_count >= 1) OR "
            "(idempotency_key IS NULL AND attempt_count = 0))",
            name="ck_tool_calls_attempt_count",
        ),
        sa.CheckConstraint(
            "(status IN ('received','claimed','executing') AND provider_ref IS NULL "
            "AND error_category IS NULL AND retry_after_at IS NULL AND "
            "completed_at IS NULL) OR "
            "(status='succeeded' AND provider_ref IS NOT NULL AND "
            "error_category IS NULL AND retry_after_at IS NULL AND "
            "completed_at IS NOT NULL) OR "
            "(status='rejected' AND provider_ref IS NULL AND "
            "error_category IS NOT NULL AND retry_after_at IS NULL AND "
            "completed_at IS NOT NULL) OR "
            "(status='duplicate' AND provider_ref IS NULL AND "
            "error_category IS NULL AND retry_after_at IS NULL AND "
            "completed_at IS NOT NULL) OR "
            "(status='failed_transient' AND provider_ref IS NULL AND "
            "error_category IS NOT NULL AND completed_at IS NULL) OR "
            "(status='failed_permanent' AND provider_ref IS NULL AND "
            "error_category IS NOT NULL AND retry_after_at IS NULL AND "
            "completed_at IS NOT NULL)",
            name="ck_tool_calls_result_fields",
        ),
        sa.CheckConstraint(
            "provider_ref IS NULL OR (char_length(provider_ref) BETWEEN 1 AND 200 "
            "AND provider_ref=btrim(provider_ref) "
            "AND provider_ref !~ '[[:space:]]' AND position('@' in provider_ref)=0 "
            "AND position('://' in provider_ref)=0 "
            "AND lower(provider_ref) !~ '(bearer|token|secret|password|authorization)')",
            name="ck_tool_calls_provider_ref",
        ),
    )
    op.create_index(
        "uq_tool_calls_tenant_tool_key",
        "tool_calls",
        ["tenant_id", "tool_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.create_index(
        "ix_tool_calls_tenant_status_retry",
        "tool_calls",
        ["tenant_id", "status", "retry_after_at", "updated_at"],
    )

    op.create_table(
        "tool_call_events",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("event_id", sa.String(32), nullable=False),
        sa.Column("tool_call_id", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(100), nullable=False),
        sa.Column("outcome", sa.String(100), nullable=False),
        sa.Column("rule", sa.String(100), nullable=True),
        sa.Column("category", sa.String(32), nullable=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(32), nullable=True),
        sa.Column("campaign_id", sa.String(32), nullable=True),
        sa.Column("message_attempt_id", sa.String(32), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("cost_note", sa.String(100), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id", "event_id", name="pk_tool_call_events"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "tool_call_id"],
            ["tool_calls.tenant_id", "tool_calls.tool_call_id"],
            deferrable=True,
            initially="DEFERRED",
            name="fk_tool_call_events_call",
        ),
        sa.CheckConstraint(
            "duration_ms >= 0", name="ck_tool_call_events_duration"
        ),
        sa.CheckConstraint(
            "category IS NULL OR category IN "
            f"({_sql_values(_ERROR_CATEGORIES)})",
            name="ck_tool_call_events_category",
        ),
        sa.CheckConstraint(
            "stage ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$' AND "
            "outcome ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$' AND "
            "actor_id ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$' AND "
            "(rule IS NULL OR rule ~ '^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$') "
            "AND (run_id IS NULL OR run_id ~ "
            "'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$') AND "
            "(campaign_id IS NULL OR campaign_id ~ "
            "'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$') AND "
            "(message_attempt_id IS NULL OR message_attempt_id ~ "
            "'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}$') AND "
            "(cost_note IS NULL OR cost_note ~ "
            "'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,99}$') AND "
            "lower(stage) !~ "
            "'(^|[_.:-])(bearer|token|secret|password|authorization)([_.:-]|$)' "
            "AND lower(outcome) !~ "
            "'(^|[_.:-])(bearer|token|secret|password|authorization)([_.:-]|$)' "
            "AND (rule IS NULL OR lower(rule) !~ "
            "'(^|[_.:-])(bearer|token|secret|password|authorization)([_.:-]|$)') "
            "AND (cost_note IS NULL OR lower(cost_note) !~ "
            "'(^|[_.:-])(bearer|token|secret|password|authorization)([_.:-]|$)')",
            name="ck_tool_call_events_safe_labels",
        ),
    )
    op.create_index(
        "ix_tool_call_events_tenant_call_occurred",
        "tool_call_events",
        ["tenant_id", "tool_call_id", "occurred_at", "event_id"],
    )
    op.execute(
        """
CREATE FUNCTION tool_call_events_append_only_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'tool call event is append-only';
END;
$$ LANGUAGE plpgsql;
"""
    )
    op.execute(
        """
CREATE TRIGGER trg_tool_call_events_append_only
BEFORE UPDATE OR DELETE ON tool_call_events
FOR EACH ROW EXECUTE FUNCTION tool_call_events_append_only_guard();
"""
    )


def downgrade() -> None:
    """按 trigger→function→child→parent 顺序恢复 0009。"""
    op.execute(
        "DROP TRIGGER trg_tool_call_events_append_only ON tool_call_events;"
    )
    op.execute("DROP FUNCTION tool_call_events_append_only_guard();")
    op.drop_index(
        "ix_tool_call_events_tenant_call_occurred", table_name="tool_call_events"
    )
    op.drop_table("tool_call_events")
    op.drop_index("ix_tool_calls_tenant_status_retry", table_name="tool_calls")
    op.drop_index("uq_tool_calls_tenant_tool_key", table_name="tool_calls")
    op.drop_table("tool_calls")
