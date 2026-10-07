"""0012 邮件投递反馈 cursor、receipt、quarantine 与 one-click token。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ATTEMPT_CORRELATION_GRAMMAR = (
    "deterministic_message_id IS NULL OR "
    "(deterministic_message_id ~ "
    "'^<[a-z0-9-]{1,32}\\.[0-9a-f]{64}@messages\\.tradeos\\.invalid>$' "
    "AND idempotency_header ~ '^[a-z0-9-]{1,32}\\.[0-9a-f]{64}$' "
    "AND substring(deterministic_message_id FROM "
    "'^<([a-z0-9-]{1,32}\\.[0-9a-f]{64})@messages\\.tradeos\\.invalid>$') "
    "= idempotency_header AND lower(idempotency_header) !~ "
    "'(^|[-.])(bearer|token|secret|password)([-.]|$)')"
)


def _create_guards() -> None:
    op.execute(
        """
        CREATE FUNCTION guard_email_feedback_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '邮件反馈事实不可修改' USING ERRCODE='23514';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION guard_email_feedback_cursor_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION '邮件反馈 cursor 不可删除' USING ERRCODE='23514';
            END IF;
            IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
               OR NEW.mailbox_alias IS DISTINCT FROM OLD.mailbox_alias
               OR NEW.bootstrap_started_at IS DISTINCT FROM OLD.bootstrap_started_at
               OR NEW.version <> OLD.version + 1
               OR (OLD.last_succeeded_at IS NOT NULL AND
                   (NEW.last_succeeded_at IS NULL OR
                    NEW.last_succeeded_at < OLD.last_succeeded_at)) THEN
                RAISE EXCEPTION '邮件反馈 cursor 更新无效' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION guard_unsubscribe_token_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION '退订 token 不可删除' USING ERRCODE='23514';
            END IF;
            IF NEW.tenant_id IS DISTINCT FROM OLD.tenant_id
               OR NEW.nonce_sha256 IS DISTINCT FROM OLD.nonce_sha256
               OR NEW.contact_point_id IS DISTINCT FROM OLD.contact_point_id
               OR NEW.message_attempt_id IS DISTINCT FROM OLD.message_attempt_id
               OR NEW.key_id IS DISTINCT FROM OLD.key_id
               OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR OLD.consumed_at IS NOT NULL
               OR NEW.consumed_at IS NULL THEN
                RAISE EXCEPTION '退订 token 更新无效' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_email_feedback_receipts_append_only "
        "BEFORE UPDATE OR DELETE ON email_feedback_receipts "
        "FOR EACH ROW EXECUTE FUNCTION guard_email_feedback_append_only()"
    )
    op.execute(
        "CREATE TRIGGER trg_email_feedback_quarantines_append_only "
        "BEFORE UPDATE OR DELETE ON email_feedback_quarantines "
        "FOR EACH ROW EXECUTE FUNCTION guard_email_feedback_append_only()"
    )
    op.execute(
        "CREATE TRIGGER trg_email_feedback_cursors_guard "
        "BEFORE UPDATE OR DELETE ON email_feedback_cursors "
        "FOR EACH ROW EXECUTE FUNCTION guard_email_feedback_cursor_update()"
    )
    op.execute(
        "CREATE TRIGGER trg_unsubscribe_tokens_guard "
        "BEFORE UPDATE OR DELETE ON unsubscribe_tokens "
        "FOR EACH ROW EXECUTE FUNCTION guard_unsubscribe_token_update()"
    )


