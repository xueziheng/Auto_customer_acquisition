"""Catalog scheduler durable reconciliation checkpoints.

Revision ID: 0058
Revises: 0057
Create Date: 2026-09-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_reconciliation_checkpoints",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("stream", sa.String(32), nullable=False),
        sa.Column("position_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("entity_id", sa.String(40), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "stream", name="pk_catalog_reconciliation_checkpoints"
        ),
        sa.CheckConstraint(
            "btrim(tenant_id)<>'' AND stream IN "
            "('pending_policies','awaiting_proposals','catalog_clusters')",
            name="ck_catalog_reconciliation_checkpoint_scope",
        ),
        sa.CheckConstraint(
            "version>=1 AND ((position_at IS NULL AND entity_id IS NULL) OR "
            "(position_at IS NOT NULL AND entity_id IS NOT NULL))",
            name="ck_catalog_reconciliation_checkpoint_position",
        ),
        sa.CheckConstraint(
            "entity_id IS NULL OR "
            "(stream='pending_policies' AND entity_id ~ '^cpv_') OR "
            "(stream='awaiting_proposals' AND entity_id ~ '^cpr_') OR "
            "(stream='catalog_clusters' AND entity_id ~ '^ncl_')",
            name="ck_catalog_reconciliation_checkpoint_entity",
        ),
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM catalog_reconciliation_checkpoints LIMIT 1)
          THEN RAISE EXCEPTION '0058 refuses destructive catalog checkpoint downgrade';
          END IF;
        END $$;
        """
    )
    op.drop_table("catalog_reconciliation_checkpoints")
