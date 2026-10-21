"""受控 QQ 验收账本；无历史回填，非空拒绝回退。"""

import sqlalchemy as sa
from alembic import op

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE qq_sent_aliases (
	tenant_id VARCHAR(40) NOT NULL,
	evidence_id VARCHAR(40) NOT NULL,
	identity_id VARCHAR(40) NOT NULL,
	attempt_id VARCHAR(40) NOT NULL,
	alias_digest VARCHAR(64) NOT NULL,
	provider_header VARCHAR(998) NOT NULL,
	canonical_header VARCHAR(998) NOT NULL,
	artifact_id VARCHAR(40) NOT NULL,
	artifact_hash VARCHAR(64) NOT NULL,
	tool_call_id VARCHAR(40) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (tenant_id, evidence_id),
	CONSTRAINT uq_qq_sent_alias UNIQUE (tenant_id, identity_id, alias_digest),
	CONSTRAINT uq_qq_alias_attempt UNIQUE (tenant_id, attempt_id),
	FOREIGN KEY(tenant_id, attempt_id) REFERENCES outreach_message_attempts (tenant_id, attempt_id),
	FOREIGN KEY(tenant_id, tool_call_id) REFERENCES tool_calls (tenant_id, tool_call_id),
	FOREIGN KEY(tenant_id, artifact_id) REFERENCES raw_artifacts (tenant_id, artifact_id)
)""")
    op.execute("""ALTER TABLE qq_sent_aliases ENABLE ROW LEVEL SECURITY""")
    op.execute("""ALTER TABLE qq_sent_aliases FORCE ROW LEVEL SECURITY""")
    op.execute(
        """CREATE POLICY qq_sent_aliases_tenant ON qq_sent_aliases USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"""
    )
    op.execute("""CREATE TABLE qq_reply_trials (
	tenant_id VARCHAR(40) NOT NULL,
	trial_id VARCHAR(40) NOT NULL,
	mailbox_alias VARCHAR(32) NOT NULL,
	attempt_id VARCHAR(40) NOT NULL,
	alias_evidence_id VARCHAR(40) NOT NULL,
	scope JSONB NOT NULL,
	state VARCHAR(20) NOT NULL,
	scan_token VARCHAR(40),
	next_scan_at TIMESTAMP WITH TIME ZONE,
	watermark JSONB,
	message_id VARCHAR(40),
	model_started_at TIMESTAMP WITH TIME ZONE,
	reason VARCHAR(40),
	sessions INTEGER NOT NULL,
	searches INTEGER NOT NULL,
	headers INTEGER NOT NULL,
	bodies INTEGER NOT NULL,
	classifications INTEGER NOT NULL,
	provider_requests INTEGER NOT NULL,
	input_bytes INTEGER NOT NULL,
	output_tokens INTEGER NOT NULL,
	provider_pending BOOLEAN NOT NULL,
	PRIMARY KEY (tenant_id, trial_id),
	CONSTRAINT uq_qq_trial_mailbox UNIQUE (tenant_id, mailbox_alias),
	CONSTRAINT uq_qq_trial_attempt UNIQUE (tenant_id, attempt_id),
	FOREIGN KEY(tenant_id, alias_evidence_id) REFERENCES qq_sent_aliases (tenant_id, evidence_id),
	CONSTRAINT ck_qq_trial_state CHECK (state IN ('active','received','stopped','complete')),
	CONSTRAINT ck_qq_trial_budget CHECK (sessions BETWEEN 0 AND 20 AND searches BETWEEN 0 AND 20 AND headers BETWEEN 0 AND 20 AND bodies BETWEEN 0 AND 1 AND classifications BETWEEN 0 AND 1 AND provider_requests BETWEEN 0 AND 4 AND input_bytes BETWEEN 0 AND 32768 AND output_tokens BETWEEN 0 AND 512)
)""")
    op.execute("""ALTER TABLE qq_reply_trials ENABLE ROW LEVEL SECURITY""")
    op.execute("""ALTER TABLE qq_reply_trials FORCE ROW LEVEL SECURITY""")
    op.execute(
        """CREATE POLICY qq_reply_trials_tenant ON qq_reply_trials USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"""
    )
    op.execute("""CREATE TABLE qq_reply_trial_events (
	tenant_id VARCHAR(40) NOT NULL,
	event_id VARCHAR(40) NOT NULL,
	trial_id VARCHAR(40) NOT NULL,
	kind VARCHAR(40) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	input_bytes INTEGER NOT NULL,
	PRIMARY KEY (tenant_id, event_id),
	FOREIGN KEY(tenant_id, trial_id) REFERENCES qq_reply_trials (tenant_id, trial_id)
)""")
    op.execute("""ALTER TABLE qq_reply_trial_events ENABLE ROW LEVEL SECURITY""")
    op.execute("""ALTER TABLE qq_reply_trial_events FORCE ROW LEVEL SECURITY""")
    op.execute(
        """CREATE POLICY qq_reply_trial_events_tenant ON qq_reply_trial_events USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true))"""
    )
    op.execute(
        """CREATE FUNCTION qq_trial_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'immutable QQ evidence'; END $$"""
    )
    op.execute(
        """CREATE TRIGGER qq_sent_aliases_immutable BEFORE UPDATE OR DELETE ON qq_sent_aliases FOR EACH ROW EXECUTE FUNCTION qq_trial_immutable()"""
    )
    op.execute(
        """CREATE TRIGGER qq_reply_trial_events_immutable BEFORE UPDATE OR DELETE ON qq_reply_trial_events FOR EACH ROW EXECUTE FUNCTION qq_trial_immutable()"""
    )
    op.execute("""CREATE FUNCTION qq_trial_monotonic() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'immutable QQ trial'; END IF;
  IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id OR NEW.trial_id IS DISTINCT FROM OLD.trial_id
     OR NEW.scope IS DISTINCT FROM OLD.scope OR NEW.mailbox_alias IS DISTINCT FROM OLD.mailbox_alias
     OR NEW.attempt_id IS DISTINCT FROM OLD.attempt_id OR NEW.alias_evidence_id IS DISTINCT FROM OLD.alias_evidence_id
     OR (OLD.watermark IS NOT NULL AND NEW.watermark IS DISTINCT FROM OLD.watermark)
     OR (OLD.message_id IS NOT NULL AND NEW.message_id IS DISTINCT FROM OLD.message_id)
     OR (OLD.model_started_at IS NOT NULL AND NEW.model_started_at IS DISTINCT FROM OLD.model_started_at)
     OR (OLD.state IN ('stopped','complete') AND NEW.state IS DISTINCT FROM OLD.state)
     OR (OLD.state = 'received' AND NEW.state = 'active')
     OR NEW.sessions < OLD.sessions OR NEW.searches < OLD.searches OR NEW.headers < OLD.headers
     OR NEW.bodies < OLD.bodies OR NEW.classifications < OLD.classifications
     OR NEW.provider_requests < OLD.provider_requests OR NEW.input_bytes < OLD.input_bytes
     OR NEW.output_tokens < OLD.output_tokens
  THEN RAISE EXCEPTION 'QQ trial reset forbidden'; END IF;
  RETURN NEW;
END $$""")
    op.execute("""CREATE TRIGGER qq_reply_trials_monotonic BEFORE UPDATE OR DELETE ON qq_reply_trials
FOR EACH ROW EXECUTE FUNCTION qq_trial_monotonic()""")


def downgrade() -> None:
    # row_security=off 不绕过 RLS；无 BYPASSRLS 的角色直接报错，不能把过滤视图当成空表。
    op.execute("SET LOCAL row_security = off")
    op.execute(
        "LOCK TABLE qq_sent_aliases, qq_reply_trials, qq_reply_trial_events IN ACCESS EXCLUSIVE MODE"
    )
    for table in ("qq_sent_aliases", "qq_reply_trials", "qq_reply_trial_events"):
        if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table})")):
            raise RuntimeError("QQ 验收账本非空，拒绝丢失证据的回退")
    op.drop_table("qq_reply_trial_events")
    op.drop_table("qq_reply_trials")
    op.drop_table("qq_sent_aliases")
    op.execute("DROP FUNCTION qq_trial_immutable()")
    op.execute("DROP FUNCTION qq_trial_monotonic()")
