"""Guard Sourcing Case V2 workflow start with persisted admission state.

Revision ID: 0055
Revises: 0054
Create Date: 2026-09-03
"""

from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None

_FUNCTION = "enforce_sourcing_v2_workflow_admission"
_TRIGGER = "trg_workflow_runs_sourcing_v2_admission"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE FUNCTION {_FUNCTION}() RETURNS trigger AS $$
        DECLARE
            existing_type text;
            existing_version integer;
            existing_subject text;
        BEGIN
            IF NEW.workflow_type <> 'sourcing_case' OR NEW.workflow_version <> 2 THEN
                RETURN NEW;
            END IF;

            SELECT workflow_type, workflow_version, subject_ref
            INTO existing_type, existing_version, existing_subject
            FROM workflow_runs
            WHERE tenant_id = NEW.tenant_id
              AND idempotency_key = NEW.idempotency_key;

            IF FOUND THEN
                IF existing_type <> NEW.workflow_type
                   OR existing_version <> NEW.workflow_version
                   OR existing_subject <> NEW.subject_ref THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23514',
                        MESSAGE = 'sourcing_case v2 workflow start rejected',
                        CONSTRAINT = 'ck_workflow_runs_sourcing_v2_admission';
                END IF;
            END IF;

            IF EXISTS (
                SELECT 1
                FROM sourcing_admissions
                WHERE tenant_id = NEW.tenant_id
                  AND case_id = NEW.subject_ref
                  AND state = 'starting'
                  AND workflow_run_id IS NULL
            ) THEN
                RETURN NEW;
            END IF;

            RAISE EXCEPTION USING
                ERRCODE = '23514',
                MESSAGE = 'sourcing_case v2 workflow start rejected',
                CONSTRAINT = 'ck_workflow_runs_sourcing_v2_admission';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER {_TRIGGER}
        BEFORE INSERT ON workflow_runs
        FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}();
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON workflow_runs")
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION}()")