def upgrade() -> None:
    """增加精确 correlation 与整页反馈持久化护栏。"""
    op.add_column(
        "outreach_message_attempts",
        sa.Column("deterministic_message_id", sa.String(256), nullable=True),
    )
    op.add_column(
        "outreach_message_attempts",
        sa.Column("idempotency_header", sa.String(128), nullable=True),
    )
    op.create_check_constraint(
        "ck_outreach_attempt_correlation_pair",
        "outreach_message_attempts",
        "(deterministic_message_id IS NULL AND idempotency_header IS NULL) OR "
        "(deterministic_message_id IS NOT NULL AND idempotency_header IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_outreach_attempt_correlation_grammar",
        "outreach_message_attempts",
        _ATTEMPT_CORRELATION_GRAMMAR,
    )
    op.create_index(
        "uq_outreach_attempts_tenant_message_id",
        "outreach_message_attempts",
        ["tenant_id", "deterministic_message_id"],
        unique=True,
        postgresql_where=sa.text("deterministic_message_id IS NOT NULL"),
    )
    op.create_index(
        "uq_outreach_attempts_tenant_idempotency_header",
        "outreach_message_attempts",
        ["tenant_id", "idempotency_header"],
        unique=True,
        postgresql_where=sa.text("idempotency_header IS NOT NULL"),
    )

    op.create_table(
        "email_feedback_cursors",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("mailbox_alias", sa.String(32), nullable=False),
        sa.Column("provider_cursor", sa.String(32768), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("bootstrap_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_succeeded_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id", "mailbox_alias", name="pk_email_feedback_cursors"
        ),
        sa.CheckConstraint(
            "tenant_id ~ '^tn_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_email_feedback_cursor_tenant",
        ),
        sa.CheckConstraint(
            "mailbox_alias ~ '^[a-z][a-z0-9-]{0,31}$'",
            name="ck_email_feedback_cursor_mailbox",
        ),
        sa.CheckConstraint("version >= 0", name="ck_email_feedback_cursor_version"),
        sa.CheckConstraint(
            "provider_cursor IS NULL OR (length(provider_cursor) BETWEEN 1 AND 32768)",
            name="ck_email_feedback_cursor_value",
        ),
    )
    op.create_table(
        "email_feedback_receipts",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("mailbox_alias", sa.String(32), nullable=False),
        sa.Column("provider_event_id", sa.String(64), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("attempt_id", sa.String(32), nullable=True),
        sa.Column("enrollment_id", sa.String(32), nullable=True),
        sa.Column("account_id", sa.String(32), nullable=True),
        sa.Column("contact_point_id", sa.String(32), nullable=True),
        sa.Column("sending_identity_id", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "mailbox_alias",
            "provider_event_id",
            name="pk_email_feedback_receipts",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "mailbox_alias"],
            [
                "email_feedback_cursors.tenant_id",
                "email_feedback_cursors.mailbox_alias",
            ],
            ondelete="RESTRICT",
            name="fk_email_feedback_receipts_cursor",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "attempt_id"],
            [
                "outreach_message_attempts.tenant_id",
                "outreach_message_attempts.attempt_id",
            ],
            ondelete="RESTRICT",
            name="fk_email_feedback_receipts_attempt",
        ),
        sa.CheckConstraint(
            "tenant_id ~ '^tn_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_email_feedback_receipt_tenant",
        ),
        sa.CheckConstraint(
            "mailbox_alias ~ '^[a-z][a-z0-9-]{0,31}$'",
            name="ck_email_feedback_receipt_mailbox",
        ),
        sa.CheckConstraint(
            "provider_event_id ~ '^[0-9a-f]{64}$'",
            name="ck_email_feedback_receipt_event",
        ),
        sa.CheckConstraint(
            "ordinal BETWEEN 0 AND 99", name="ck_email_feedback_receipt_ordinal"
        ),
        sa.CheckConstraint(
            "kind IN ('hard_bounce','soft_bounce','unparseable')",
            name="ck_email_feedback_receipt_kind",
        ),
        sa.CheckConstraint(
            "result IN ('applied','recorded','quarantined')",
            name="ck_email_feedback_receipt_result",
        ),
        sa.CheckConstraint(
            "(result='quarantined' AND kind='unparseable' AND attempt_id IS NULL "
            "AND enrollment_id IS NULL AND account_id IS NULL "
            "AND contact_point_id IS NULL AND sending_identity_id IS NULL) OR "
            "(result IN ('applied','recorded') AND kind IN ('hard_bounce','soft_bounce') "
            "AND attempt_id IS NOT NULL AND enrollment_id IS NOT NULL "
            "AND account_id IS NOT NULL AND contact_point_id IS NOT NULL "
            "AND sending_identity_id IS NOT NULL)",
            name="ck_email_feedback_receipt_target",
        ),
    )
    op.create_index(
        "ix_email_feedback_receipts_tenant_mailbox_created",
        "email_feedback_receipts",
        ["tenant_id", "mailbox_alias", "created_at", "provider_event_id"],
    )
    op.create_table(
        "email_feedback_quarantines",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("mailbox_alias", sa.String(32), nullable=False),
        sa.Column("provider_event_id", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("provider_ref_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "mailbox_alias",
            "provider_event_id",
            name="pk_email_feedback_quarantines",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "mailbox_alias", "provider_event_id"],
            [
                "email_feedback_receipts.tenant_id",
                "email_feedback_receipts.mailbox_alias",
                "email_feedback_receipts.provider_event_id",
            ],
            ondelete="RESTRICT",
            name="fk_email_feedback_quarantine_receipt",
        ),
        sa.CheckConstraint(
            "reason IN ('malformed','unsupported','missing-correlation',"
            "'ambiguous-correlation','cross-tenant-correlation')",
            name="ck_email_feedback_quarantine_reason",
        ),
        sa.CheckConstraint(
            "provider_ref_digest ~ '^[0-9a-f]{64}$'",
            name="ck_email_feedback_quarantine_digest",
        ),
    )
    op.create_index(
        "ix_email_feedback_quarantines_tenant_created",
        "email_feedback_quarantines",
        ["tenant_id", "created_at", "provider_event_id"],
    )
    op.create_table(
        "unsubscribe_tokens",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("nonce_sha256", sa.LargeBinary(32), nullable=False),
        sa.Column("contact_point_id", sa.String(32), nullable=False),
        sa.Column("message_attempt_id", sa.String(32), nullable=False),
        sa.Column("key_id", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "nonce_sha256", name="pk_unsubscribe_tokens"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "message_attempt_id"],
            [
                "outreach_message_attempts.tenant_id",
                "outreach_message_attempts.attempt_id",
            ],
            ondelete="RESTRICT",
            name="fk_unsubscribe_token_attempt",
        ),
        sa.CheckConstraint(
            "tenant_id ~ '^tn_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_unsubscribe_token_tenant",
        ),
        sa.CheckConstraint(
            "octet_length(nonce_sha256)=32", name="ck_unsubscribe_token_nonce"
        ),
        sa.CheckConstraint(
            "contact_point_id ~ '^cp_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_unsubscribe_token_contact",
        ),
        sa.CheckConstraint(
            "message_attempt_id ~ '^mat_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_unsubscribe_token_attempt",
        ),
        sa.CheckConstraint(
            "key_id ~ '^[a-z0-9-]{1,32}$'",
            name="ck_unsubscribe_token_key",
        ),
        sa.CheckConstraint(
            "expires_at = created_at + interval '90 days'",
            name="ck_unsubscribe_token_expiry",
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR "
            "(consumed_at >= created_at AND consumed_at < expires_at)",
            name="ck_unsubscribe_token_consumed",
        ),
    )
    op.create_index(
        "ix_unsubscribe_tokens_tenant_attempt",
        "unsubscribe_tokens",
        ["tenant_id", "message_attempt_id", "created_at"],
    )
    _create_guards()


