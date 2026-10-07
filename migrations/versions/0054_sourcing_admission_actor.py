"""Persist trusted manual sourcing admission actor across lease recovery.

Revision ID: 0054
Revises: 0053
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None

_CORE = (
    "btrim(tenant_id) <> '' AND btrim(admission_id) <> '' "
    "AND btrim(case_id) <> '' AND btrim(need_id) <> '' "
    "AND (current_snapshot_id IS NULL OR btrim(current_snapshot_id) <> '') "
    "AND (claim_token IS NULL OR btrim(claim_token) <> '') "
    "AND (workflow_run_id IS NULL OR btrim(workflow_run_id) <> '') "
    "AND (admission_requested_by IS NULL OR btrim(admission_requested_by) <> '') "
    "AND (admitted_by IS NULL OR btrim(admitted_by) <> '')"
)

_STATE_FIELDS = (
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
    "AND admission_requested_by IS NULL AND blocked_reason IS NULL) OR "
    "(state = 'blocked' AND claim_token IS NULL "
    "AND claim_expires_at IS NULL AND workflow_run_id IS NULL "
    "AND admitted_at IS NULL AND admitted_by IS NULL "
    "AND admission_requested_by IS NULL "
    "AND blocked_reason IS NOT NULL "
    "AND blocked_reason IN ('priority_facts_invalid','case_state_mismatch') "
    "AND (current_snapshot_id IS NOT NULL "
    "OR blocked_reason = 'priority_facts_invalid'))"
)

_OLD_CORE = (
    "btrim(tenant_id) <> '' AND btrim(admission_id) <> '' "
    "AND btrim(case_id) <> '' AND btrim(need_id) <> '' "
    "AND (current_snapshot_id IS NULL OR btrim(current_snapshot_id) <> '') "
    "AND (claim_token IS NULL OR btrim(claim_token) <> '') "
    "AND (workflow_run_id IS NULL OR btrim(workflow_run_id) <> '') "
    "AND (admitted_by IS NULL OR btrim(admitted_by) <> '')"
)

_OLD_STATE_FIELDS = (
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
    "AND blocked_reason IS NOT NULL "
    "AND blocked_reason IN ('priority_facts_invalid','case_state_mismatch') "
    "AND (current_snapshot_id IS NOT NULL "
    "OR blocked_reason = 'priority_facts_invalid'))"
)


def _replace_checks(*, core: str, state_fields: str) -> None:
    op.drop_constraint(
        "ck_sourcing_admissions_core", "sourcing_admissions", type_="check"
    )
    op.drop_constraint(
        "ck_sourcing_admissions_state_fields",
        "sourcing_admissions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_sourcing_admissions_core", "sourcing_admissions", core
    )
    op.create_check_constraint(
        "ck_sourcing_admissions_state_fields", "sourcing_admissions", state_fields
    )


def upgrade() -> None:
    op.add_column(
        "sourcing_admissions",
        sa.Column("admission_requested_by", sa.String(length=200), nullable=True),
    )
    _replace_checks(core=_CORE, state_fields=_STATE_FIELDS)


def downgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM sourcing_admissions
            WHERE admission_requested_by IS NOT NULL
            LIMIT 1
          ) THEN
            RAISE EXCEPTION '0054 refuses to drop manual admission actor intent';
          END IF;
        END $$;
        """
    )
    op.drop_constraint(
        "ck_sourcing_admissions_core", "sourcing_admissions", type_="check"
    )
    op.drop_constraint(
        "ck_sourcing_admissions_state_fields",
        "sourcing_admissions",
        type_="check",
    )
    op.drop_column("sourcing_admissions", "admission_requested_by")
    op.create_check_constraint(
        "ck_sourcing_admissions_core", "sourcing_admissions", _OLD_CORE
    )
    op.create_check_constraint(
        "ck_sourcing_admissions_state_fields",
        "sourcing_admissions",
        _OLD_STATE_FIELDS,
    )
