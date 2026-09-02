"""Persist sourcing admission queue and immutable priority snapshots.

Revision ID: 0053
Revises: 0052
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sourcing_admissions",
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("admission_id", sa.String(length=40), nullable=False),
        sa.Column("case_id", sa.String(length=40), nullable=False),
        sa.Column("need_id", sa.String(length=40), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("current_snapshot_id", sa.String(length=40), nullable=True),
        sa.Column("claim_token", sa.String(length=200), nullable=True),
        sa.Column("claim_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("workflow_run_id", sa.String(length=40), nullable=True),
        sa.Column("blocked_reason", sa.String(length=40), nullable=True),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("admitted_by", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "admission_id", name="pk_sourcing_admissions"
        ),
        sa.UniqueConstraint("tenant_id", "case_id", name="uq_sourcing_admissions_case"),
        sa.UniqueConstraint("tenant_id", "need_id", name="uq_sourcing_admissions_need"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["sourcing_cases.tenant_id", "sourcing_cases.case_id"],
            name="fk_sourcing_admissions_case",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "need_id"],
            ["validated_needs.tenant_id", "validated_needs.need_id"],
            name="fk_sourcing_admissions_need",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('waiting','starting','admitted','blocked')",
            name="ck_sourcing_admissions_state",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(admission_id) <> '' "
            "AND btrim(case_id) <> '' AND btrim(need_id) <> '' "
            "AND (current_snapshot_id IS NULL OR btrim(current_snapshot_id) <> '') "
            "AND (claim_token IS NULL OR btrim(claim_token) <> '') "
            "AND (workflow_run_id IS NULL OR btrim(workflow_run_id) <> '') "
            "AND (admitted_by IS NULL OR btrim(admitted_by) <> '')",
            name="ck_sourcing_admissions_core",
        ),
        sa.CheckConstraint(
            "ready_at <= created_at AND updated_at >= created_at",
            name="ck_sourcing_admissions_times",
        ),
        sa.CheckConstraint(
            "(state = 'waiting' AND current_snapshot_id IS NOT NULL "
            "AND claim_token IS NULL AND claim_expires_at IS NULL "
            "AND workflow_run_id IS NULL AND admitted_at IS NULL "
            "AND admitted_by IS NULL AND blocked_reason IS NULL) OR "
            "(state = 'starting' AND current_snapshot_id IS NOT NULL "
            "AND claim_token IS NOT NULL AND claim_expires_at IS NOT NULL "
            "AND claim_expires_at > updated_at AND workflow_run_id IS NULL "
            "AND admitted_at IS NULL AND admitted_by IS NULL "
            "AND blocked_reason IS NULL) OR "
            "(state = 'admitted' AND current_snapshot_id IS NOT NULL "
            "AND claim_token IS NULL AND claim_expires_at IS NULL "
            "AND workflow_run_id IS NOT NULL AND admitted_at IS NOT NULL "
            "AND admitted_by IS NOT NULL AND admitted_at = updated_at "
            "AND blocked_reason IS NULL) OR "
            "(state = 'blocked' AND claim_token IS NULL "
            "AND claim_expires_at IS NULL AND workflow_run_id IS NULL "
            "AND admitted_at IS NULL AND admitted_by IS NULL "
            "AND blocked_reason IN ('priority_facts_invalid','case_state_mismatch') "
            "AND (current_snapshot_id IS NOT NULL "
            "OR blocked_reason = 'priority_facts_invalid'))",
            name="ck_sourcing_admissions_state_fields",
        ),
    )
    op.create_index(
        "ix_sourcing_admissions_queue",
        "sourcing_admissions",
        ["tenant_id", "state", "current_snapshot_id"],
    )

    op.create_table(
        "sourcing_priority_snapshots",
        sa.Column("tenant_id", sa.String(length=40), nullable=False),
        sa.Column("snapshot_id", sa.String(length=40), nullable=False),
        sa.Column("admission_id", sa.String(length=40), nullable=False),
        sa.Column("case_id", sa.String(length=40), nullable=False),
        sa.Column("need_id", sa.String(length=40), nullable=False),
        sa.Column("cluster_id", sa.String(length=40), nullable=True),
        sa.Column("cluster_member_count", sa.Integer(), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ranking_version", sa.String(length=64), nullable=False),
        sa.Column("facts_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("facts_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "snapshot_id", name="pk_sourcing_priority_snapshots"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "admission_id",
            "snapshot_id",
            name="uq_sourcing_priority_snapshots_admission_snapshot",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "admission_id",
            "facts_hash",
            name="uq_sourcing_priority_snapshots_facts",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "admission_id"],
            ["sourcing_admissions.tenant_id", "sourcing_admissions.admission_id"],
            name="fk_sourcing_priority_snapshots_admission",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["sourcing_cases.tenant_id", "sourcing_cases.case_id"],
            name="fk_sourcing_priority_snapshots_case",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "need_id"],
            ["validated_needs.tenant_id", "validated_needs.need_id"],
            name="fk_sourcing_priority_snapshots_need",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(snapshot_id) <> '' "
            "AND btrim(admission_id) <> '' AND btrim(case_id) <> '' "
            "AND btrim(need_id) <> '' "
            "AND (cluster_id IS NULL OR btrim(cluster_id) <> '')",
            name="ck_sourcing_priority_snapshots_core",
        ),
        sa.CheckConstraint(
            "cluster_member_count >= 1 "
            "AND (cluster_id IS NOT NULL OR cluster_member_count = 1)",
            name="ck_sourcing_priority_snapshots_cluster",
        ),
        sa.CheckConstraint(
            "ranking_version = 'need-cluster-admission-v1'",
            name="ck_sourcing_priority_snapshots_version",
        ),
        sa.CheckConstraint(
            "facts_hash ~ '^[0-9a-f]{64}$'",
            name="ck_sourcing_priority_snapshots_hash",
        ),
        sa.CheckConstraint(
            "ready_at <= created_at AND facts_observed_at <= created_at",
            name="ck_sourcing_priority_snapshots_times",
        ),
    )
    op.create_index(
        "ix_sourcing_priority_snapshots_order",
        "sourcing_priority_snapshots",
        [
            "tenant_id",
            sa.text("cluster_member_count DESC"),
            "ready_at",
            "need_id",
            "snapshot_id",
        ],
    )
    op.create_foreign_key(
        "fk_sourcing_admissions_current_snapshot",
        "sourcing_admissions",
        "sourcing_priority_snapshots",
        ["tenant_id", "admission_id", "current_snapshot_id"],
        ["tenant_id", "admission_id", "snapshot_id"],
        ondelete="RESTRICT",
        deferrable=True,
        initially="DEFERRED",
    )
    op.execute(
        """
        CREATE FUNCTION reject_sourcing_priority_snapshot_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'sourcing priority snapshots are immutable';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_sourcing_priority_snapshots_immutable
        BEFORE UPDATE OR DELETE ON sourcing_priority_snapshots
        FOR EACH ROW EXECUTE FUNCTION reject_sourcing_priority_snapshot_mutation();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM sourcing_admissions LIMIT 1)
             OR EXISTS (SELECT 1 FROM sourcing_priority_snapshots LIMIT 1) THEN
            RAISE EXCEPTION '0053 refuses to drop sourcing admission evidence';
          END IF;
        END $$;
        """
    )
    op.execute(
        "DROP TRIGGER trg_sourcing_priority_snapshots_immutable "
        "ON sourcing_priority_snapshots"
    )
    op.execute("DROP FUNCTION reject_sourcing_priority_snapshot_mutation()")
    op.drop_constraint(
        "fk_sourcing_admissions_current_snapshot",
        "sourcing_admissions",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_sourcing_priority_snapshots_order",
        table_name="sourcing_priority_snapshots",
    )
    op.drop_table("sourcing_priority_snapshots")
    op.drop_index("ix_sourcing_admissions_queue", table_name="sourcing_admissions")
    op.drop_table("sourcing_admissions")
