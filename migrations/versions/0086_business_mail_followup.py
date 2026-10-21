"""普通来信处理与草稿，追加历史受RLS及不可变保护。"""

import sqlalchemy as sa
from alembic import op

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None
TABLES = {
    "business_mail_followups": """
CREATE TABLE business_mail_followups (
	tenant_id VARCHAR(40) NOT NULL,
	message_id VARCHAR(40) NOT NULL,
	revision INTEGER NOT NULL,
	status VARCHAR(30) NOT NULL,
	draft_id VARCHAR(40),
	PRIMARY KEY (tenant_id, message_id),
	FOREIGN KEY(tenant_id, message_id) REFERENCES business_mail_messages (tenant_id, message_id) ON DELETE RESTRICT,
	CONSTRAINT ck_business_followup CHECK (revision >= 0 AND status IN ('pending','in_progress','waiting_customer','completed'))
)

""",
    "business_mail_reply_drafts": """
CREATE TABLE business_mail_reply_drafts (
	tenant_id VARCHAR(40) NOT NULL,
	draft_id VARCHAR(40) NOT NULL,
	message_id VARCHAR(40) NOT NULL,
	subject TEXT NOT NULL,
	body_text TEXT NOT NULL,
	source_sha256 VARCHAR(64) NOT NULL,
	created_by VARCHAR(40) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (tenant_id, draft_id),
	FOREIGN KEY(tenant_id, message_id) REFERENCES business_mail_messages (tenant_id, message_id) ON DELETE RESTRICT
)

""",
    "business_mail_followup_events": """
CREATE TABLE business_mail_followup_events (
	tenant_id VARCHAR(40) NOT NULL,
	event_id VARCHAR(40) NOT NULL,
	message_id VARCHAR(40) NOT NULL,
	kind VARCHAR(30) NOT NULL,
	revision INTEGER NOT NULL,
	status VARCHAR(30) NOT NULL,
	note TEXT NOT NULL,
	actor_id VARCHAR(40) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	draft_id VARCHAR(40),
	request_key VARCHAR(200) NOT NULL,
	payload_sha256 VARCHAR(64) NOT NULL,
	PRIMARY KEY (tenant_id, event_id),
	FOREIGN KEY(tenant_id, message_id) REFERENCES business_mail_messages (tenant_id, message_id) ON DELETE RESTRICT,
	CONSTRAINT uq_business_followup_revision UNIQUE (tenant_id, message_id, revision),
	CONSTRAINT uq_business_followup_request UNIQUE (tenant_id, request_key)
)

""",
}


def upgrade() -> None:
    for name, ddl in TABLES.items():
        op.execute(sa.text(ddl))
        predicate = "current_user::text = 'tradeos_t_' || pg_catalog.substr(pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(tenant_id, 'UTF8')), 'hex'), 1, 48)"
        op.execute(f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
        for suffix, kind in (("guard", "RESTRICTIVE"), ("allow", "PERMISSIVE")):
            op.execute(
                f"CREATE POLICY tradeos_tenant_{suffix} ON {name} AS {kind} FOR ALL TO PUBLIC USING ({predicate}) WITH CHECK ({predicate})"
            )
        op.execute(f"""DO $$ DECLARE account record; BEGIN
FOR account IN SELECT rolname FROM pg_catalog.pg_roles WHERE rolname ~ '^tradeos_t_[0-9a-f]{{48}}$'
LOOP EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {name} TO %I', account.rolname); END LOOP; END $$""")
    for name in ("business_mail_reply_drafts", "business_mail_followup_events"):
        op.execute(
            f"CREATE TRIGGER business_mail_history_immutable BEFORE UPDATE OR DELETE ON {name} FOR EACH ROW EXECUTE FUNCTION business_mail_history_immutable()"
        )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    for name in reversed(TABLES):
        op.execute(f"LOCK TABLE {name} IN ACCESS EXCLUSIVE MODE")
        if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {name})")):
            raise RuntimeError("已有邮件处理历史，拒绝有损回退")
    for name in reversed(TABLES):
        op.drop_table(name)
