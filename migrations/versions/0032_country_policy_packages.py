"""国家政策不可变版本、字段来源与 append-only 激活事实。

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "country_policy_versions",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("country_policy_version_id", sa.String(40), nullable=False),
        sa.Column("country", sa.String(64), nullable=False),
        sa.Column("country_key", sa.String(64), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("base_version_id", sa.String(40), nullable=True),
        sa.Column("base_content_hash", sa.CHAR(64), nullable=True),
        sa.Column("public_research_allowed", sa.Boolean(), nullable=False),
        sa.Column("contact_enrichment_allowed", sa.Boolean(), nullable=False),
        sa.Column("cold_b2b_email_allowed", sa.Boolean(), nullable=False),
        sa.Column("personal_data_basis_required", sa.Boolean(), nullable=False),
        sa.Column("subject_type_affects_judgment", sa.Boolean(), nullable=False),
        sa.Column("contact_type_affects_judgment", sa.Boolean(), nullable=False),
        sa.Column("opt_out_deadline_days", sa.Integer(), nullable=True),
        sa.Column("local_representative_required", sa.Boolean(), nullable=False),
        sa.Column("requirements", postgresql.JSONB(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=False),
        sa.Column("proposed_by", sa.String(40), nullable=False),
        sa.Column("proposed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "country_policy_version_id",
            name="pk_country_policy_versions",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "country_key",
            "version_number",
            name="uq_country_policy_versions_country_number",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_country_policy_versions_idempotency",
        ),
        sa.CheckConstraint(
            "version_number > 0 AND "
            "(opt_out_deadline_days IS NULL OR "
            "opt_out_deadline_days BETWEEN 1 AND 365)",
            name="ck_country_policy_versions_bounds",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$' AND "
            "(base_content_hash IS NULL OR "
            "base_content_hash ~ '^[0-9a-f]{64}$')",
            name="ck_country_policy_versions_hashes",
        ),
        sa.CheckConstraint(
            "(base_version_id IS NULL AND base_content_hash IS NULL) OR "
            "(base_version_id IS NOT NULL AND base_content_hash IS NOT NULL)",
            name="ck_country_policy_versions_base_pair",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(requirements) = 'array' AND "
            "jsonb_array_length(requirements) <= 100",
            name="ck_country_policy_versions_requirements",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND "
            "btrim(country_policy_version_id) <> '' AND "
            "btrim(country) <> '' AND btrim(country_key) <> '' AND "
            "btrim(notes) <> '' AND btrim(proposed_by) <> '' AND "
            "btrim(idempotency_key) <> ''",
            name="ck_country_policy_versions_core_nonblank",
        ),
    )

    op.create_table(
        "country_policy_field_provenance",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("country_policy_version_id", sa.String(40), nullable=False),
        sa.Column("field_name", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String(200), nullable=False),
        sa.Column("extracted_by", sa.String(200), nullable=False),
        sa.Column("extracted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_by", sa.String(40), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_url", sa.String(2048), nullable=True),
        sa.Column("page_hash", sa.CHAR(64), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "country_policy_version_id",
            "field_name",
            name="pk_country_policy_field_provenance",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "country_policy_version_id"],
            [
                "country_policy_versions.tenant_id",
                "country_policy_versions.country_policy_version_id",
            ],
            ondelete="RESTRICT",
            name="fk_country_policy_field_provenance_version",
        ),
        sa.CheckConstraint(
            "field_name IN ("
            "'public_research_allowed','contact_enrichment_allowed',"
            "'cold_b2b_email_allowed','personal_data_basis_required',"
            "'subject_type_affects_judgment','contact_type_affects_judgment',"
            "'opt_out_deadline_days','local_representative_required',"
            "'requirements')",
            name="ck_country_policy_field_provenance_field",
        ),
        sa.CheckConstraint(
            "source_type IN ('web_page','upload','employee_input') AND "
            "source_type <> 'agent_inference'",
            name="ck_country_policy_field_provenance_source",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND "
            "btrim(country_policy_version_id) <> '' AND "
            "btrim(field_name) <> '' AND btrim(source_id) <> '' AND "
            "btrim(extracted_by) <> '' AND btrim(confirmed_by) <> '' AND "
            "extracted_by = 'human:' || confirmed_by AND "
            "confirmed_at = extracted_at",
            name="ck_country_policy_field_provenance_human_confirmed",
        ),
        sa.CheckConstraint(
            "(source_type = 'web_page' AND source_url IS NOT NULL AND "
            "btrim(source_url) <> '' AND page_hash ~ '^[0-9a-f]{64}$') OR "
            "(source_type <> 'web_page' AND source_url IS NULL AND "
            "page_hash IS NULL)",
            name="ck_country_policy_field_provenance_web_shape",
        ),
    )

    op.create_table(
        "country_policy_activations",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("country_policy_activation_id", sa.String(40), nullable=False),
        sa.Column("country_key", sa.String(64), nullable=False),
        sa.Column("activation_sequence", sa.Integer(), nullable=False),
        sa.Column("country_policy_version_id", sa.String(40), nullable=False),
        sa.Column("content_hash", sa.CHAR(64), nullable=False),
        sa.Column("approval_id", sa.String(40), nullable=False),
        sa.Column("change_set_ref", sa.String(160), nullable=False),
        sa.Column("approved_by", sa.String(40), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_by", sa.String(200), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "country_policy_activation_id",
            name="pk_country_policy_activations",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "country_policy_version_id"],
            [
                "country_policy_versions.tenant_id",
                "country_policy_versions.country_policy_version_id",
            ],
            ondelete="RESTRICT",
            name="fk_country_policy_activations_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "country_key",
            "activation_sequence",
            name="uq_country_policy_activations_country_sequence",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "country_policy_version_id",
            name="uq_country_policy_activations_version",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "approval_id",
            name="uq_country_policy_activations_approval",
        ),
        sa.CheckConstraint(
            "activation_sequence > 0 AND "
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_country_policy_activations_sequence_hash",
        ),
        sa.CheckConstraint(
            "change_set_ref = 'country_policy:' || "
            "country_policy_version_id || ':' || content_hash",
            name="ck_country_policy_activations_change_set",
        ),
        sa.CheckConstraint(
            "approved_at <= activated_at",
            name="ck_country_policy_activations_times",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND "
            "btrim(country_policy_activation_id) <> '' AND "
            "btrim(country_key) <> '' AND "
            "btrim(country_policy_version_id) <> '' AND "
            "btrim(approval_id) <> '' AND btrim(change_set_ref) <> '' AND "
            "btrim(approved_by) <> '' AND btrim(activated_by) <> ''",
            name="ck_country_policy_activations_core_nonblank",
        ),
    )

    op.execute(
        "CREATE FUNCTION guard_country_policy_append_only() RETURNS trigger "
        "AS $$ BEGIN RAISE EXCEPTION 'country policy facts are append-only' "
        "USING ERRCODE='23514'; END; $$ LANGUAGE plpgsql"
    )
    for table in (
        "country_policy_versions",
        "country_policy_field_provenance",
        "country_policy_activations",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON "
            f"{table} FOR EACH ROW EXECUTE FUNCTION "
            "guard_country_policy_append_only()"
        )


def downgrade() -> None:
    for table in (
        "country_policy_activations",
        "country_policy_field_provenance",
        "country_policy_versions",
    ):
        op.execute(f"DROP TRIGGER trg_{table}_append_only ON {table}")
    op.execute("DROP FUNCTION guard_country_policy_append_only()")
    op.drop_table("country_policy_activations")
    op.drop_table("country_policy_field_provenance")
    op.drop_table("country_policy_versions")
