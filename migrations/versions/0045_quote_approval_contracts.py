"""报价单轮审批请求、唯一绑定与不可变成功receipt。

Revision ID: 0045
Revises: 0044
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

_MARKED = """(lower(btrim(coalesce(change_set_ref,''))) LIKE 'quote:%' OR
    lower(btrim(coalesce(proposed_change->>'schema_version',''))) LIKE 'quote-approval%')"""


def _approval_guard(*, new_contract: bool) -> None:
    """保留旧状态边；新增namespace的决定事实不能随应用状态改变。"""
    decision_guard = (
        """AND (OLD.contract_namespace IS NULL OR OLD.state='pending' OR
      (OLD.decided_by IS NOT DISTINCT FROM NEW.decided_by AND
       OLD.decided_at IS NOT DISTINCT FROM NEW.decided_at AND
       OLD.decision_note IS NOT DISTINCT FROM NEW.decision_note))"""
        if new_contract
        else ""
    )
    op.execute(f"""CREATE OR REPLACE FUNCTION approval_package_guard() RETURNS trigger AS $$
      BEGIN
        IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable approval'; END IF;
        IF (to_jsonb(OLD)-ARRAY['state','decided_at','decided_by','decision_note','applied_at','apply_error'])
          IS NOT DISTINCT FROM
          (to_jsonb(NEW)-ARRAY['state','decided_at','decided_by','decision_note','applied_at','apply_error'])
          AND ((OLD.state='pending' AND NEW.state IN ('approved','rejected','expired')) OR
               (OLD.state='approved' AND NEW.state IN ('applied','apply_failed')))
          {decision_guard} THEN RETURN NEW; END IF;
        RAISE EXCEPTION 'immutable approval or invalid transition';
      END; $$ LANGUAGE plpgsql""")


def upgrade() -> None:
    """不能为已有不完整标记行伪造原请求hash或期限。"""
    if (
        op.get_bind()
        .execute(
            sa.text(f"SELECT EXISTS(SELECT 1 FROM approval_packages WHERE {_MARKED})")
        )
        .scalar()
    ):
        raise RuntimeError("存在无法证明原请求的报价审批标记，拒绝迁移")
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS(SELECT 1 FROM quotation_approval_bindings)"))
        .scalar()
    ):
        raise RuntimeError("存在缺少请求快照的旧报价审批绑定，拒绝迁移")
    op.add_column(
        "approval_packages",
        sa.Column("contract_namespace", sa.String(32), nullable=True),
    )
    op.add_column(
        "approval_packages", sa.Column("request_hash", sa.String(64), nullable=True)
    )
    op.add_column(
        "approval_packages",
        sa.Column("expires_at_limit", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_approval_quote_contract",
        "approval_packages",
        f"""
      (contract_namespace IS NULL AND request_hash IS NULL AND NOT {_MARKED}) OR
      coalesce((contract_namespace='quote-approval-v1' AND request_hash ~ '^[0-9a-f]{{64}}$'
        AND expires_at_limit IS NOT NULL AND expires_at <= expires_at_limit
        AND proposed_change->>'schema_version'='quote-approval-v1'
        AND proposed_change->>'tenant_id'=tenant_id
        AND proposed_change->>'approval_type'=approval_type
        AND proposed_change->>'prepared_by'=proposed_by_employee
        AND proposed_change->>'submitted_owner_id'=owner_employee
        AND jsonb_typeof(proposed_change->'quote_version')='number'
        AND (proposed_change->>'quote_version') ~ '^[1-9][0-9]*$'
        AND proposed_change->>'quote_id' ~ '^quo_[0-9A-HJKMNP-TV-Z]{{26}}$'
        AND proposed_change->>'content_hash' ~ '^[0-9a-f]{{64}}$'
        AND approval_type IN ('quote_send','margin_floor_override','discount','delivery_commitment','payment_terms','certification_commitment')
        AND change_set_ref='quote:'||(proposed_change->>'quote_id')||':'||
            (proposed_change->>'content_hash')||':'||approval_type),false)
    """,
    )
    op.create_index(
        "uq_approval_quote_change_set",
        "approval_packages",
        ["tenant_id", "change_set_ref"],
        unique=True,
        postgresql_where=sa.text("contract_namespace='quote-approval-v1'"),
    )
    _approval_guard(new_contract=True)
    for name, type_ in (
        ("request_hash", sa.String(64)),
        ("payload_hash", sa.String(64)),
        ("fact", postgresql.JSONB()),
    ):
        op.add_column(
            "quotation_approval_bindings", sa.Column(name, type_, nullable=False)
        )
    op.create_unique_constraint(
        "uq_quotation_approval_type",
        "quotation_approval_bindings",
        ["tenant_id", "quote_id", "approval_type"],
    )
    op.create_table(
        "quotation_approval_receipts",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("quote_id", sa.String(40), nullable=False),
        sa.Column("quote_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("facts_hash", sa.String(64), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("quote_send_decider", sa.String(40), nullable=False),
        sa.Column("approval_run_id", sa.String(40), nullable=False),
        sa.Column("decisions", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "quote_id", name="pk_quotation_approval_receipts"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "quote_id"],
            ["quotations.tenant_id", "quotations.quote_id"],
            name="fk_quote_approval_receipt_quote",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "quote_send_decider"],
            ["employees.tenant_id", "employees.employee_id"],
            name="fk_quote_approval_receipt_decider",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "approval_run_id"],
            ["workflow_runs.tenant_id", "workflow_runs.run_id"],
            name="fk_quote_approval_receipt_run",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "quote_version>0 AND content_hash ~ '^[0-9a-f]{64}$' AND facts_hash ~ '^[0-9a-f]{64}$' AND jsonb_typeof(decisions)='array'",
            name="ck_quote_approval_receipt_shape",
        ),
    )
    op.execute("""CREATE FUNCTION guard_quote_approval_binding() RETURNS trigger AS $$
      DECLARE p approval_packages%ROWTYPE; q quotations%ROWTYPE;
      BEGIN
        SELECT * INTO p FROM approval_packages WHERE tenant_id=NEW.tenant_id AND approval_id=NEW.approval_id;
        SELECT * INTO q FROM quotations WHERE tenant_id=NEW.tenant_id AND quote_id=NEW.quote_id;
        IF p.contract_namespace IS DISTINCT FROM 'quote-approval-v1' OR p.approval_type IS DISTINCT FROM NEW.approval_type
          OR p.request_hash IS DISTINCT FROM NEW.request_hash OR NEW.payload_hash !~ '^[0-9a-f]{64}$'
          OR NEW.fact->'payload' IS DISTINCT FROM p.proposed_change
          OR NEW.fact->>'tenant_id' IS DISTINCT FROM NEW.tenant_id
          OR NEW.fact->>'approval_id' IS DISTINCT FROM NEW.approval_id
          OR NEW.fact->>'request_hash' IS DISTINCT FROM p.request_hash
          OR NEW.fact->>'change_set_ref' IS DISTINCT FROM p.change_set_ref
          OR NEW.fact->>'approval_type' IS DISTINCT FROM p.approval_type
          OR NEW.fact->>'prepared_by' IS DISTINCT FROM p.proposed_by_employee
          OR NEW.fact->>'submitted_owner_id' IS DISTINCT FROM p.owner_employee
          OR NEW.fact->>'proposed_by_run' IS DISTINCT FROM p.proposed_by_run
          OR (NEW.fact->>'created_at')::timestamptz IS DISTINCT FROM p.created_at
          OR (NEW.fact->>'expires_at')::timestamptz IS DISTINCT FROM p.expires_at
          OR (NEW.fact->>'expires_at_limit')::timestamptz IS DISTINCT FROM p.expires_at_limit
          OR p.proposed_change->>'quote_id' IS DISTINCT FROM NEW.quote_id
          OR p.proposed_change->>'quote_version' IS DISTINCT FROM NEW.quote_version::text
          OR p.proposed_change->>'content_hash' IS DISTINCT FROM NEW.content_hash
          OR p.proposed_change->'policy'->>'policy_id' IS DISTINCT FROM q.content->'basis'->'policy'->>'policy_id'
          OR p.proposed_change->'policy'->>'content_hash' IS DISTINCT FROM q.content->'basis'->'policy'->>'content_hash'
        THEN RAISE EXCEPTION 'quote approval binding mismatch'; END IF;
        RETURN NEW;
      END; $$ LANGUAGE plpgsql""")
    op.execute(
        "CREATE TRIGGER trg_quote_approval_binding_contract BEFORE INSERT ON quotation_approval_bindings FOR EACH ROW EXECUTE FUNCTION guard_quote_approval_binding()"
    )
    op.execute("""CREATE FUNCTION guard_quote_approval_receipt() RETURNS trigger AS $$
      DECLARE q quotations%ROWTYPE; r workflow_runs%ROWTYPE; p approval_packages%ROWTYPE;
        d jsonb; b quotation_approval_bindings%ROWTYPE; n integer;
      BEGIN
        IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'immutable quote approval receipt'; END IF;
        SELECT * INTO q FROM quotations WHERE tenant_id=NEW.tenant_id AND quote_id=NEW.quote_id;
        SELECT * INTO r FROM workflow_runs WHERE tenant_id=NEW.tenant_id AND run_id=NEW.approval_run_id;
        IF q.state IS DISTINCT FROM 'approved' OR q.content_hash IS DISTINCT FROM NEW.content_hash
          OR q.version IS DISTINCT FROM NEW.quote_version OR r.workflow_type IS DISTINCT FROM 'quote_approval'
          OR r.workflow_version IS DISTINCT FROM 1 OR r.subject_ref IS DISTINCT FROM NEW.quote_id
          OR r.context->>'quote_version' IS DISTINCT FROM NEW.quote_version::text
          OR r.context->>'content_hash' IS DISTINCT FROM NEW.content_hash
          THEN RAISE EXCEPTION 'quote receipt parent mismatch'; END IF;
        SELECT count(*) INTO n FROM quotation_approval_bindings WHERE tenant_id=NEW.tenant_id AND quote_id=NEW.quote_id;
        IF n=0 OR n<>jsonb_array_length(NEW.decisions) OR n<>(SELECT count(DISTINCT value->>'approval_id') FROM jsonb_array_elements(NEW.decisions))
          THEN RAISE EXCEPTION 'quote receipt decisions mismatch'; END IF;
        FOR d IN SELECT value FROM jsonb_array_elements(NEW.decisions) LOOP
          SELECT * INTO b FROM quotation_approval_bindings WHERE tenant_id=NEW.tenant_id AND quote_id=NEW.quote_id
            AND approval_id=d->>'approval_id' AND approval_type=d->>'approval_type';
          SELECT * INTO p FROM approval_packages WHERE tenant_id=NEW.tenant_id AND approval_id=d->>'approval_id';
          IF b.approval_id IS NULL OR d->>'tenant_id' IS DISTINCT FROM NEW.tenant_id
            OR d->>'request_hash' IS DISTINCT FROM b.request_hash OR d->'payload' IS DISTINCT FROM b.fact->'payload'
            OR d->>'proposed_by_run' IS DISTINCT FROM NEW.approval_run_id OR d->>'decision' IS DISTINCT FROM 'approve'
            OR p.state IS DISTINCT FROM 'approved' OR d->>'decided_by' IS DISTINCT FROM p.decided_by
            OR (d->>'decided_at')::timestamptz IS DISTINCT FROM p.decided_at
            OR d->>'decision_note' IS DISTINCT FROM p.decision_note
            OR (d-ARRAY['decision','decided_by','decided_at','decision_note']) IS DISTINCT FROM
               (b.fact-ARRAY['state','applied_at','application_error_code','decision','decided_by','decided_at','decision_note'])
            OR (d->>'approval_type'='quote_send' AND d->>'decided_by' IS DISTINCT FROM NEW.quote_send_decider)
            THEN RAISE EXCEPTION 'quote receipt binding mismatch'; END IF;
        END LOOP;
        RETURN NEW;
      END; $$ LANGUAGE plpgsql""")
    op.execute(
        "CREATE TRIGGER trg_quote_approval_receipt BEFORE INSERT OR UPDATE OR DELETE ON quotation_approval_receipts FOR EACH ROW EXECUTE FUNCTION guard_quote_approval_receipt()"
    )


def downgrade() -> None:
    """新审批事实存在即拒绝，不通过删除历史强行降级。"""
    for query in (
        "SELECT EXISTS(SELECT 1 FROM approval_packages WHERE contract_namespace IS NOT NULL)",
        "SELECT EXISTS(SELECT 1 FROM quotation_approval_receipts)",
        "SELECT EXISTS(SELECT 1 FROM quotation_approval_bindings)",
    ):
        if op.get_bind().execute(sa.text(query)).scalar():
            raise RuntimeError("存在报价审批历史，拒绝降级")
    op.drop_table("quotation_approval_receipts")
    op.execute("DROP FUNCTION guard_quote_approval_receipt()")
    op.execute(
        "DROP TRIGGER trg_quote_approval_binding_contract ON quotation_approval_bindings"
    )
    op.execute("DROP FUNCTION guard_quote_approval_binding()")
    op.drop_constraint(
        "uq_quotation_approval_type", "quotation_approval_bindings", type_="unique"
    )
    for column in ("fact", "payload_hash", "request_hash"):
        op.drop_column("quotation_approval_bindings", column)
    _approval_guard(new_contract=False)
    op.drop_index("uq_approval_quote_change_set", table_name="approval_packages")
    op.drop_constraint("ck_approval_quote_contract", "approval_packages", type_="check")
    for column in ("expires_at_limit", "request_hash", "contract_namespace"):
        op.drop_column("approval_packages", column)
