"""成本表绑定唯一 Sourcing Case 与被人工选择的供给来源。"""

import sqlalchemy as sa
from alembic import op

revision = "0049"
down_revision = "0048"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cost_sheets",
        sa.Column("source_sourcing_case_id", sa.String(40), nullable=True),
    )
    op.add_column(
        "cost_sheets", sa.Column("source_option_id", sa.String(40), nullable=True)
    )
    op.add_column(
        "cost_sheets", sa.Column("source_candidate_id", sa.String(40), nullable=True)
    )
    op.create_check_constraint(
        "ck_cost_sheets_sourcing_origin",
        "cost_sheets",
        "(source_sourcing_case_id IS NULL AND source_option_id IS NULL AND source_candidate_id IS NULL) OR "
        "(source_sourcing_case_id IS NOT NULL AND source_option_id IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_cost_sheets_sourcing_case",
        "cost_sheets",
        "sourcing_cases",
        ["tenant_id", "source_sourcing_case_id"],
        ["tenant_id", "case_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_cost_sheets_sourcing_option",
        "cost_sheets",
        "sourcing_supply_options",
        ["tenant_id", "source_sourcing_case_id", "source_option_id"],
        ["tenant_id", "case_id", "option_id"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_cost_sheets_sourcing_candidate_path",
        "cost_sheets",
        "sourcing_supply_options",
        [
            "tenant_id",
            "source_sourcing_case_id",
            "source_option_id",
            "source_candidate_id",
        ],
        ["tenant_id", "case_id", "option_id", "supplier_candidate_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "uq_cost_sheets_sourcing_case",
        "cost_sheets",
        ["tenant_id", "source_sourcing_case_id"],
        unique=True,
        postgresql_where=sa.text("source_sourcing_case_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_cost_sheets_sourcing_case", table_name="cost_sheets")
    op.drop_constraint(
        "fk_cost_sheets_sourcing_candidate_path", "cost_sheets", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_cost_sheets_sourcing_option", "cost_sheets", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_cost_sheets_sourcing_case", "cost_sheets", type_="foreignkey"
    )
    op.drop_constraint("ck_cost_sheets_sourcing_origin", "cost_sheets", type_="check")
    op.drop_column("cost_sheets", "source_candidate_id")
    op.drop_column("cost_sheets", "source_option_id")
    op.drop_column("cost_sheets", "source_sourcing_case_id")
