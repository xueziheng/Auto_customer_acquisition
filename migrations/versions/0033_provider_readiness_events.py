"""Provider readiness tenant-scoped append-only 事件流。

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_readiness_events",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("provider_readiness_event_id", sa.String(30), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column(
            "capability_set",
            postgresql.ARRAY(sa.String(64)),
            nullable=False,
        ),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("configuration_version", sa.String(32), nullable=False),
        sa.Column("configuration_hash", sa.CHAR(64), nullable=False),
        sa.Column("connector_profile_version", sa.String(32), nullable=False),
        sa.Column("transport_profile", sa.String(64), nullable=False),
        sa.Column("api_key_version", sa.String(32), nullable=False),
        sa.Column("validation_key", sa.String(200), nullable=True),
        sa.Column("outcome_code", sa.String(64), nullable=True),
        sa.Column("evidence_ref", sa.String(200), nullable=True),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "provider_readiness_event_id",
            name="pk_provider_readiness_events",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "capability_set",
            "sequence",
            name="uq_provider_readiness_events_stream_sequence",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "provider",
            "capability_set",
            "idempotency_key",
            name="uq_provider_readiness_events_stream_idempotency",
        ),
        sa.CheckConstraint(
            "sequence > 0",
            name="ck_provider_readiness_events_sequence",
        ),
        sa.CheckConstraint(
            "event_type IN ("
            "'configured','validation_started','validation_passed',"
            "'validation_failed','runtime_composed')",
            name="ck_provider_readiness_events_type",
        ),
        sa.CheckConstraint(
            "provider ~ '^[a-z][a-z0-9._-]{0,31}$' AND "
            "configuration_version ~ '^[a-z0-9][a-z0-9._-]{0,31}$' AND "
            "connector_profile_version ~ '^[a-z0-9][a-z0-9._-]{0,31}$' AND "
            "transport_profile ~ '^[a-z0-9][a-z0-9._-]{0,63}$' AND "
            "api_key_version ~ '^[a-z0-9][a-z0-9._-]{0,31}$'",
            name="ck_provider_readiness_events_lowercase_labels",
        ),
        sa.CheckConstraint(
            "configuration_hash ~ '^[0-9a-f]{64}$'",
            name="ck_provider_readiness_events_hash",
        ),
        sa.CheckConstraint(
            "cardinality(capability_set) = 2 AND "
            "capability_set = "
            "ARRAY['contact.enrich','contact.verify']::varchar(64)[]",
            name="ck_provider_readiness_events_capabilities",
        ),
        sa.CheckConstraint(
            "outcome_code IS NULL OR outcome_code IN ("
            "'auth_required','rate_limited','provider_transient',"
            "'provider_permanent','response_invalid',"
            "'reconciliation_required')",
            name="ck_provider_readiness_events_outcome",
        ),
        sa.CheckConstraint(
            "(event_type = 'configured' AND validation_key IS NULL AND "
            "outcome_code IS NULL AND evidence_ref IS NULL) OR "
            "(event_type = 'validation_started' AND validation_key IS NOT NULL "
            "AND outcome_code IS NULL AND evidence_ref IS NULL) OR "
            "(event_type = 'validation_passed' AND validation_key IS NOT NULL "
            "AND outcome_code IS NULL AND evidence_ref IS NOT NULL) OR "
            "(event_type = 'validation_failed' AND validation_key IS NOT NULL "
            "AND outcome_code IS NOT NULL AND evidence_ref IS NULL) OR "
            "(event_type = 'runtime_composed' AND validation_key IS NULL AND "
            "outcome_code IS NULL AND evidence_ref IS NULL)",
            name="ck_provider_readiness_events_event_fields",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND "
            "provider_readiness_event_id ~ '^pre_[0-7][0-9A-HJKMNP-TV-Z]{25}$' "
            "AND btrim(actor_id) <> '' AND btrim(idempotency_key) <> '' AND "
            "(validation_key IS NULL OR btrim(validation_key) <> '') AND "
            "(evidence_ref IS NULL OR btrim(evidence_ref) <> '')",
            name="ck_provider_readiness_events_core",
        ),
    )

    op.execute(
        "CREATE FUNCTION guard_provider_readiness_append_only() RETURNS trigger "
        "AS $$ BEGIN RAISE EXCEPTION 'provider readiness events are append-only' "
        "USING ERRCODE='23514'; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_provider_readiness_events_append_only "
        "BEFORE UPDATE OR DELETE ON provider_readiness_events FOR EACH ROW "
        "EXECUTE FUNCTION guard_provider_readiness_append_only()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_provider_readiness_events_append_only "
        "ON provider_readiness_events"
    )
    op.execute("DROP FUNCTION guard_provider_readiness_append_only()")
    op.drop_table("provider_readiness_events")
