"""潜客企业名称与国家保存字段级来源。

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "prospect_accounts",
        sa.Column(
            "field_provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_prospect_accounts_field_provenance_jsonb",
        "prospect_accounts",
        "jsonb_typeof(field_provenance) = 'object'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_prospect_accounts_field_provenance_jsonb",
        "prospect_accounts",
        type_="check",
    )
    op.drop_column("prospect_accounts", "field_provenance")
