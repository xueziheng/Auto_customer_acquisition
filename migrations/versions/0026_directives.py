"""Phase 1 老板指令提案、历史版本与当前指针。

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "directive_proposals",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("proposal_id", sa.String(40), nullable=False),
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("parsed_content", postgresql.JSONB(), nullable=False),
        sa.Column("interpretation_summary", sa.Text(), nullable=False),
        sa.Column("expected_behavior_changes", postgresql.JSONB(), nullable=False),
        sa.Column("parsed_by", sa.String(128), nullable=False),
        sa.Column(
            "state",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'pending_confirmation'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(40), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id", "proposal_id", name="pk_directive_proposals"
        ),
        sa.CheckConstraint(
            "state IN ('pending_confirmation','confirmed','rejected','expired')",
            name="ck_directive_proposals_state",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(parsed_content) = 'object' AND "
            "jsonb_typeof(expected_behavior_changes) = 'array' AND "
            "jsonb_array_length(expected_behavior_changes) > 0",
            name="ck_directive_proposals_jsonb",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(proposal_id) <> '' AND "
            "btrim(raw_text) <> '' AND btrim(interpretation_summary) <> '' AND "
            "btrim(parsed_by) <> ''",
            name="ck_directive_proposals_core_nonblank",
        ),
        sa.CheckConstraint(
            "(state = 'pending_confirmation' AND decided_at IS NULL AND "
            "decided_by IS NULL) OR "
            "(state = 'expired' AND decided_at IS NOT NULL) OR "
            "(state IN ('confirmed','rejected') AND decided_at IS NOT NULL AND "
            "decided_by IS NOT NULL)",
            name="ck_directive_proposals_decision",
        ),
    )
    op.create_index(
        "ix_directive_proposals_tenant_state_created",
        "directive_proposals",
        ["tenant_id", "state", "created_at", "proposal_id"],
    )
    op.create_table(
        "directive_versions",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("directive_id", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("source_proposal_id", sa.String(40), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_by", sa.String(40), nullable=False),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rollback_of", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id", "directive_id", name="pk_directive_versions"
        ),
        sa.UniqueConstraint(
            "tenant_id", "version", name="uq_directive_versions_tenant_version"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "directive_id",
            "version",
            name="uq_directive_versions_pointer",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "source_proposal_id"],
            ["directive_proposals.tenant_id", "directive_proposals.proposal_id"],
            ondelete="RESTRICT",
            name="fk_directive_versions_proposal",
        ),
        sa.CheckConstraint("version >= 1", name="ck_directive_versions_version"),
        sa.CheckConstraint(
            "jsonb_typeof(content) = 'object'",
            name="ck_directive_versions_content_jsonb",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(directive_id) <> '' AND "
            "btrim(source_proposal_id) <> '' AND btrim(activated_by) <> ''",
            name="ck_directive_versions_core_nonblank",
        ),
        sa.CheckConstraint(
            "superseded_at IS NULL OR superseded_at >= activated_at",
            name="ck_directive_versions_superseded_at",
        ),
        sa.CheckConstraint(
            "rollback_of IS NULL OR (rollback_of >= 1 AND rollback_of < version)",
            name="ck_directive_versions_rollback",
        ),
    )
    op.create_index(
        "ix_directive_versions_tenant_version",
        "directive_versions",
        ["tenant_id", "version"],
    )
    op.create_table(
        "boss_directives",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("directive_id", sa.String(40), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", name="pk_boss_directives"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "directive_id", "version"],
            [
                "directive_versions.tenant_id",
                "directive_versions.directive_id",
                "directive_versions.version",
            ],
            ondelete="RESTRICT",
            name="fk_boss_directives_version",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(directive_id) <> '' AND version >= 1",
            name="ck_boss_directives_core",
        ),
    )
    op.execute(
        """
        CREATE FUNCTION directive_proposal_guard() RETURNS trigger AS $$
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
    )
    op.execute(
        "CREATE TRIGGER trg_directive_proposals_guard BEFORE UPDATE OR DELETE ON "
        "directive_proposals FOR EACH ROW EXECUTE FUNCTION directive_proposal_guard();"
    )
    op.execute(
        """
        CREATE FUNCTION directive_version_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'directive versions are append only';
            END IF;
            IF OLD.tenant_id = NEW.tenant_id
               AND OLD.directive_id = NEW.directive_id
               AND OLD.version = NEW.version
               AND OLD.content = NEW.content
               AND OLD.source_proposal_id = NEW.source_proposal_id
               AND OLD.activated_at = NEW.activated_at
               AND OLD.activated_by = NEW.activated_by
               AND OLD.rollback_of IS NOT DISTINCT FROM NEW.rollback_of
               AND OLD.superseded_at IS NULL
               AND NEW.superseded_at IS NOT NULL THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'directive version immutable fields changed';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_directive_versions_guard BEFORE UPDATE OR DELETE ON "
        "directive_versions FOR EACH ROW EXECUTE FUNCTION directive_version_guard();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_directive_versions_guard ON directive_versions")
    op.execute("DROP FUNCTION directive_version_guard()")
    op.execute("DROP TRIGGER trg_directive_proposals_guard ON directive_proposals")
    op.execute("DROP FUNCTION directive_proposal_guard()")
    op.drop_table("boss_directives")
    op.drop_index(
        "ix_directive_versions_tenant_version",
        table_name="directive_versions",
    )
    op.drop_table("directive_versions")
    op.drop_index(
        "ix_directive_proposals_tenant_state_created",
        table_name="directive_proposals",
    )
    op.drop_table("directive_proposals")
