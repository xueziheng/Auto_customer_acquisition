"""完整需求成本适用性与可恢复报价冻结。Revision 0043，前序0042。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def _text(name: str, length: int = 64) -> sa.Column:
    return sa.Column(name, sa.String(length), nullable=False)


def _fk(
    table: str, key: str, target: str, target_key: str | None = None, **kw: object
) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["tenant_id", key],
        [f"{target}.tenant_id", f"{target}.{target_key or key}"],
        name=f"fk_{table}_{key}",
        ondelete="RESTRICT",
        **kw,
    )


def upgrade() -> None:
    """新表只增，operation唯一允许首次完成；不修改0042或旧成本内容。"""
    op.create_index(
        "ix_costing_coverage_sheet_hash",
        "costing_coverage",
        ["tenant_id", "cost_sheet_id", "sheet_hash", "confirmed_at", "coverage_id"],
    )
    op.execute("""CREATE FUNCTION reject_quote_lock_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'immutable quote lock record'; END; $$ LANGUAGE plpgsql""")
    op.create_table(
        "cost_scope_confirmations",
        _text("tenant_id", 40),
        _text("confirmation_id", 40),
        _text("cost_sheet_id", 40),
        _text("opportunity_id", 40),
        _text("need_id", 40),
        _text("coverage_id"),
        _text("idempotency_key", 128),
        _text("request_hash"),
        _text("content_hash"),
        _text("sheet_hash"),
        _text("need_facts_hash"),
        _text("specification_hash"),
        _text("terms_hash"),
        _text("confirmed_by", 40),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "confirmation_id", name="pk_cost_scope_confirmations"
        ),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_cost_scope_confirmations_key"
        ),
        _fk("cost_scope_confirmations", "cost_sheet_id", "cost_sheets"),
        _fk("cost_scope_confirmations", "coverage_id", "costing_coverage"),
        _fk("cost_scope_confirmations", "need_id", "validated_needs"),
        _fk("cost_scope_confirmations", "confirmed_by", "employees", "employee_id"),
        _fk("cost_scope_confirmations", "opportunity_id", "opportunities"),
        sa.CheckConstraint(
            "jsonb_typeof(payload)='object'", name="ck_cost_scope_confirmations_json"
        ),
        sa.CheckConstraint(
            "isfinite(confirmed_at)", name="ck_cost_scope_confirmations_time"
        ),
        sa.CheckConstraint(
            " AND ".join(
                f"{name} ~ '^[0-9a-f]{{64}}$'"
                for name in (
                    "request_hash",
                    "content_hash",
                    "sheet_hash",
                    "need_facts_hash",
                    "specification_hash",
                    "terms_hash",
                )
            ),
            name="ck_cost_scope_confirmations_hash",
        ),
        sa.CheckConstraint(
            "idempotency_key=btrim(idempotency_key) AND length(idempotency_key)>0 "
            "AND idempotency_key !~ '[[:cntrl:]]'",
            name="ck_cost_scope_confirmations_key",
        ),
    )
    op.create_table(
        "costing_quote_bases",
        _text("tenant_id", 40),
        _text("basis_id", 40),
        _text("operation_id", 40),
        _text("cost_sheet_id", 40),
        _text("opportunity_id", 40),
        _text("scope_confirmation_id", 40),
        _text("request_hash"),
        _text("context_hash"),
        _text("sheet_hash"),
        _text("basis_hash"),
        _text("policy_id"),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "basis_id", name="pk_costing_quote_bases"),
        sa.UniqueConstraint(
            "tenant_id", "operation_id", name="uq_costing_quote_bases_operation"
        ),
        _fk(
            "costing_quote_bases",
            "scope_confirmation_id",
            "cost_scope_confirmations",
            "confirmation_id",
        ),
        _fk("costing_quote_bases", "cost_sheet_id", "cost_sheets"),
        _fk("costing_quote_bases", "policy_id", "costing_policies"),
        _fk("costing_quote_bases", "opportunity_id", "opportunities"),
        sa.CheckConstraint(
            "jsonb_typeof(payload)='object'", name="ck_costing_quote_bases_json"
        ),
        sa.CheckConstraint(
            " AND ".join(
                f"{name} ~ '^[0-9a-f]{{64}}$'"
                for name in ("request_hash", "context_hash", "sheet_hash", "basis_hash")
            ),
            name="ck_costing_quote_bases_hash",
        ),
        sa.CheckConstraint(
            "isfinite(valid_until) AND isfinite(frozen_at) AND valid_until>frozen_at",
            name="ck_costing_quote_bases_time",
        ),
    )
    op.create_table(
        "quote_creation_operations",
        _text("tenant_id", 40),
        _text("operation_id", 40),
        _text("idempotency_key", 128),
        _text("request_hash"),
        _text("cost_sheet_id", 40),
        _text("basis_id", 40),
        _text("state", 16),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("intent", postgresql.JSONB(), nullable=False),
        sa.Column("completion", postgresql.JSONB(), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id", "operation_id", name="pk_quote_creation_operations"
        ),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_quote_creation_operations_key"
        ),
        _fk("quote_creation_operations", "cost_sheet_id", "cost_sheets"),
        _fk(
            "quote_creation_operations",
            "basis_id",
            "costing_quote_bases",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$'", name="ck_quote_creation_operations_hash"
        ),
        sa.CheckConstraint(
            "isfinite(created_at) AND (completed_at IS NULL OR isfinite(completed_at))",
            name="ck_quote_creation_operations_time",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(intent)='object' AND "
            "(completion IS NULL OR jsonb_typeof(completion)='object')",
            name="ck_quote_creation_operations_json",
        ),
        sa.CheckConstraint(
            "(state='frozen' AND completion IS NULL AND completed_at IS NULL) OR "
            "(state='completed' AND completion IS NOT NULL AND completed_at IS NOT NULL AND completed_at>=created_at)",
            name="ck_quote_creation_operations_state",
        ),
        sa.CheckConstraint(
            "idempotency_key=btrim(idempotency_key) AND length(idempotency_key)>0 "
            "AND idempotency_key !~ '[[:cntrl:]]'",
            name="ck_quote_creation_operations_key",
        ),
    )
    op.create_foreign_key(
        "fk_costing_quote_bases_operation_id",
        "costing_quote_bases",
        "quote_creation_operations",
        ["tenant_id", "operation_id"],
        ["tenant_id", "operation_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.create_index(
        "uq_quote_creation_operations_pending",
        "quote_creation_operations",
        ["tenant_id", "cost_sheet_id"],
        unique=True,
        postgresql_where=sa.text("state='frozen'"),
    )
    op.create_index(
        "ix_quote_creation_operations_sheet",
        "quote_creation_operations",
        ["tenant_id", "cost_sheet_id"],
    )
    for name in ("cost_scope_confirmations", "costing_quote_bases"):
        op.execute(
            f"CREATE TRIGGER trg_{name}_immutable BEFORE UPDATE OR DELETE ON {name} "
            "FOR EACH ROW EXECUTE FUNCTION reject_quote_lock_mutation()"
        )
    op.execute("""CREATE FUNCTION guard_quote_creation_mutation() RETURNS trigger AS $$
        BEGIN
          IF TG_OP='DELETE' THEN RAISE EXCEPTION 'immutable quote creation'; END IF;
          IF TG_OP='INSERT' THEN
            IF NEW.state<>'frozen' THEN RAISE EXCEPTION 'creation must start frozen'; END IF;
            RETURN NEW;
          END IF;
          IF OLD.state<>'frozen' OR NEW.state<>'completed' OR
             (to_jsonb(NEW)-ARRAY['state','completion','completed_at']) IS DISTINCT FROM
             (to_jsonb(OLD)-ARRAY['state','completion','completed_at']) THEN
            RAISE EXCEPTION 'immutable quote creation binding';
          END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql""")
    op.execute(
        "CREATE TRIGGER trg_quote_creation_operations_guard BEFORE INSERT OR UPDATE OR DELETE "
        "ON quote_creation_operations FOR EACH ROW EXECUTE FUNCTION guard_quote_creation_mutation()"
    )


def downgrade() -> None:
    """有商业证据先要求授权归档，不把降级当成删除历史的许可。"""
    for table in (
        "cost_scope_confirmations",
        "costing_quote_bases",
        "quote_creation_operations",
    ):
        if (
            op.get_bind()
            .execute(sa.text(f"SELECT EXISTS(SELECT 1 FROM {table})"))
            .scalar()
        ):
            raise RuntimeError("报价冻结记录非空，降级前需要授权归档")
    op.drop_constraint(
        "fk_costing_quote_bases_operation_id", "costing_quote_bases", type_="foreignkey"
    )
    op.drop_index("ix_costing_coverage_sheet_hash", table_name="costing_coverage")
    for table in (
        "quote_creation_operations",
        "costing_quote_bases",
        "cost_scope_confirmations",
    ):
        op.drop_table(table)
    op.execute("DROP FUNCTION guard_quote_creation_mutation()")
    op.execute("DROP FUNCTION reject_quote_lock_mutation()")
