"""联系人验证观察时间与成本说明。

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-21
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "contact_points",
        sa.Column(
            "verification_checked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "contact_points",
        sa.Column("verification_cost_note", sa.String(200), nullable=True),
    )
    op.drop_constraint(
        "ck_contact_points_optional_nonblank", "contact_points", type_="check"
    )
    op.create_check_constraint(
        "ck_contact_points_optional_nonblank",
        "contact_points",
        "(verification_provider IS NULL OR btrim(verification_provider) <> '') "
        "AND (verification_cost_note IS NULL OR "
        "btrim(verification_cost_note) <> '') "
        "AND (enrichment_cost_note IS NULL OR "
        "btrim(enrichment_cost_note) <> '')",
    )
    op.drop_constraint(
        "ck_contact_points_provider_state", "contact_points", type_="check"
    )
    op.create_check_constraint(
        "ck_contact_points_verification_observation",
        "contact_points",
        "(verification_checked_at IS NULL AND verification_cost_note IS NULL AND "
        "((verification_status = 'unverified' AND verification_provider IS NULL) OR "
        "(verification_status <> 'unverified' AND verification_provider IS NOT NULL))) "
        "OR (verification_checked_at IS NOT NULL AND "
        "verification_provider IS NOT NULL AND verification_cost_note IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_contact_points_verification_observation",
        "contact_points",
        type_="check",
    )
    op.execute(
        "UPDATE contact_points SET verification_provider = NULL "
        "WHERE verification_status = 'unverified'"
    )
    op.drop_constraint(
        "ck_contact_points_optional_nonblank", "contact_points", type_="check"
    )
    op.drop_column("contact_points", "verification_cost_note")
    op.drop_column("contact_points", "verification_checked_at")
    op.create_check_constraint(
        "ck_contact_points_provider_state",
        "contact_points",
        "(verification_status = 'unverified') = "
        "(verification_provider IS NULL)",
    )
    op.create_check_constraint(
        "ck_contact_points_optional_nonblank",
        "contact_points",
        "(verification_provider IS NULL OR btrim(verification_provider) <> '') "
        "AND (enrichment_cost_note IS NULL OR "
        "btrim(enrichment_cost_note) <> '')",
    )
