"""Phase 1 承诺账本持久化。

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-22
"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "commitments",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("commitment_id", sa.String(40), nullable=False),
        sa.Column("commitment_type", sa.String(16), nullable=False),
        sa.Column("owner", sa.String(40), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "due_at_uncertain",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("source_message_id", sa.String(200), nullable=False),
        sa.Column("verbatim", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("account_id", sa.String(40), nullable=True),
        sa.Column("opportunity_id", sa.String(40), nullable=True),
        sa.Column("extracted_by", sa.String(128), nullable=True),
        sa.Column("confirmed_by", sa.String(40), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id", "commitment_id", name="pk_commitments"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "source_message_id",
            "action",
            name="uq_commitments_source_action",
        ),
        sa.CheckConstraint(
            "commitment_type IN ('employee','customer')",
            name="ck_commitments_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending','waiting_customer','fulfilled','overdue','cancelled')",
            name="ck_commitments_status",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(commitment_id) <> '' AND "
            "btrim(owner) <> '' AND btrim(action) <> '' AND "
            "btrim(source_message_id) <> '' AND btrim(verbatim) <> ''",
            name="ck_commitments_core_nonblank",
        ),
        sa.CheckConstraint(
            "(extracted_by IS NULL OR btrim(extracted_by) <> '') AND "
            "char_length(action) <= 4000 AND char_length(verbatim) <= 8000",
            name="ck_commitments_optional_nonblank",
        ),
        sa.CheckConstraint(
            "(confirmed_by IS NULL AND confirmed_at IS NULL) OR "
            "(confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",
            name="ck_commitments_confirmation_pair",
        ),
        sa.CheckConstraint(
            "(status = 'fulfilled' AND fulfilled_at IS NOT NULL) OR "
            "(status <> 'fulfilled' AND fulfilled_at IS NULL)",
            name="ck_commitments_fulfilled_pair",
        ),
    )
    op.create_index(
        "ix_commitments_tenant_owner_status_due",
        "commitments",
        ["tenant_id", "owner", "status", "due_at", "commitment_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_commitments_tenant_owner_status_due", table_name="commitments"
    )
    op.drop_table("commitments")
