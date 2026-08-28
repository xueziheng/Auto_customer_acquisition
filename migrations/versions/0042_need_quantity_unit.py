"""客户数量单位事实与不可变人工确认；旧Need不回填。

Revision ID: 0042
Revises: 0041
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """增量增加三单位列与确认表，不改变旧数量和完整度。"""
    op.create_table(
        "need_unit_confirmations",
        *(
            sa.Column(n, sa.String(40), nullable=False)
            for n in (
                "tenant_id",
                "confirmation_id",
                "need_id",
                "artifact_id",
                "source_message_id",
                "confirmed_by",
            )
        ),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("quantity_fact_hash", sa.String(64), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "confirmation_id", name="pk_need_unit_confirmations"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "need_id",
            "confirmation_id",
            name="uq_need_unit_confirmations_need_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "need_id",
            "idempotency_key",
            name="uq_need_unit_confirmations_key",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "need_id"],
            ["validated_needs.tenant_id", "validated_needs.need_id"],
            name="fk_need_unit_confirmations_need",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "artifact_id"],
            ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"],
            name="fk_need_unit_confirmations_artifact",
        ),
        sa.CheckConstraint(
            "request_hash ~ '^[0-9a-f]{64}$' AND quantity_fact_hash ~ '^[0-9a-f]{64}$'",
            name="ck_need_unit_confirmations_hash",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_need_unit_confirmations_payload",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(confirmation_id) <> '' AND "
            "btrim(need_id) <> '' AND btrim(artifact_id) <> '' AND btrim(source_message_id) <> '' "
            "AND btrim(confirmed_by) <> '' AND btrim(idempotency_key) <> ''",
            name="ck_need_unit_confirmations_nonblank",
        ),
    )
    op.add_column(
        "validated_needs",
        sa.Column("unit", postgresql.JSONB(none_as_null=True), nullable=True),
    )
    op.add_column(
        "validated_needs",
        sa.Column("unit_quantity_fact_hash", sa.String(64), nullable=True),
    )
    op.add_column(
        "validated_needs",
        sa.Column("unit_confirmation_id", sa.String(40), nullable=True),
    )
    op.create_check_constraint(
        "ck_validated_needs_unit_binding",
        "validated_needs",
        "(unit IS NULL AND unit_quantity_fact_hash IS NULL AND unit_confirmation_id IS NULL) OR "
        "(unit IS NOT NULL AND unit_quantity_fact_hash IS NOT NULL AND unit_confirmation_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_validated_needs_unit_jsonb",
        "validated_needs",
        "unit IS NULL OR jsonb_typeof(unit) = 'object'",
    )
    op.create_check_constraint(
        "ck_validated_needs_unit_hash",
        "validated_needs",
        "unit_quantity_fact_hash IS NULL OR unit_quantity_fact_hash ~ '^[0-9a-f]{64}$'",
    )
    op.create_foreign_key(
        "fk_validated_needs_unit_confirmation",
        "validated_needs",
        "need_unit_confirmations",
        ["tenant_id", "need_id", "unit_confirmation_id"],
        ["tenant_id", "need_id", "confirmation_id"],
    )
    op.execute("""
        CREATE FUNCTION reject_need_unit_confirmation_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'immutable need unit confirmation'; END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_need_unit_confirmations_immutable
        BEFORE UPDATE OR DELETE ON need_unit_confirmations
        FOR EACH ROW EXECUTE FUNCTION reject_need_unit_confirmation_mutation()
    """)
    op.execute("""
        CREATE FUNCTION check_need_unit_binding() RETURNS trigger AS $$
        DECLARE receipt need_unit_confirmations%ROWTYPE;
        BEGIN
            IF TG_OP = 'UPDATE' AND (NEW.unit, NEW.unit_quantity_fact_hash, NEW.unit_confirmation_id)
                IS NOT DISTINCT FROM (OLD.unit, OLD.unit_quantity_fact_hash, OLD.unit_confirmation_id)
            THEN RETURN NEW; END IF;
            IF NEW.unit IS NULL AND NEW.unit_quantity_fact_hash IS NULL AND NEW.unit_confirmation_id IS NULL
            THEN
                IF TG_OP = 'INSERT' THEN RETURN NEW; END IF;
                IF OLD.unit IS NULL THEN RETURN NEW; END IF;
                RAISE EXCEPTION 'need unit receipt cannot be cleared';
            END IF;
            SELECT * INTO receipt FROM need_unit_confirmations
                WHERE tenant_id = NEW.tenant_id AND need_id = NEW.need_id
                    AND confirmation_id = NEW.unit_confirmation_id;
            IF NOT FOUND OR NEW.unit IS DISTINCT FROM receipt.payload->'unit'
                OR NEW.unit_quantity_fact_hash IS DISTINCT FROM receipt.quantity_fact_hash
                OR receipt.payload->>'quantity_fact_hash' IS DISTINCT FROM receipt.quantity_fact_hash
                OR receipt.payload->>'tenant_id' IS DISTINCT FROM NEW.tenant_id
                OR receipt.payload->>'need_id' IS DISTINCT FROM NEW.need_id
                OR receipt.payload->>'confirmation_id' IS DISTINCT FROM NEW.unit_confirmation_id
            THEN RAISE EXCEPTION 'need unit receipt mismatch'; END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_validated_needs_unit_binding BEFORE INSERT OR UPDATE ON validated_needs
        FOR EACH ROW EXECUTE FUNCTION check_need_unit_binding()
    """)


def downgrade() -> None:
    """有业务确认时拒绝降级；必须先授权归档处理，不静默丢失证据。"""
    op.execute("""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM need_unit_confirmations) THEN
                RAISE EXCEPTION '单位确认已有业务证据，降级前必须取得授权并归档处理';
            END IF;
        END $$
    """)
    op.drop_constraint(
        "fk_validated_needs_unit_confirmation", "validated_needs", type_="foreignkey"
    )
    op.execute("DROP TRIGGER trg_validated_needs_unit_binding ON validated_needs")
    for name in (
        "ck_validated_needs_unit_binding",
        "ck_validated_needs_unit_jsonb",
        "ck_validated_needs_unit_hash",
    ):
        op.drop_constraint(name, "validated_needs", type_="check")
    for name in ("unit", "unit_quantity_fact_hash", "unit_confirmation_id"):
        op.drop_column("validated_needs", name)
    op.execute("DROP FUNCTION check_need_unit_binding()")
    op.drop_table("need_unit_confirmations")
    op.execute("DROP FUNCTION reject_need_unit_confirmation_mutation()")
