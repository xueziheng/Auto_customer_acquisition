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
_SUBJECT_INDEX = "uq_workflow_runs_sourcing_v2_subject"


def upgrade() -> None:
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (
                SELECT 1 FROM workflow_runs
                WHERE workflow_type = 'sourcing_case' AND workflow_version = 2
                GROUP BY tenant_id, subject_ref HAVING count(*) > 1
            ) THEN
                RAISE EXCEPTION '0055 refuses duplicate sourcing_case v2 subjects';
            END IF;
        END $$;
        """
    )
    op.create_index(
        _SUBJECT_INDEX,
        "workflow_runs",
        ["tenant_id", "workflow_type", "subject_ref"],
        unique=True,
        postgresql_where="workflow_type = 'sourcing_case' AND workflow_version = 2",
    )
    op.execute(
        f"""
        CREATE FUNCTION {_FUNCTION}() RETURNS trigger AS $$
        DECLARE
            existing_type text;
            existing_version integer;
            existing_subject text;
            admission_need_id text;
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
                IF existing_type = NEW.workflow_type
                   AND existing_version = NEW.workflow_version
                   AND existing_subject = NEW.subject_ref THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'sourcing_case v2 workflow start rejected',
                    CONSTRAINT = 'ck_workflow_runs_sourcing_v2_admission';
            END IF;

            SELECT need_id INTO admission_need_id
            FROM sourcing_admissions
            WHERE tenant_id = NEW.tenant_id
              AND case_id = NEW.subject_ref
              AND state = 'starting'
              AND workflow_run_id IS NULL;

            IF NOT FOUND
               OR NEW.idempotency_key <>
                  'sourcing-case:v2:' || NEW.tenant_id || ':' || admission_need_id
               OR EXISTS (
                    SELECT 1 FROM workflow_runs
                    WHERE tenant_id = NEW.tenant_id
                      AND workflow_type = NEW.workflow_type
                      AND subject_ref = NEW.subject_ref
               ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '23514',
                    MESSAGE = 'sourcing_case v2 workflow start rejected',
                    CONSTRAINT = 'ck_workflow_runs_sourcing_v2_admission';
            END IF;

            RETURN NEW;
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
    op.execute(
        """
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM sourcing_admissions LIMIT 1)
               OR EXISTS (
                    SELECT 1 FROM workflow_runs
                    WHERE workflow_type = 'sourcing_case' AND workflow_version = 2
                    LIMIT 1
               ) THEN
                RAISE EXCEPTION '0055 refuses to remove active sourcing v2 guard';
            END IF;
        END $$;
        """
    )
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON workflow_runs")
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION}()")
    op.drop_index(_SUBJECT_INDEX, table_name="workflow_runs")
