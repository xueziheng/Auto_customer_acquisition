"""Phase 1 人工审批包与幂等应用事实。

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approval_packages",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("approval_id", sa.String(40), nullable=False),
        sa.Column("approval_type", sa.String(64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("proposed_change", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("blast_radius", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "state", sa.String(32), nullable=False, server_default=sa.text("'pending'")
        ),
        sa.Column("proposed_by_run", sa.String(40), nullable=True),
        sa.Column("proposed_by_employee", sa.String(40), nullable=True),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=False),
        sa.Column("change_set_ref", sa.String(200), nullable=True),
        sa.Column("owner_employee", sa.String(40), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(40), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("apply_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id", "approval_id", name="pk_approval_packages"
        ),
        sa.CheckConstraint(
            "state IN ('pending','approved','applied','apply_failed','rejected','expired')",
            name="ck_approval_packages_state",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(proposed_change) = 'object' AND "
            "jsonb_typeof(blast_radius) = 'object' AND "
            "jsonb_typeof(evidence_refs) = 'array'",
            name="ck_approval_packages_jsonb",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(approval_id) <> '' AND "
            "btrim(approval_type) <> '' AND btrim(title) <> '' AND btrim(reason) <> ''",
            name="ck_approval_packages_core_nonblank",
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name="ck_approval_packages_expiry"
        ),
        sa.CheckConstraint(
            "(state = 'pending' AND decided_at IS NULL AND decided_by IS NULL) OR "
            "(state = 'expired' AND decided_at IS NULL AND decided_by IS NULL) OR "
            "(state IN ('approved','applied','apply_failed','rejected') AND "
            "decided_at IS NOT NULL AND decided_by IS NOT NULL)",
            name="ck_approval_packages_decision",
        ),
        sa.CheckConstraint(
            "(state = 'applied' AND applied_at IS NOT NULL AND apply_error IS NULL) OR "
            "(state = 'apply_failed' AND applied_at IS NULL AND apply_error IS NOT NULL) OR "
            "(state NOT IN ('applied','apply_failed') AND applied_at IS NULL AND apply_error IS NULL)",
            name="ck_approval_packages_application",
        ),
    )
    op.create_index(
        "ix_approval_packages_tenant_state_expiry",
        "approval_packages",
        ["tenant_id", "state", "expires_at", "approval_id"],
    )
    op.create_index(
        "ix_approval_packages_tenant_change_set",
        "approval_packages",
        ["tenant_id", "change_set_ref", "created_at"],
    )
    op.create_index(
        "uq_approval_packages_pending_change_set",
        "approval_packages",
        ["tenant_id", "change_set_ref"],
        unique=True,
        postgresql_where=sa.text("state = 'pending' AND change_set_ref IS NOT NULL"),
    )
    op.create_table(
        "approval_applications",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("approval_id", sa.String(40), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "approval_id", name="pk_approval_applications"
        ),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_approval_applications_key"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "approval_id"],
            ["approval_packages.tenant_id", "approval_packages.approval_id"],
            ondelete="RESTRICT",
            name="fk_approval_applications_package",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(approval_id) <> '' AND "
            "btrim(idempotency_key) <> ''",
            name="ck_approval_applications_nonblank",
        ),
    )
    op.execute(
        """
        CREATE FUNCTION approval_package_guard() RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'approval packages are immutable facts';
            END IF;
            IF OLD.tenant_id = NEW.tenant_id
               AND OLD.approval_id = NEW.approval_id
               AND OLD.approval_type = NEW.approval_type
               AND OLD.title = NEW.title
               AND OLD.proposed_change = NEW.proposed_change
               AND OLD.reason = NEW.reason
               AND OLD.blast_radius = NEW.blast_radius
               AND OLD.created_at = NEW.created_at
               AND OLD.expires_at = NEW.expires_at
               AND OLD.proposed_by_run IS NOT DISTINCT FROM NEW.proposed_by_run
               AND OLD.proposed_by_employee IS NOT DISTINCT FROM NEW.proposed_by_employee
               AND OLD.evidence_refs = NEW.evidence_refs
               AND OLD.change_set_ref IS NOT DISTINCT FROM NEW.change_set_ref
               AND OLD.owner_employee IS NOT DISTINCT FROM NEW.owner_employee
               AND ((OLD.state = 'pending' AND NEW.state IN ('approved','rejected','expired'))
                    OR (OLD.state = 'approved' AND NEW.state IN ('applied','apply_failed'))) THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'approval package immutable fields or state changed';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_approval_packages_guard BEFORE UPDATE OR DELETE ON "
        "approval_packages FOR EACH ROW EXECUTE FUNCTION approval_package_guard();"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_approval_packages_guard ON approval_packages")
    op.execute("DROP FUNCTION approval_package_guard()")
    op.drop_table("approval_applications")
    op.drop_index(
        "uq_approval_packages_pending_change_set", table_name="approval_packages"
    )
    op.drop_index(
        "ix_approval_packages_tenant_change_set", table_name="approval_packages"
    )
    op.drop_index(
        "ix_approval_packages_tenant_state_expiry", table_name="approval_packages"
    )
    op.drop_table("approval_packages")
