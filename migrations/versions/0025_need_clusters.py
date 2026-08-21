"""Phase 1 需求簇与成员关系。

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "need_clusters",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("cluster_id", sa.String(40), nullable=False),
        sa.Column("category", sa.String(200), nullable=False),
        sa.Column("keywords", postgresql.JSONB(), nullable=False),
        sa.Column("countries", postgresql.JSONB(), nullable=False),
        sa.Column("total_potential_quantity", sa.Integer(), nullable=True),
        sa.Column("recurring_demand", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "cluster_id", name="pk_need_clusters"),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(cluster_id) <> '' AND "
            "btrim(category) <> ''",
            name="ck_need_clusters_core_nonblank",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(keywords) = 'array' AND "
            "jsonb_typeof(countries) = 'array'",
            name="ck_need_clusters_arrays_jsonb",
        ),
        sa.CheckConstraint(
            "total_potential_quantity IS NULL OR total_potential_quantity >= 0",
            name="ck_need_clusters_quantity_nonnegative",
        ),
    )
    op.create_index(
        "ix_need_clusters_tenant_category",
        "need_clusters",
        ["tenant_id", "category", "created_at", "cluster_id"],
    )
    op.add_column(
        "validated_needs",
        sa.Column("cluster_id", sa.String(40), nullable=True),
    )
    op.create_foreign_key(
        "fk_validated_needs_cluster",
        "validated_needs",
        "need_clusters",
        ["tenant_id", "cluster_id"],
        ["tenant_id", "cluster_id"],
    )
    op.create_table(
        "need_cluster_members",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("cluster_id", sa.String(40), nullable=False),
        sa.Column("need_id", sa.String(40), nullable=False),
        sa.Column("assigned_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "cluster_id",
            "need_id",
            name="pk_need_cluster_members",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "cluster_id"],
            ["need_clusters.tenant_id", "need_clusters.cluster_id"],
            ondelete="CASCADE",
            name="fk_need_cluster_members_cluster",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "need_id"],
            ["validated_needs.tenant_id", "validated_needs.need_id"],
            ondelete="CASCADE",
            name="fk_need_cluster_members_need",
        ),
    )
    op.create_index(
        "uq_need_cluster_members_need",
        "need_cluster_members",
        ["tenant_id", "need_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_need_cluster_members_need",
        table_name="need_cluster_members",
    )
    op.drop_table("need_cluster_members")
    op.drop_constraint(
        "fk_validated_needs_cluster",
        "validated_needs",
        type_="foreignkey",
    )
    op.drop_column("validated_needs", "cluster_id")
    op.drop_index(
        "ix_need_clusters_tenant_category",
        table_name="need_clusters",
    )
    op.drop_table("need_clusters")
