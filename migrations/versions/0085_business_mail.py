"""普通来信先持久可见，分类独立追加；全表企业RLS。"""

import sqlalchemy as sa
from alembic import op

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None

TABLES = {
    "business_mailboxes": (
        """CREATE TABLE business_mailboxes (
	tenant_id VARCHAR(40) NOT NULL, 
	mailbox_id VARCHAR(40) NOT NULL, 
	owner_id VARCHAR(40) NOT NULL, 
	user_id VARCHAR(128) NOT NULL, 
	address VARCHAR(320) NOT NULL, 
	binding_operation_id VARCHAR(40) NOT NULL, 
	enabled BOOLEAN NOT NULL, 
	uidvalidity BIGINT, 
	after_uid BIGINT NOT NULL, 
	revision INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	model_blocked_until TIMESTAMP WITH TIME ZONE, 
	last_synced_at TIMESTAMP WITH TIME ZONE, 
	failure_code VARCHAR(40), 
	PRIMARY KEY (tenant_id, mailbox_id), 
	CONSTRAINT uq_business_mailbox_address UNIQUE (tenant_id, address), 
	FOREIGN KEY(tenant_id, owner_id) REFERENCES employees (tenant_id, employee_id) ON DELETE RESTRICT
)""",
    ),
    "business_mail_messages": (
        """CREATE TABLE business_mail_messages (
	tenant_id VARCHAR(40) NOT NULL, 
	message_id VARCHAR(40) NOT NULL, 
	mailbox_id VARCHAR(40) NOT NULL, 
	sender TEXT NOT NULL, 
	subject TEXT NOT NULL, 
	body_text TEXT NOT NULL, 
	message_header TEXT, 
	received_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	ingested_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	content_sha256 VARCHAR(64) NOT NULL, 
	mime_sha256 VARCHAR(64), 
	source_sha256 VARCHAR(64) NOT NULL, 
	source_kind VARCHAR(20) NOT NULL, 
	artifact_id VARCHAR(40) NOT NULL, 
	uidvalidity BIGINT NOT NULL, 
	uid BIGINT NOT NULL, 
	category VARCHAR(20) NOT NULL, 
	classification_status VARCHAR(20) NOT NULL, 
	revision INTEGER NOT NULL, 
	failure_code VARCHAR(40), 
	run_id VARCHAR(40) NOT NULL, 
	model VARCHAR(128), 
	configuration_version VARCHAR(128), 
	claimed_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (tenant_id, message_id), 
	FOREIGN KEY(tenant_id, mailbox_id) REFERENCES business_mailboxes (tenant_id, mailbox_id) ON DELETE RESTRICT, 
	CONSTRAINT uq_business_mail_content UNIQUE (tenant_id, mailbox_id, content_sha256), 
	CONSTRAINT uq_business_mail_message_mailbox UNIQUE (tenant_id, message_id, mailbox_id), 
	FOREIGN KEY(tenant_id, artifact_id) REFERENCES raw_artifacts (tenant_id, artifact_id) ON DELETE RESTRICT, 
	CONSTRAINT ck_business_mail_source CHECK ((source_kind='full_mime' AND mime_sha256=source_sha256) OR (source_kind='headers_only' AND mime_sha256 IS NULL)), 
	CONSTRAINT ck_business_mail_bounds CHECK (uidvalidity BETWEEN 1 AND 4294967295 AND uid BETWEEN 1 AND 4294967295 AND revision >= 0), 
	CONSTRAINT uq_business_mail_run UNIQUE (tenant_id, run_id), 
	CONSTRAINT ck_business_mail_category CHECK (category IN ('pending','business','other','review')), 
	CONSTRAINT ck_business_mail_status CHECK (classification_status IN ('pending','classifying','classified','failed'))
)""",
        """CREATE INDEX ix_business_mail_inbox ON business_mail_messages (tenant_id, mailbox_id, received_at)""",
    ),
    "business_mail_receipts": (
        """CREATE TABLE business_mail_receipts (
	tenant_id VARCHAR(40) NOT NULL, 
	mailbox_id VARCHAR(40) NOT NULL, 
	uidvalidity BIGINT NOT NULL, 
	uid BIGINT NOT NULL, 
	message_id VARCHAR(40) NOT NULL, 
	mime_sha256 VARCHAR(64), 
	source_sha256 VARCHAR(64) NOT NULL, 
	source_kind VARCHAR(20) NOT NULL, 
	artifact_id VARCHAR(40) NOT NULL, 
	PRIMARY KEY (tenant_id, mailbox_id, uidvalidity, uid), 
	FOREIGN KEY(tenant_id, message_id, mailbox_id) REFERENCES business_mail_messages (tenant_id, message_id, mailbox_id) ON DELETE RESTRICT, 
	FOREIGN KEY(tenant_id, artifact_id) REFERENCES raw_artifacts (tenant_id, artifact_id) ON DELETE RESTRICT, 
	CONSTRAINT ck_business_receipt_source CHECK ((source_kind='full_mime' AND mime_sha256=source_sha256) OR (source_kind='headers_only' AND mime_sha256 IS NULL))
)""",
    ),
    "business_mail_classifications": (
        """CREATE TABLE business_mail_classifications (
	tenant_id VARCHAR(40) NOT NULL, 
	classification_id VARCHAR(40) NOT NULL, 
	message_id VARCHAR(40) NOT NULL, 
	revision INTEGER NOT NULL, 
	category VARCHAR(20) NOT NULL, 
	origin VARCHAR(20) NOT NULL, 
	reason TEXT NOT NULL, 
	evidence_quote TEXT, 
	model VARCHAR(128), 
	configuration_version VARCHAR(128), 
	actor_id VARCHAR(40), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (tenant_id, classification_id), 
	FOREIGN KEY(tenant_id, message_id) REFERENCES business_mail_messages (tenant_id, message_id) ON DELETE RESTRICT, 
	CONSTRAINT uq_business_mail_classification_revision UNIQUE (tenant_id, message_id, revision), 
	CONSTRAINT ck_business_mail_classification CHECK (category IN ('business','other','review') AND origin IN ('model','human','system'))
)""",
    ),
}


