"""Phase 1 人工成本表与利润规则持久化。

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-23
"""

import sqlalchemy as sa
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cost_sheets",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("cost_sheet_id", sa.String(40), nullable=False),
        sa.Column("opportunity_id", sa.String(40), nullable=False),
        sa.Column("version_type", sa.String(16), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("base_currency", sa.CHAR(3), nullable=False),
        sa.Column("quote_currency", sa.CHAR(3), nullable=False),
        sa.Column("fx_snapshot_id", sa.String(40), nullable=True),
        sa.Column("created_by", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("risk_accepted_by", sa.String(40), nullable=True),
        sa.Column("risk_accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("risk_justification", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id", "cost_sheet_id", name="pk_cost_sheets"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "opportunity_id",
            "version_type",
            "version_number",
            name="uq_cost_sheets_version",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "opportunity_id"],
            ["opportunities.tenant_id", "opportunities.opportunity_id"],
            ondelete="RESTRICT",
            name="fk_cost_sheets_opportunity",
        ),
        sa.CheckConstraint(
            "version_type IN ('estimated','quoted','actual')",
            name="ck_cost_sheets_version_type",
        ),
        sa.CheckConstraint(
            "version_number > 0 AND quantity > 0",
            name="ck_cost_sheets_positive_dimensions",
        ),
        sa.CheckConstraint(
            "base_currency ~ '^[A-Z]{3}$' AND quote_currency ~ '^[A-Z]{3}$'",
            name="ck_cost_sheets_currencies",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(cost_sheet_id) <> '' AND "
            "btrim(opportunity_id) <> '' AND "
            "(created_by IS NULL OR btrim(created_by) <> '') AND "
            "(fx_snapshot_id IS NULL OR btrim(fx_snapshot_id) <> '')",
            name="ck_cost_sheets_core_nonblank",
        ),
        sa.CheckConstraint(
            "version_type <> 'quoted' OR fx_snapshot_id IS NOT NULL",
            name="ck_cost_sheets_quoted_fx_snapshot",
        ),
        sa.CheckConstraint(
            "locked_at IS NULL OR locked_at >= created_at",
            name="ck_cost_sheets_locked_at",
        ),
        sa.CheckConstraint(
            "(risk_accepted_by IS NULL AND risk_accepted_at IS NULL AND "
            "risk_justification IS NULL) OR "
            "(risk_accepted_by IS NOT NULL AND risk_accepted_at IS NOT NULL AND "
            "risk_justification IS NOT NULL AND "
            "btrim(risk_accepted_by) <> '' AND btrim(risk_justification) <> '')",
            name="ck_cost_sheets_risk_acceptance",
        ),
    )
    op.create_index(
        "ix_cost_sheets_tenant_opportunity_version",
        "cost_sheets",
        ["tenant_id", "opportunity_id", "version_type", "version_number"],
    )

    op.create_table(
        "cost_items",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("cost_sheet_id", sa.String(40), nullable=False),
        sa.Column("item_sequence", sa.Integer(), nullable=False),
        sa.Column("item_type", sa.String(40), nullable=False),
        sa.Column("amount", sa.Numeric(28, 12), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("price_basis", sa.String(16), nullable=False),
        sa.Column("is_per_unit", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("source_ref", sa.String(200), nullable=True),
        sa.Column("entered_by", sa.String(40), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "cost_sheet_id",
            "item_sequence",
            name="pk_cost_items",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "cost_sheet_id"],
            ["cost_sheets.tenant_id", "cost_sheets.cost_sheet_id"],
            ondelete="CASCADE",
            name="fk_cost_items_sheet",
        ),
        sa.CheckConstraint("item_sequence > 0", name="ck_cost_items_sequence"),
        sa.CheckConstraint("amount >= 0", name="ck_cost_items_amount"),
        sa.CheckConstraint(
            "item_type IN ("
            "'product_purchase','sample_fee','mold_fee','customization_fee',"
            "'logo_printing','packaging','quality_inspection','wastage',"
            "'domestic_freight','international_freight','insurance',"
            "'customs_clearance','duties_and_taxes','destination_freight',"
            "'warehousing','payment_fees','sales_commission',"
            "'customer_acquisition','contact_data_cost','ad_allocation',"
            "'agent_api_allocation','returns_reserve')",
            name="ck_cost_items_item_type",
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'", name="ck_cost_items_currency"
        ),
        sa.CheckConstraint(
            "price_basis IN ('indicative','quoted','actual')",
            name="ck_cost_items_price_basis",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(cost_sheet_id) <> '' AND "
            "btrim(item_type) <> '' AND "
            "(source_ref IS NULL OR btrim(source_ref) <> '') AND "
            "(entered_by IS NULL OR btrim(entered_by) <> '')",
            name="ck_cost_items_core_nonblank",
        ),
        sa.CheckConstraint(
            "entered_by IS NULL OR source_ref IS NOT NULL",
            name="ck_cost_items_confirmed_source",
        ),
    )

    op.create_table(
        "cost_sheet_fx_rates",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("cost_sheet_id", sa.String(40), nullable=False),
        sa.Column("base_currency", sa.CHAR(3), nullable=False),
        sa.Column("quote_currency", sa.CHAR(3), nullable=False),
        sa.Column("rate", sa.Numeric(28, 12), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(200), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "cost_sheet_id",
            "base_currency",
            name="pk_cost_sheet_fx_rates",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "cost_sheet_id"],
            ["cost_sheets.tenant_id", "cost_sheets.cost_sheet_id"],
            ondelete="CASCADE",
            name="fk_cost_sheet_fx_rates_sheet",
        ),
        sa.CheckConstraint("rate > 0", name="ck_cost_sheet_fx_rates_rate"),
        sa.CheckConstraint(
            "base_currency ~ '^[A-Z]{3}$' AND "
            "quote_currency ~ '^[A-Z]{3}$'",
            name="ck_cost_sheet_fx_rates_currencies",
        ),
        sa.CheckConstraint(
            "base_currency <> quote_currency OR rate = 1",
            name="ck_cost_sheet_fx_rates_identity",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(cost_sheet_id) <> '' AND "
            "btrim(source) <> ''",
            name="ck_cost_sheet_fx_rates_core_nonblank",
        ),
    )

    op.create_table(
        "margin_rules",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("margin_rule_id", sa.String(40), nullable=False),
        sa.Column("category", sa.String(200), nullable=True),
        sa.Column("minimum_margin_rate", sa.Numeric(18, 12), nullable=False),
        sa.Column("target_margin_rate", sa.Numeric(18, 12), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "margin_rule_id", name="pk_margin_rules"
        ),
        sa.CheckConstraint(
            "minimum_margin_rate >= 0 AND "
            "minimum_margin_rate <= target_margin_rate AND "
            "target_margin_rate < 1",
            name="ck_margin_rules_rates",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(margin_rule_id) <> '' AND "
            "(category IS NULL OR btrim(category) <> '')",
            name="ck_margin_rules_core_nonblank",
        ),
    )
    op.create_index(
        "ix_margin_rules_tenant_category_effective",
        "margin_rules",
        ["tenant_id", "category", "effective_from", "margin_rule_id"],
    )

    op.execute(
        "CREATE FUNCTION guard_locked_cost_sheet_mutation() RETURNS trigger "
        "AS $$ BEGIN "
        "IF OLD.locked_at IS NOT NULL THEN "
        "RAISE EXCEPTION 'locked cost sheet is immutable' USING ERRCODE='23514'; "
        "END IF; IF TG_OP = 'DELETE' THEN RETURN OLD; END IF; RETURN NEW; "
        "END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_cost_sheets_locked_immutable "
        "BEFORE UPDATE OR DELETE ON cost_sheets FOR EACH ROW "
        "EXECUTE FUNCTION guard_locked_cost_sheet_mutation()"
    )
    op.execute(
        "CREATE FUNCTION guard_locked_cost_sheet_child_mutation() RETURNS trigger "
        "AS $$ DECLARE sheet_locked_at timestamptz; BEGIN "
        "SELECT locked_at INTO sheet_locked_at FROM cost_sheets "
        "WHERE tenant_id = COALESCE(NEW.tenant_id, OLD.tenant_id) "
        "AND cost_sheet_id = COALESCE(NEW.cost_sheet_id, OLD.cost_sheet_id) "
        "FOR SHARE; "
        "IF sheet_locked_at IS NOT NULL THEN "
        "RAISE EXCEPTION 'locked cost sheet child is immutable' "
        "USING ERRCODE='23514'; END IF; "
        "IF TG_OP = 'DELETE' THEN RETURN OLD; END IF; RETURN NEW; "
        "END; $$ LANGUAGE plpgsql"
    )
    for table in ("cost_items", "cost_sheet_fx_rates"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_locked_immutable "
            f"BEFORE INSERT OR UPDATE OR DELETE ON {table} FOR EACH ROW "
            "EXECUTE FUNCTION guard_locked_cost_sheet_child_mutation()"
        )


def downgrade() -> None:
    for table in ("cost_sheet_fx_rates", "cost_items"):
        op.execute(f"DROP TRIGGER trg_{table}_locked_immutable ON {table}")
    op.execute("DROP FUNCTION guard_locked_cost_sheet_child_mutation()")
    op.execute("DROP TRIGGER trg_cost_sheets_locked_immutable ON cost_sheets")
    op.execute("DROP FUNCTION guard_locked_cost_sheet_mutation()")
    op.drop_index(
        "ix_margin_rules_tenant_category_effective", table_name="margin_rules"
    )
    op.drop_table("margin_rules")
    op.drop_table("cost_sheet_fx_rates")
    op.drop_table("cost_items")
    op.drop_index(
        "ix_cost_sheets_tenant_opportunity_version", table_name="cost_sheets"
    )
    op.drop_table("cost_sheets")
