"""Add optimistic sourcing admission policy proposals.

Revision ID: 0052
Revises: 0051
Create Date: 2026-09-02
"""

import sqlalchemy as sa
from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


_GUARD_WITH_BASE_VERSION = """
CREATE OR REPLACE FUNCTION directive_proposal_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'directive proposals are immutable facts';
    END IF;
    IF OLD.tenant_id = NEW.tenant_id
       AND OLD.proposal_id = NEW.proposal_id
       AND OLD.raw_text = NEW.raw_text
       AND OLD.parsed_content = NEW.parsed_content
       AND OLD.interpretation_summary = NEW.interpretation_summary
       AND OLD.expected_behavior_changes = NEW.expected_behavior_changes
       AND OLD.parsed_by = NEW.parsed_by
       AND OLD.created_at = NEW.created_at
       AND OLD.base_directive_version IS NOT DISTINCT FROM NEW.base_directive_version
       AND OLD.state = 'pending_confirmation'
       AND NEW.state IN ('confirmed','rejected','expired') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'directive proposal immutable fields changed';
END;
$$ LANGUAGE plpgsql;
"""

_LEGACY_GUARD = """
CREATE OR REPLACE FUNCTION directive_proposal_guard() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'directive proposals are immutable facts';
    END IF;
    IF OLD.tenant_id = NEW.tenant_id
       AND OLD.proposal_id = NEW.proposal_id
       AND OLD.raw_text = NEW.raw_text
       AND OLD.parsed_content = NEW.parsed_content
       AND OLD.interpretation_summary = NEW.interpretation_summary
       AND OLD.expected_behavior_changes = NEW.expected_behavior_changes
       AND OLD.parsed_by = NEW.parsed_by
       AND OLD.created_at = NEW.created_at
       AND OLD.state = 'pending_confirmation'
       AND NEW.state IN ('confirmed','rejected','expired') THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'directive proposal immutable fields changed';
END;
$$ LANGUAGE plpgsql;
"""


def upgrade() -> None:
    op.add_column(
        "directive_proposals",
        sa.Column("base_directive_version", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_directive_proposals_base_version",
        "directive_proposals",
        "base_directive_version IS NULL OR base_directive_version >= 0",
    )
    op.execute(_GUARD_WITH_BASE_VERSION)


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM directive_proposals
                WHERE base_directive_version IS NOT NULL
            ) THEN
                RAISE EXCEPTION '0052 refuses to drop sourcing admission base evidence';
            END IF;
        END;
        $$;
        """
    )
    op.execute(_LEGACY_GUARD)
    op.drop_constraint(
        "ck_directive_proposals_base_version",
        "directive_proposals",
        type_="check",
    )
    op.drop_column("directive_proposals", "base_directive_version")