def upgrade() -> None:
    # DDL已冻结；后续ORM演进不得改变历史迁移。
    for name, statements in TABLES.items():
        for statement in statements:
            op.execute(sa.text(statement))
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
    op.execute("""CREATE FUNCTION business_mail_facts_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
IF (NEW.tenant_id,NEW.message_id,NEW.mailbox_id,NEW.sender,NEW.subject,NEW.body_text,NEW.message_header,NEW.received_at,NEW.ingested_at,NEW.content_sha256,NEW.mime_sha256,NEW.source_sha256,NEW.source_kind,NEW.artifact_id,NEW.uidvalidity,NEW.uid,NEW.run_id)
 IS DISTINCT FROM
(OLD.tenant_id,OLD.message_id,OLD.mailbox_id,OLD.sender,OLD.subject,OLD.body_text,OLD.message_header,OLD.received_at,OLD.ingested_at,OLD.content_sha256,OLD.mime_sha256,OLD.source_sha256,OLD.source_kind,OLD.artifact_id,OLD.uidvalidity,OLD.uid,OLD.run_id)
THEN RAISE EXCEPTION 'immutable business mail facts'; END IF; RETURN NEW; END $$""")
    op.execute(
        "CREATE TRIGGER business_mail_facts_immutable BEFORE UPDATE ON business_mail_messages FOR EACH ROW EXECUTE FUNCTION business_mail_facts_immutable()"
    )
    op.execute("""CREATE FUNCTION business_mail_history_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'immutable business mail history'; END $$""")
    for name in ("business_mail_receipts", "business_mail_classifications"):
        op.execute(
            f"CREATE TRIGGER business_mail_history_immutable BEFORE UPDATE OR DELETE ON {name} FOR EACH ROW EXECUTE FUNCTION business_mail_history_immutable()"
        )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    for name in reversed(TABLES):
        op.execute(f"LOCK TABLE {name} IN ACCESS EXCLUSIVE MODE")
        if op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {name})")):
            raise RuntimeError("已有普通来信或分类历史，拒绝有损回退")
        op.drop_table(name)
    op.execute("DROP FUNCTION business_mail_history_immutable()")
    op.execute("DROP FUNCTION business_mail_facts_immutable()")
