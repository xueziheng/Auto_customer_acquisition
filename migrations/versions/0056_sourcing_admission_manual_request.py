"""Persist immutable manual admission request identity outside the short lease.

Revision ID: 0056
Revises: 0055
Create Date: 2026-09-03
"""

import sqlalchemy as sa
from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None

_FUNCTION = "enforce_sourcing_admission_manual_request"
_TRIGGER = "trg_sourcing_admissions_manual_request"

_CORE = (
    "btrim(tenant_id) <> '' AND btrim(admission_id) <> '' "
    "AND btrim(case_id) <> '' AND btrim(need_id) <> '' "
    "AND (current_snapshot_id IS NULL OR btrim(current_snapshot_id) <> '') "
    "AND (claim_token IS NULL OR btrim(claim_token) <> '') "
    "AND (workflow_run_id IS NULL OR btrim(workflow_run_id) <> '') "
    "AND (admission_requested_by IS NULL OR btrim(admission_requested_by) <> '') "
    "AND (manual_request_id IS NULL OR "
    "(btrim(manual_request_id) <> '' AND admission_requested_by IS NOT NULL)) "
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
    "AND admission_requested_by IS NULL AND manual_request_id IS NULL "
    "AND blocked_reason IS NULL) OR "
    "(state = 'blocked' AND claim_token IS NULL "
    "AND claim_expires_at IS NULL AND workflow_run_id IS NULL "
    "AND admitted_at IS NULL AND admitted_by IS NULL "
    "AND admission_requested_by IS NULL AND manual_request_id IS NULL "
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
    "AND (admission_requested_by IS NULL OR btrim(admission_requested_by) <> '') "
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
        sa.Column("manual_request_id", sa.String(length=200), nullable=True),
    )
    _replace_checks(core=_CORE, state_fields=_STATE_FIELDS)
    op.execute(
        f"""
        CREATE FUNCTION {_FUNCTION}() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.admission_requested_by IS NOT NULL
                   AND NEW.manual_request_id IS NULL THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'sourcing admission manual request rejected',
                        CONSTRAINT = 'ck_sourcing_admissions_manual_request';
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.manual_request_id IS NOT NULL THEN
                IF NEW.manual_request_id IS DISTINCT FROM OLD.manual_request_id
                   OR NEW.admission_requested_by IS DISTINCT FROM
                      OLD.admission_requested_by THEN
                    IF NOT (
                        NEW.manual_request_id IS NULL
                        AND NEW.admission_requested_by IS NULL
                        AND NEW.state IN ('admitted', 'blocked')
                    ) THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '23514',
                            MESSAGE = 'sourcing admission manual request rejected',
                            CONSTRAINT = 'ck_sourcing_admissions_manual_request';
                    END IF;
                END IF;
                RETURN NEW;
            END IF;

            IF OLD.admission_requested_by IS NOT NULL THEN
                IF NEW.manual_request_id IS NOT NULL
                   OR NEW.admission_requested_by IS DISTINCT FROM
                      OLD.admission_requested_by THEN
                    IF NOT (
                        NEW.manual_request_id IS NULL
                        AND NEW.admission_requested_by IS NULL
                        AND NEW.state IN ('admitted', 'blocked')
                    ) THEN
                        RAISE EXCEPTION USING
                            ERRCODE = '23514',
                            MESSAGE = 'sourcing admission manual request rejected',
                            CONSTRAINT = 'ck_sourcing_admissions_manual_request';
                    END IF;
                END IF;
                RETURN NEW;
            END IF;

            IF NEW.admission_requested_by IS NOT NULL
               AND NEW.manual_request_id IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'sourcing admission manual request rejected',
                    CONSTRAINT = 'ck_sourcing_admissions_manual_request';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_TRIGGER}
        BEFORE INSERT OR UPDATE ON sourcing_admissions
        FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM sourcing_admissions
            WHERE manual_request_id IS NOT NULL
            LIMIT 1
          ) THEN
            RAISE EXCEPTION '0056 refuses to drop manual admission request identity';
          END IF;
        END $$;
        """
    )
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON sourcing_admissions")
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION}()")
    op.drop_constraint(
        "ck_sourcing_admissions_core", "sourcing_admissions", type_="check"
    )
    op.drop_constraint(
        "ck_sourcing_admissions_state_fields",
        "sourcing_admissions",
        type_="check",
    )
    op.drop_column("sourcing_admissions", "manual_request_id")
    op.create_check_constraint(
        "ck_sourcing_admissions_core", "sourcing_admissions", _OLD_CORE
    )
    op.create_check_constraint(
        "ck_sourcing_admissions_state_fields",
        "sourcing_admissions",
        _OLD_STATE_FIELDS,
    )
