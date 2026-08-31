"""成本来源完整绑定被选 Option/Product 与确定性数量档。"""

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cost_sheets", sa.Column("source_product_id", sa.String(40), nullable=True)
    )
    op.add_column(
        "cost_sheets",
        sa.Column("source_tier_minimum_quantity", sa.Integer(), nullable=True),
    )
    op.add_column(
        "cost_sheets", sa.Column("source_unit", sa.String(50), nullable=True)
    )
    op.create_unique_constraint(
        "uq_sourcing_supply_options_case_option_product",
        "sourcing_supply_options",
        ["tenant_id", "case_id", "option_id", "product_id"],
    )
    op.execute(
        sa.text(
            "UPDATE cost_sheets AS sheet SET source_product_id = option.product_id "
            "FROM sourcing_supply_options AS option "
            "WHERE sheet.tenant_id = option.tenant_id "
            "AND sheet.source_sourcing_case_id = option.case_id "
            "AND sheet.source_option_id = option.option_id "
            "AND sheet.source_sourcing_case_id IS NOT NULL "
            "AND sheet.source_product_id IS NULL"
        )
    )
    op.execute(
        sa.text(
            "UPDATE cost_sheets AS sheet "
            "SET source_tier_minimum_quantity = product.moq, "
            "source_unit = lower(regexp_replace(btrim(product.internal_cost_unit), "
            "'[[:space:]]+', ' ', 'g')) "
            "FROM sourcing_supply_options AS option "
            "JOIN products AS product "
            "ON product.tenant_id = option.tenant_id "
            "AND product.product_id = option.product_id "
            "WHERE sheet.tenant_id = option.tenant_id "
            "AND sheet.source_sourcing_case_id = option.case_id "
            "AND sheet.source_option_id = option.option_id "
            "AND option.source = 'existing_product' "
            "AND sheet.source_sourcing_case_id IS NOT NULL"
        )
    )
    op.execute(
        sa.text(
            "WITH ranked AS ("
            "SELECT sheet.tenant_id, sheet.cost_sheet_id, price.minimum_quantity, "
            "lower(regexp_replace(btrim(price.unit), '[[:space:]]+', ' ', 'g')) "
            "AS source_unit, row_number() OVER ("
            "PARTITION BY sheet.tenant_id, sheet.cost_sheet_id "
            "ORDER BY price.minimum_quantity DESC, price.artifact_id) AS rank "
            "FROM cost_sheets AS sheet "
            "JOIN sourcing_supply_options AS option "
            "ON option.tenant_id = sheet.tenant_id "
            "AND option.case_id = sheet.source_sourcing_case_id "
            "AND option.option_id = sheet.source_option_id "
            "JOIN product_candidate_price_refs AS price "
            "ON price.tenant_id = option.tenant_id "
            "AND price.product_id = option.product_id "
            "JOIN cost_items AS item ON item.tenant_id = sheet.tenant_id "
            "AND item.cost_sheet_id = sheet.cost_sheet_id "
            "AND item.item_type = 'product_purchase' "
            "AND item.is_per_unit IS TRUE "
            "AND item.source_ref = price.artifact_id "
            "AND item.amount = price.unit_amount "
            "AND item.currency = price.currency "
            "WHERE option.source = 'supplier_candidate' "
            "AND price.minimum_quantity <= sheet.quantity) "
            "UPDATE cost_sheets AS sheet "
            "SET source_tier_minimum_quantity = ranked.minimum_quantity, "
            "source_unit = ranked.source_unit FROM ranked "
            "WHERE ranked.rank = 1 AND sheet.tenant_id = ranked.tenant_id "
            "AND sheet.cost_sheet_id = ranked.cost_sheet_id"
        )
    )
    op.execute(
        sa.text(
            "DO $$ BEGIN IF EXISTS (SELECT 1 FROM cost_sheets "
            "WHERE source_sourcing_case_id IS NOT NULL AND "
            "(source_product_id IS NULL OR source_tier_minimum_quantity IS NULL "
            "OR source_unit IS NULL OR btrim(source_unit) = '')) THEN "
            "RAISE EXCEPTION '0051 cannot backfill complete sourcing cost origin'; "
            "END IF; END $$"
        )
    )
    op.drop_constraint(
        "fk_cost_sheets_sourcing_option", "cost_sheets", type_="foreignkey"
    )
    op.drop_constraint("ck_cost_sheets_sourcing_origin", "cost_sheets", type_="check")
    op.create_check_constraint(
        "ck_cost_sheets_sourcing_origin",
        "cost_sheets",
        "(source_sourcing_case_id IS NULL AND source_option_id IS NULL "
        "AND source_product_id IS NULL AND source_candidate_id IS NULL "
        "AND source_tier_minimum_quantity IS NULL AND source_unit IS NULL) OR "
        "(source_sourcing_case_id IS NOT NULL AND source_option_id IS NOT NULL "
        "AND source_product_id IS NOT NULL "
        "AND source_tier_minimum_quantity IS NOT NULL "
        "AND source_tier_minimum_quantity >= 1 "
        "AND source_tier_minimum_quantity <= quantity "
        "AND source_unit IS NOT NULL AND btrim(source_unit) <> '')",
    )
    op.create_foreign_key(
        "fk_cost_sheets_sourcing_option_product",
        "cost_sheets",
        "sourcing_supply_options",
        [
            "tenant_id",
            "source_sourcing_case_id",
            "source_option_id",
            "source_product_id",
        ],
        ["tenant_id", "case_id", "option_id", "product_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_cost_sheets_sourcing_option_product",
        "cost_sheets",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_cost_sheets_sourcing_option",
        "cost_sheets",
        "sourcing_supply_options",
        ["tenant_id", "source_sourcing_case_id", "source_option_id"],
        ["tenant_id", "case_id", "option_id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint("ck_cost_sheets_sourcing_origin", "cost_sheets", type_="check")
    op.drop_constraint(
        "uq_sourcing_supply_options_case_option_product",
        "sourcing_supply_options",
        type_="unique",
    )
    op.drop_column("cost_sheets", "source_unit")
    op.drop_column("cost_sheets", "source_tier_minimum_quantity")
    op.drop_column("cost_sheets", "source_product_id")
    op.create_check_constraint(
        "ck_cost_sheets_sourcing_origin",
        "cost_sheets",
        "(source_sourcing_case_id IS NULL AND source_option_id IS NULL "
        "AND source_candidate_id IS NULL) OR "
        "(source_sourcing_case_id IS NOT NULL AND source_option_id IS NOT NULL)",
    )
