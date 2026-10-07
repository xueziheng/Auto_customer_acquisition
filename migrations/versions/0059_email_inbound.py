"""入站独立游标、只增receipt与待核对；0058之后。"""

from alembic import op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE email_inbound_cursors (
	tenant_id VARCHAR(40) NOT NULL,
	mailbox_alias VARCHAR(32) NOT NULL,
	configured_identity_id VARCHAR(40) NOT NULL,
	route_id VARCHAR(32) NOT NULL,
	config_version VARCHAR(32) NOT NULL,
	provider_cursor TEXT NOT NULL,
	version BIGINT NOT NULL,
	bootstrap_started_at TIMESTAMP WITH TIME ZONE NOT NULL,
	after_epoch BIGINT NOT NULL,
	confirmed_by VARCHAR(40) NOT NULL,
	confirmed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	last_succeeded_at TIMESTAMP WITH TIME ZONE,
	blocked_reason VARCHAR(40),
	next_retry_at TIMESTAMP WITH TIME ZONE,
	CONSTRAINT pk_email_inbound_cursors PRIMARY KEY (tenant_id, mailbox_alias),
	CONSTRAINT fk_email_inbound_cursor_identity FOREIGN KEY(tenant_id, configured_identity_id) REFERENCES sending_identities (tenant_id, identity_id) ON DELETE RESTRICT,
	CONSTRAINT ck_email_inbound_cursor_version CHECK (version >= 1 AND octet_length(provider_cursor) BETWEEN 1 AND 32768 AND after_epoch >= 0)
)
    """)
    op.execute("""
CREATE TABLE email_inbound_receipts (
	tenant_id VARCHAR(40) NOT NULL,
	mailbox_alias VARCHAR(32) NOT NULL,
	provider_ref_digest VARCHAR(64) NOT NULL,
	item_fingerprint VARCHAR(64) NOT NULL,
	parser_version VARCHAR(32) NOT NULL,
	guard_version VARCHAR(32) NOT NULL,
	disposition VARCHAR(40) NOT NULL,
	raw_artifact_id VARCHAR(32),
	raw_hash VARCHAR(64),
	raw_size BIGINT,
	message_id VARCHAR(32),
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_email_inbound_receipts PRIMARY KEY (tenant_id, mailbox_alias, provider_ref_digest),
	CONSTRAINT fk_email_inbound_receipt_cursor FOREIGN KEY(tenant_id, mailbox_alias) REFERENCES email_inbound_cursors (tenant_id, mailbox_alias) ON DELETE RESTRICT,
	CONSTRAINT fk_email_inbound_receipt_raw FOREIGN KEY(tenant_id, raw_artifact_id) REFERENCES raw_artifacts (tenant_id, artifact_id) ON DELETE RESTRICT,
	CONSTRAINT fk_email_inbound_receipt_message FOREIGN KEY(tenant_id, message_id) REFERENCES messages (tenant_id, message_id) ON DELETE RESTRICT,
	CONSTRAINT ck_email_inbound_receipt_hash CHECK (provider_ref_digest ~ '^[0-9a-f]{64}$' AND item_fingerprint ~ '^[0-9a-f]{64}$'),
	CONSTRAINT ck_email_inbound_receipt_raw CHECK ((raw_artifact_id IS NULL AND raw_hash IS NULL AND raw_size IS NULL) OR (raw_artifact_id IS NOT NULL AND raw_hash IS NOT NULL AND raw_size IS NOT NULL AND raw_hash ~ '^[0-9a-f]{64}$' AND raw_size BETWEEN 1 AND 4194304))
)
    """)
    op.execute("""
CREATE TABLE email_inbound_reviews (
	tenant_id VARCHAR(40) NOT NULL,
	review_id VARCHAR(40) NOT NULL,
	mailbox_alias VARCHAR(32) NOT NULL,
	provider_ref_digest VARCHAR(64) NOT NULL,
	raw_artifact_id VARCHAR(32),
	reason VARCHAR(40) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	CONSTRAINT pk_email_inbound_reviews PRIMARY KEY (tenant_id, review_id),
	CONSTRAINT uq_email_inbound_review_receipt UNIQUE (tenant_id, mailbox_alias, provider_ref_digest),
	CONSTRAINT fk_email_inbound_review_receipt FOREIGN KEY(tenant_id, mailbox_alias, provider_ref_digest) REFERENCES email_inbound_receipts (tenant_id, mailbox_alias, provider_ref_digest) ON DELETE RESTRICT,
	CONSTRAINT fk_email_inbound_review_raw FOREIGN KEY(tenant_id, raw_artifact_id) REFERENCES raw_artifacts (tenant_id, artifact_id) ON DELETE RESTRICT
)
    """)
    op.execute(
        """CREATE FUNCTION email_inbound_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'email inbound immutable'; END $$"""
    )
    for table in ("email_inbound_receipts", "email_inbound_reviews"):
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION email_inbound_immutable()"
        )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM email_inbound_cursors LIMIT 1)
      OR EXISTS (SELECT 1 FROM email_inbound_receipts LIMIT 1)
      OR EXISTS (SELECT 1 FROM email_inbound_reviews LIMIT 1) THEN
        RAISE EXCEPTION '0059 refuses destructive inbound downgrade';
      END IF; END $$""")
    op.drop_table("email_inbound_reviews")
    op.drop_table("email_inbound_receipts")
    op.drop_table("email_inbound_cursors")
    op.execute("DROP FUNCTION email_inbound_immutable()")