def downgrade() -> None:
    """删除反馈持久化，并恢复 0011 Attempt schema。"""
    for trigger, table in (
        ("trg_unsubscribe_tokens_guard", "unsubscribe_tokens"),
        ("trg_email_feedback_quarantines_append_only", "email_feedback_quarantines"),
        ("trg_email_feedback_receipts_append_only", "email_feedback_receipts"),
        ("trg_email_feedback_cursors_guard", "email_feedback_cursors"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    op.drop_table("unsubscribe_tokens")
    op.drop_table("email_feedback_quarantines")
    op.drop_table("email_feedback_receipts")
    op.drop_table("email_feedback_cursors")
    for function in (
        "guard_unsubscribe_token_update",
        "guard_email_feedback_cursor_update",
        "guard_email_feedback_append_only",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")
    op.drop_index(
        "uq_outreach_attempts_tenant_idempotency_header",
        table_name="outreach_message_attempts",
    )
    op.drop_index(
        "uq_outreach_attempts_tenant_message_id",
        table_name="outreach_message_attempts",
    )
    op.drop_constraint(
        "ck_outreach_attempt_correlation_grammar",
        "outreach_message_attempts",
        type_="check",
    )
    op.drop_constraint(
        "ck_outreach_attempt_correlation_pair",
        "outreach_message_attempts",
        type_="check",
    )
    op.drop_column("outreach_message_attempts", "idempotency_header")
    op.drop_column("outreach_message_attempts", "deterministic_message_id")
