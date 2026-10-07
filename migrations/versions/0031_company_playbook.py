"""Company Playbook 不可变版本与 append-only 激活事实。

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "company_playbook_versions",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("playbook_version_id", sa.String(40), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("base_version_id", sa.String(40), nullable=True),
        sa.Column("base_content_hash", sa.CHAR(64), nullable=True),
        sa.Column("company_type", sa.String(200), nullable=False),
        sa.Column("minimum_deal_amount", sa.Numeric(28, 12), nullable=False),
        sa.Column("minimum_deal_currency", sa.CHAR(3), nullable=False),
        sa.Column(
            "excluded_categories", postgresql.JSONB(), nullable=False
        ),
        sa.Column("sourcing_regions", postgresql.JSONB(), nullable=False),
        sa.Column("excluded_countries", postgresql.JSONB(), nullable=False),
        sa.Column("monthly_budget_credits", sa.BigInteger(), nullable=True),
        sa.Column(
            "approval_requirements", postgresql.JSONB(), nullable=False
        ),
        sa.Column("supply_capabilities_note", sa.Text(), nullable=True),
        sa.Column("proposed_by", sa.String(40), nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(40), nullable=False),
        sa.Column("extracted_by", sa.String(200), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "playbook_version_id",
            name="pk_company_playbook_versions",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "version_number",
            name="uq_company_playbook_versions_number",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_company_playbook_versions_idempotency",
        ),
        sa.CheckConstraint(
            "version_number > 0 AND minimum_deal_amount >= 0 AND "
            "(monthly_budget_credits IS NULL OR monthly_budget_credits >= 0)",
            name="ck_company_playbook_versions_nonnegative",
        ),
        sa.CheckConstraint(
            "minimum_deal_currency ~ '^[A-Z]{3}$'",
            name="ck_company_playbook_versions_currency",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$' AND "
            "(base_content_hash IS NULL OR base_content_hash ~ '^[0-9a-f]{64}$')",
            name="ck_company_playbook_versions_hashes",
        ),
        sa.CheckConstraint(
            "(base_version_id IS NULL AND base_content_hash IS NULL) OR "
            "(base_version_id IS NOT NULL AND base_content_hash IS NOT NULL)",
            name="ck_company_playbook_versions_base_pair",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(excluded_categories) = 'array' AND "
            "jsonb_array_length(excluded_categories) <= 200 AND "
            "jsonb_typeof(sourcing_regions) = 'array' AND "
            "jsonb_array_length(sourcing_regions) <= 200 AND "
            "jsonb_typeof(excluded_countries) = 'array' AND "
            "jsonb_array_length(excluded_countries) <= 200 AND "
            "jsonb_typeof(approval_requirements) = 'array' AND "
            "jsonb_array_length(approval_requirements) <= 200",
            name="ck_company_playbook_versions_json_arrays",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(playbook_version_id) <> '' AND "
            "btrim(company_type) <> '' AND btrim(proposed_by) <> '' AND "
            "btrim(idempotency_key) <> '' AND "
            "(supply_capabilities_note IS NULL OR "
            "btrim(supply_capabilities_note) <> '')",
            name="ck_company_playbook_versions_core_nonblank",
        ),
        sa.CheckConstraint(
            "source_type = 'employee_input' AND "
            "source_id = playbook_version_id AND "
            "extracted_by = 'human:' || proposed_by AND "
            "extracted_at = proposed_at",
            name="ck_company_playbook_versions_provenance",
        ),
    )
    op.create_index(
        "ix_company_playbook_versions_tenant_number",
        "company_playbook_versions",
        ["tenant_id", "version_number", "playbook_version_id"],
    )

    op.create_table(
        "company_playbook_activations",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("activation_id", sa.String(40), nullable=False),
        sa.Column("playbook_version_id", sa.String(40), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("approval_id", sa.String(40), nullable=False),
        sa.Column("change_set_ref", sa.String(160), nullable=False),
        sa.Column("approved_by", sa.String(40), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_by", sa.String(200), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "activation_id",
            name="pk_company_playbook_activations",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "playbook_version_id"],
            [
                "company_playbook_versions.tenant_id",
                "company_playbook_versions.playbook_version_id",
            ],
            ondelete="RESTRICT",
            name="fk_company_playbook_activations_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "playbook_version_id",
            name="uq_company_playbook_activations_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "approval_id",
            name="uq_company_playbook_activations_approval",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_company_playbook_activations_hash",
        ),
        sa.CheckConstraint(
            "change_set_ref = 'playbook:' || playbook_version_id || ':' || "
            "content_hash",
            name="ck_company_playbook_activations_change_set",
        ),
        sa.CheckConstraint(
            "approved_at <= activated_at",
            name="ck_company_playbook_activations_times",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(activation_id) <> '' AND "
            "btrim(playbook_version_id) <> '' AND btrim(approval_id) <> '' AND "
            "btrim(change_set_ref) <> '' AND btrim(approved_by) <> '' AND "
            "btrim(activated_by) <> ''",
            name="ck_company_playbook_activations_core_nonblank",
        ),
    )
    op.create_index(
        "ix_company_playbook_activations_tenant_current",
        "company_playbook_activations",
        ["tenant_id", "activated_at", "activation_id"],
    )

    op.execute(
        "CREATE FUNCTION guard_company_playbook_append_only() RETURNS trigger "
        "AS $$ BEGIN RAISE EXCEPTION 'company playbook facts are append-only' "
        "USING ERRCODE='23514'; END; $$ LANGUAGE plpgsql"
    )
    for table in (
        "company_playbook_versions",
        "company_playbook_activations",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON "
            f"{table} FOR EACH ROW EXECUTE FUNCTION "
            "guard_company_playbook_append_only()"
        )


def downgrade() -> None:
    for table in (
        "company_playbook_activations",
        "company_playbook_versions",
    ):
        op.execute(f"DROP TRIGGER trg_{table}_append_only ON {table}")
    op.execute("DROP FUNCTION guard_company_playbook_append_only()")
    op.drop_index(
        "ix_company_playbook_activations_tenant_current",
        table_name="company_playbook_activations",
    )
    op.drop_table("company_playbook_activations")
    op.drop_index(
        "ix_company_playbook_versions_tenant_number",
        table_name="company_playbook_versions",
    )
    op.drop_table("company_playbook_versions")
