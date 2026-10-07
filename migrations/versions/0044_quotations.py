"""不可变报价版本及人工抬头。Revision0044，前序0043。"""

from alembic import op
from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import postgresql

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """建立tenant复合FK和不可变内容；不回改旧迁移。"""
    op.create_table(
        "quotation_issuers",
        Column("tenant_id", String(40), nullable=False),
        Column("issuer_id", String(40), nullable=False),
        Column("version", Integer, nullable=False),
        Column("idempotency_key", String(128), nullable=False),
        Column("request_hash", String(64), nullable=False),
        Column("content_hash", String(64), nullable=False),
        Column("confirmed_by", String(40), nullable=False),
        Column("confirmed_at", DateTime(timezone=True), nullable=False),
        Column("payload", postgresql.JSONB, nullable=False),
        PrimaryKeyConstraint("tenant_id", "issuer_id", name="pk_quotation_issuers"),
        UniqueConstraint("tenant_id", "version", name="uq_quotation_issuers_version"),
        UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_quotation_issuers_key"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "confirmed_by"],
            ["employees.tenant_id", "employees.employee_id"],
            name="fk_quotation_issuers_employee",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "version>0 AND idempotency_key=btrim(idempotency_key) AND length(idempotency_key)>0 AND idempotency_key !~ '[[:cntrl:]]'",
            name="ck_quotation_issuers_input",
        ),
        CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_quotation_issuers_hash",
        ),
    )
    op.create_table(
        "quotations",
        Column("tenant_id", String(40), nullable=False),
        Column("quote_id", String(40), nullable=False),
        Column("opportunity_id", String(40), nullable=False),
        Column("version", Integer, nullable=False),
        Column("state", String(20), nullable=False),
        Column("operation_id", String(64), nullable=False),
        Column("request_hash", String(64), nullable=False),
        Column("basis_id", String(64), nullable=False),
        Column("cost_sheet_id", String(40), nullable=False),
        Column("issuer_id", String(40), nullable=False),
        Column("content_hash", String(64), nullable=False),
        Column("prepared_by", String(40), nullable=False),
        Column("owner_id", String(40), nullable=False),
        Column("replaces_quote_id", String(40), nullable=True),
        Column("replaced_quote_version", Integer, nullable=True),
        Column("valid_until", DateTime(timezone=True), nullable=False),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("content", postgresql.JSONB, nullable=False),
        PrimaryKeyConstraint("tenant_id", "quote_id", name="pk_quotations"),
        UniqueConstraint(
            "tenant_id", "opportunity_id", "version", name="uq_quotations_version"
        ),
        UniqueConstraint("tenant_id", "operation_id", name="uq_quotations_operation"),
        ForeignKeyConstraint(
            ["tenant_id", "opportunity_id"],
            ["opportunities.tenant_id", "opportunities.opportunity_id"],
            name="fk_quotations_opportunity_id",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            [
                "quote_creation_operations.tenant_id",
                "quote_creation_operations.operation_id",
            ],
            name="fk_quotations_operation_id",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "basis_id"],
            ["costing_quote_bases.tenant_id", "costing_quote_bases.basis_id"],
            name="fk_quotations_basis_id",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "cost_sheet_id"],
            ["cost_sheets.tenant_id", "cost_sheets.cost_sheet_id"],
            name="fk_quotations_cost_sheet_id",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "issuer_id"],
            ["quotation_issuers.tenant_id", "quotation_issuers.issuer_id"],
            name="fk_quotations_issuer_id",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "prepared_by"],
            ["employees.tenant_id", "employees.employee_id"],
            name="fk_quotations_prepared_by",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "owner_id"],
            ["employees.tenant_id", "employees.employee_id"],
            name="fk_quotations_owner_id",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "replaces_quote_id"],
            ["quotations.tenant_id", "quotations.quote_id"],
            name="fk_quotations_replaces_quote_id",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "version>0 AND isfinite(valid_until) AND isfinite(created_at) AND valid_until>created_at",
            name="ck_quotations_time",
        ),
        CheckConstraint(
            "(replaces_quote_id IS NULL AND replaced_quote_version IS NULL) OR (replaces_quote_id IS NOT NULL AND replaced_quote_version>0)",
            name="ck_quotations_revision",
        ),
        CheckConstraint(
            "state IN ('draft','pending_approval','approved','sent','accepted','rejected','expired','superseded')",
            name="ck_quotations_state",
        ),
        CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$' AND content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_quotations_hash",
        ),
    )
    op.create_table(
        "quotation_lines",
        Column("tenant_id", String(40), nullable=False),
        Column("quote_id", String(40), nullable=False),
        Column("line_number", Integer, nullable=False),
        Column("payload", postgresql.JSONB, nullable=False),
        PrimaryKeyConstraint(
            "tenant_id", "quote_id", "line_number", name="pk_quotation_lines"
        ),
        CheckConstraint("line_number=1", name="ck_quotation_lines_one"),
        ForeignKeyConstraint(
            ["tenant_id", "quote_id"],
            ["quotations.tenant_id", "quotations.quote_id"],
            name="fk_quotation_lines_quote",
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "quotation_evidence_refs",
        Column("tenant_id", String(40), nullable=False),
        Column("quote_id", String(40), nullable=False),
        Column("evidence_id", String(64), nullable=False),
        Column("evidence_hash", String(64), nullable=False),
        Column("kind", String(24), nullable=False),
        PrimaryKeyConstraint(
            "tenant_id", "quote_id", "evidence_id", name="pk_quotation_evidence_refs"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "evidence_id"],
            ["costing_price_evidence.tenant_id", "costing_price_evidence.evidence_id"],
            name="fk_quotation_evidence_refs_evidence",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "quote_id"],
            ["quotations.tenant_id", "quotations.quote_id"],
            name="fk_quotation_evidence_refs_quote",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "evidence_hash ~ '^[0-9a-f]{64}$'", name="ck_quotation_evidence_refs_hash"
        ),
    )
    op.create_table(
        "quotation_state_events",
        Column("tenant_id", String(40), nullable=False),
        Column("event_id", String(40), nullable=False),
        Column("quote_id", String(40), nullable=False),
        Column("from_state", String(20), nullable=True),
        Column("to_state", String(20), nullable=False),
        Column("actor_id", String(40), nullable=True),
        Column("reason", String(32), nullable=False),
        Column("at", DateTime(timezone=True), nullable=False),
        Column("reference_id", String(64), nullable=True),
        PrimaryKeyConstraint("tenant_id", "event_id", name="pk_quotation_state_events"),
        ForeignKeyConstraint(
            ["tenant_id", "actor_id"],
            ["employees.tenant_id", "employees.employee_id"],
            name="fk_quotation_state_events_employee",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "tenant_id", "quote_id", "to_state", name="uq_quotation_state_events_target"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "quote_id"],
            ["quotations.tenant_id", "quotations.quote_id"],
            name="fk_quotation_state_events_quote",
            ondelete="RESTRICT",
        ),
    )
    op.create_table(
        "quotation_approval_bindings",
        Column("tenant_id", String(40), nullable=False),
        Column("quote_id", String(40), nullable=False),
        Column("approval_type", String(64), nullable=False),
        Column("approval_id", String(40), nullable=False),
        Column("quote_version", Integer, nullable=False),
        Column("content_hash", String(64), nullable=False),
        Column("bound_at", DateTime(timezone=True), nullable=False),
        PrimaryKeyConstraint(
            "tenant_id",
            "quote_id",
            "approval_type",
            "approval_id",
            name="pk_quotation_approval_bindings",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "approval_id"],
            ["approval_packages.tenant_id", "approval_packages.approval_id"],
            name="fk_quotation_approval_bindings_approval",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "quote_id"],
            ["quotations.tenant_id", "quotations.quote_id"],
            name="fk_quotation_approval_bindings_quote",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_quotation_approval_bindings_hash",
        ),
    )
    op.create_table(
        "quotation_send_receipts",
        Column("tenant_id", String(40), nullable=False),
        Column("attempt_id", String(40), nullable=False),
        Column("quote_id", String(40), nullable=False),
        Column("content_hash", String(64), nullable=False),
        Column("sent_at", DateTime(timezone=True), nullable=False),
        PrimaryKeyConstraint(
            "tenant_id", "attempt_id", name="pk_quotation_send_receipts"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "attempt_id"],
            [
                "outreach_message_attempts.tenant_id",
                "outreach_message_attempts.attempt_id",
            ],
            name="fk_quotation_send_receipts_attempt",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "quote_id"],
            ["quotations.tenant_id", "quotations.quote_id"],
            name="fk_quotation_send_receipts_quote",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'", name="ck_quotation_send_receipts_hash"
        ),
    )
    op.create_index(
        "uq_quotations_active",
        "quotations",
        ["tenant_id", "opportunity_id"],
        unique=True,
        postgresql_where=text(
            "state IN ('draft','pending_approval','approved','sent')"
        ),
    )
    _triggers()


