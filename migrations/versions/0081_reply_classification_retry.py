"""一次人工分类恢复；固定租户角色与不可变授权来源。"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None
TABLE = "reply_classification_retries"

def upgrade() -> None:
    op.create_table(TABLE,
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("message_id", sa.String(40), primary_key=True),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("original_invocation_id", sa.String(40), nullable=False),
        sa.Column("original_step_id", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("binding", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("model_started_at", sa.DateTime(timezone=True)),
        *[sa.Column(n, sa.Integer(), nullable=False) for n in ("classifications", "provider_requests", "input_bytes", "output_tokens")],
        sa.Column("provider_pending", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_reply_retry_key"),
        sa.UniqueConstraint("tenant_id", "run_id", name="uq_reply_retry_run"),
        sa.ForeignKeyConstraint(["tenant_id", "run_id"], ["workflow_runs.tenant_id", "workflow_runs.run_id"]),
        sa.ForeignKeyConstraint(["tenant_id", "original_invocation_id"], ["model_invocations.tenant_id", "model_invocations.invocation_id"]),
        sa.CheckConstraint("state IN ('queued','running','classified','failed')", name="ck_reply_retry_state"),
        sa.CheckConstraint("classifications BETWEEN 0 AND 1 AND provider_requests BETWEEN 0 AND 4 AND input_bytes BETWEEN 0 AND 32768 AND output_tokens BETWEEN 0 AND 512", name="ck_reply_retry_budget"))
    predicate = "current_user::text = 'tradeos_t_' || pg_catalog.substr(pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(tenant_id, 'UTF8')), 'hex'), 1, 48)"
    op.execute(f"ALTER TABLE {TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY tradeos_tenant_guard ON {TABLE} AS RESTRICTIVE FOR ALL TO PUBLIC USING ({predicate}) WITH CHECK ({predicate})")
    op.execute(f"CREATE POLICY tradeos_tenant_allow ON {TABLE} AS PERMISSIVE FOR ALL TO PUBLIC USING ({predicate}) WITH CHECK ({predicate})")
    op.execute("""CREATE FUNCTION reply_retry_monotonic() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'immutable reply retry'; END IF;
 IF (NEW.tenant_id,NEW.message_id,NEW.run_id,NEW.original_invocation_id,NEW.original_step_id,NEW.idempotency_key,NEW.binding,NEW.created_at,NEW.expires_at)
 IS DISTINCT FROM (OLD.tenant_id,OLD.message_id,OLD.run_id,OLD.original_invocation_id,OLD.original_step_id,OLD.idempotency_key,OLD.binding,OLD.created_at,OLD.expires_at)
 OR (OLD.model_started_at IS NOT NULL AND NEW.model_started_at IS DISTINCT FROM OLD.model_started_at)
 OR (OLD.state IN ('classified','failed') AND NEW.state IS DISTINCT FROM OLD.state)
 OR NEW.classifications < OLD.classifications OR NEW.provider_requests < OLD.provider_requests
 OR NEW.input_bytes < OLD.input_bytes OR NEW.output_tokens < OLD.output_tokens
 THEN RAISE EXCEPTION 'reply retry reset forbidden'; END IF;
 RETURN NEW;
END $$""")
    op.execute(f"CREATE TRIGGER reply_retry_monotonic BEFORE UPDATE OR DELETE ON {TABLE} FOR EACH ROW EXECUTE FUNCTION reply_retry_monotonic()")

def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute(f"LOCK TABLE {TABLE} IN ACCESS EXCLUSIVE MODE")
    if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {TABLE})")):
        raise RuntimeError("恢复账本非空，拒绝丢失证据的回退")
    op.drop_table(TABLE)
    op.execute("DROP FUNCTION reply_retry_monotonic()")
