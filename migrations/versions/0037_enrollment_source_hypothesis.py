"""Enrollment 保留账户发现来源假设。

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "outreach_enrollments",
        sa.Column("source_hypothesis_id", sa.String(40), nullable=True),
    )
    op.create_foreign_key(
        "fk_outreach_enrollments_source_hypothesis",
        "outreach_enrollments",
        "need_hypotheses",
        ["tenant_id", "source_hypothesis_id"],
        ["tenant_id", "hypothesis_id"],
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_outreach_enrollments_tenant_source_hypothesis",
        "outreach_enrollments",
        ["tenant_id", "source_hypothesis_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_outreach_enrollments_tenant_source_hypothesis",
        table_name="outreach_enrollments",
    )
    op.drop_constraint(
        "fk_outreach_enrollments_source_hypothesis",
        "outreach_enrollments",
        type_="foreignkey",
    )
    op.drop_column("outreach_enrollments", "source_hypothesis_id")