def _triggers() -> None:
    """SQL绕过不能修改历史内容或制造无审计状态。"""
    op.execute("""CREATE FUNCTION reject_quotation_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'immutable quotation record'; END; $$ LANGUAGE plpgsql""")
    for table in (
        "quotation_issuers",
        "quotation_lines",
        "quotation_evidence_refs",
        "quotation_state_events",
        "quotation_approval_bindings",
        "quotation_send_receipts",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION reject_quotation_mutation()"
        )
    op.execute("""CREATE FUNCTION guard_quotation_write() RETURNS trigger AS $$
      DECLARE operation quote_creation_operations%ROWTYPE; basis costing_quote_bases%ROWTYPE;
        issuer quotation_issuers%ROWTYPE; mapped jsonb; prices jsonb; field text;
      BEGIN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable quotation'; END IF;
        IF TG_OP='UPDATE' THEN
          IF (to_jsonb(NEW)-'state') IS DISTINCT FROM (to_jsonb(OLD)-'state') THEN
            RAISE EXCEPTION 'immutable quotation content';
          END IF;
          IF NOT ((OLD.state='draft' AND NEW.state IN ('pending_approval','expired','superseded')) OR
            (OLD.state='pending_approval' AND NEW.state IN ('approved','rejected','expired','superseded')) OR
            (OLD.state='approved' AND NEW.state IN ('sent','expired','superseded')) OR
            (OLD.state='sent' AND NEW.state IN ('accepted','rejected','expired','superseded'))) THEN
            RAISE EXCEPTION 'invalid quotation transition';
          END IF;
          RETURN NEW;
        END IF;
        IF NEW.state<>'draft' THEN RAISE EXCEPTION 'quotation must start draft'; END IF;
        SELECT * INTO operation FROM quote_creation_operations
          WHERE tenant_id=NEW.tenant_id AND operation_id=NEW.operation_id;
        SELECT * INTO basis FROM costing_quote_bases
          WHERE tenant_id=NEW.tenant_id AND basis_id=NEW.basis_id;
        SELECT * INTO issuer FROM quotation_issuers
          WHERE tenant_id=NEW.tenant_id AND issuer_id=NEW.issuer_id;
        IF operation.operation_id IS NULL OR basis.basis_id IS NULL OR issuer.issuer_id IS NULL OR
          operation.request_hash IS DISTINCT FROM NEW.request_hash OR operation.basis_id IS DISTINCT FROM NEW.basis_id OR
          operation.cost_sheet_id IS DISTINCT FROM NEW.cost_sheet_id OR basis.operation_id IS DISTINCT FROM NEW.operation_id OR
          basis.cost_sheet_id IS DISTINCT FROM NEW.cost_sheet_id OR basis.opportunity_id IS DISTINCT FROM NEW.opportunity_id OR
          operation.intent IS DISTINCT FROM NEW.content->'intent' OR
          (operation.intent->>'replaces_quote_id') IS DISTINCT FROM NEW.replaces_quote_id OR
          (operation.intent->>'expected_quote_version')::integer IS DISTINCT FROM NEW.replaced_quote_version OR
          issuer.payload IS DISTINCT FROM NEW.content->'issuer' THEN
          RAISE EXCEPTION 'quotation binding mismatch';
        END IF;
        FOREACH field IN ARRAY ARRAY['tenant_id','quote_id','opportunity_id','version','operation_id','request_hash',
          'prepared_by','owner_id','replaces_quote_id','replaced_quote_version','content_hash'] LOOP
          IF to_jsonb(NEW)->field IS DISTINCT FROM NEW.content->field THEN
            RAISE EXCEPTION 'quotation column mismatch';
          END IF;
        END LOOP;
        IF (NEW.content->>'valid_until')::timestamptz IS DISTINCT FROM NEW.valid_until OR
           (NEW.content->>'created_at')::timestamptz IS DISTINCT FROM NEW.created_at THEN
          RAISE EXCEPTION 'quotation clock mismatch';
        END IF;
        SELECT coalesce(jsonb_agg((value-ARRAY['amount','currency']) || jsonb_build_object(
          'tenant_id',NEW.tenant_id,'amount',jsonb_build_object('amount',value->'amount','currency',value->'currency'))
          ORDER BY ord),'[]'::jsonb) INTO prices
          FROM jsonb_array_elements(basis.payload->'price_evidence') WITH ORDINALITY AS p(value,ord);
        mapped=jsonb_set(basis.payload,'{price_evidence}',prices);
        IF mapped IS DISTINCT FROM NEW.content->'basis' THEN RAISE EXCEPTION 'quotation basis mismatch'; END IF;
        RETURN NEW;
      END; $$ LANGUAGE plpgsql""")
    op.execute(
        "CREATE TRIGGER trg_quotations_guard BEFORE INSERT OR UPDATE OR DELETE ON quotations "
        "FOR EACH ROW EXECUTE FUNCTION guard_quotation_write()"
    )
    op.execute("""CREATE FUNCTION audit_quotation_write() RETURNS trigger AS $$
      DECLARE old_state text; n integer;
      BEGIN
        old_state=CASE WHEN TG_OP='INSERT' THEN NULL ELSE OLD.state END;
        SELECT count(*) INTO n FROM quotation_state_events WHERE tenant_id=NEW.tenant_id AND quote_id=NEW.quote_id
          AND from_state IS NOT DISTINCT FROM old_state AND to_state=NEW.state
          AND xmin=pg_current_xact_id()::text::xid;
        IF n<>1 THEN RAISE EXCEPTION 'quotation state event required'; END IF;
        IF TG_OP='INSERT' THEN
          SELECT count(*) INTO n FROM quotation_lines WHERE tenant_id=NEW.tenant_id AND quote_id=NEW.quote_id;
          IF n<>1 THEN RAISE EXCEPTION 'quotation single line required'; END IF;
          SELECT count(*) INTO n FROM quotation_evidence_refs WHERE tenant_id=NEW.tenant_id AND quote_id=NEW.quote_id;
          IF n<>jsonb_array_length(NEW.content->'basis'->'price_evidence') THEN
            RAISE EXCEPTION 'quotation evidence incomplete'; END IF;
        END IF;
        RETURN NULL;
      END; $$ LANGUAGE plpgsql""")
    op.execute(
        "CREATE CONSTRAINT TRIGGER trg_quotations_audit AFTER INSERT OR UPDATE ON quotations "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION audit_quotation_write()"
    )
    op.execute("""CREATE FUNCTION guard_quotation_child() RETURNS trigger AS $$
      DECLARE quote quotations%ROWTYPE; price costing_price_evidence%ROWTYPE; evidence jsonb;
      BEGIN
        SELECT * INTO quote FROM quotations WHERE tenant_id=NEW.tenant_id AND quote_id=NEW.quote_id;
        IF quote.quote_id IS NULL THEN RAISE EXCEPTION 'quotation child missing parent'; END IF;
        IF TG_TABLE_NAME='quotation_lines' THEN
          IF NEW.payload IS DISTINCT FROM quote.content->'lines'->0 THEN
            RAISE EXCEPTION 'quotation line mismatch'; END IF;
        ELSIF TG_TABLE_NAME='quotation_evidence_refs' THEN
          SELECT * INTO price FROM costing_price_evidence WHERE tenant_id=NEW.tenant_id AND evidence_id=NEW.evidence_id;
          SELECT value INTO evidence FROM jsonb_array_elements(quote.content->'basis'->'price_evidence')
            WHERE value->>'evidence_id'=NEW.evidence_id;
          IF evidence IS NULL OR price.evidence_id IS NULL OR NEW.evidence_hash IS DISTINCT FROM price.evidence_hash OR
            NEW.kind IS DISTINCT FROM price.payload->>'kind' OR NEW.evidence_hash IS DISTINCT FROM evidence->>'evidence_hash' OR
            NEW.kind IS DISTINCT FROM evidence->>'kind' THEN RAISE EXCEPTION 'quotation evidence mismatch'; END IF;
        ELSIF TG_TABLE_NAME='quotation_state_events' THEN
          IF NEW.to_state IS DISTINCT FROM quote.state THEN RAISE EXCEPTION 'quotation event state mismatch'; END IF;
        ELSIF TG_TABLE_NAME='quotation_send_receipts' THEN
          IF NEW.content_hash IS DISTINCT FROM quote.content_hash THEN RAISE EXCEPTION 'quotation receipt mismatch'; END IF;
        ELSIF TG_TABLE_NAME='quotation_approval_bindings' THEN
          IF NEW.content_hash IS DISTINCT FROM quote.content_hash OR NEW.quote_version IS DISTINCT FROM quote.version THEN
            RAISE EXCEPTION 'quotation approval binding mismatch'; END IF;
        END IF;
        RETURN NEW;
      END; $$ LANGUAGE plpgsql""")
    for table in (
        "quotation_lines",
        "quotation_evidence_refs",
        "quotation_state_events",
        "quotation_approval_bindings",
        "quotation_send_receipts",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_binding BEFORE INSERT ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION guard_quotation_child()"
        )


def downgrade() -> None:
    """隔离迁移往返；删除商业历史须由运维另行获得授权。"""
    for table in (
        "quotation_issuers",
        "quotations",
        "quotation_lines",
        "quotation_evidence_refs",
        "quotation_state_events",
        "quotation_approval_bindings",
        "quotation_send_receipts",
    ):
        if (
            op.get_bind()
            .execute(text(f"SELECT EXISTS (SELECT 1 FROM {table})"))
            .scalar()
        ):
            raise RuntimeError("存在不可变报价历史，拒绝降级")
    op.drop_table("quotation_send_receipts")
    op.drop_table("quotation_approval_bindings")
    op.drop_table("quotation_state_events")
    op.drop_table("quotation_evidence_refs")
    op.drop_table("quotation_lines")
    op.drop_table("quotations")
    op.drop_table("quotation_issuers")
    op.execute("DROP FUNCTION reject_quotation_mutation()")
    op.execute("DROP FUNCTION guard_quotation_write()")
    op.execute("DROP FUNCTION audit_quotation_write()")
    op.execute("DROP FUNCTION guard_quotation_child()")
