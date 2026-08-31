"""成本来源补充被人工主选的 Product 引用。"""

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
    op.drop_constraint("ck_cost_sheets_sourcing_origin", "cost_sheets", type_="check")
    op.create_check_constraint(
        "ck_cost_sheets_sourcing_origin",
        "cost_sheets",
        "(source_sourcing_case_id IS NULL AND source_option_id IS NULL "
        "AND source_product_id IS NULL AND source_candidate_id IS NULL) OR "
        "(source_sourcing_case_id IS NOT NULL AND source_option_id IS NOT NULL "
        "AND source_product_id IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_cost_sheets_sourcing_product",
        "cost_sheets",
        "products",
        ["tenant_id", "source_product_id"],
        ["tenant_id", "product_id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_cost_sheets_sourcing_product", "cost_sheets", type_="foreignkey"
    )
    op.drop_constraint("ck_cost_sheets_sourcing_origin", "cost_sheets", type_="check")
    op.drop_column("cost_sheets", "source_product_id")
    op.create_check_constraint(
        "ck_cost_sheets_sourcing_origin",
        "cost_sheets",
        "(source_sourcing_case_id IS NULL AND source_option_id IS NULL "
        "AND source_candidate_id IS NULL) OR "
        "(source_sourcing_case_id IS NOT NULL AND source_option_id IS NOT NULL)",
    )
