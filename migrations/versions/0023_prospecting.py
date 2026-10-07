"""prospecting 企业、联系人、联系方式、法律依据与删除抑制。

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-20
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prospect_accounts",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("account_id", sa.String(40), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("country", sa.String(16), nullable=False),
        sa.Column("website_domain", sa.String(253), nullable=True),
        sa.Column("entity_type", sa.String(80), nullable=True),
        sa.Column("industry", sa.String(160), nullable=True),
        sa.Column("size_hint", sa.String(80), nullable=True),
        sa.Column("source_signal_refs", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "account_id", name="pk_prospect_accounts"),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(account_id) <> '' AND "
            "btrim(name) <> '' AND btrim(country) <> ''",
            name="ck_prospect_accounts_core_nonblank",
        ),
        sa.CheckConstraint(
            "(website_domain IS NULL OR btrim(website_domain) <> '') AND "
            "(entity_type IS NULL OR btrim(entity_type) <> '') AND "
            "(industry IS NULL OR btrim(industry) <> '') AND "
            "(size_hint IS NULL OR btrim(size_hint) <> '')",
            name="ck_prospect_accounts_optional_nonblank",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(source_signal_refs) = 'array'",
            name="ck_prospect_accounts_source_refs_jsonb",
        ),
    )
    op.create_index(
        "uq_prospect_accounts_domain",
        "prospect_accounts",
        ["tenant_id", "website_domain"],
        unique=True,
        postgresql_where=sa.text("website_domain IS NOT NULL"),
    )
    op.create_index(
        "ix_prospect_accounts_name",
        "prospect_accounts",
        ["tenant_id", "country", "name"],
    )
    op.create_table(
        "prospect_contacts",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("contact_id", sa.String(40), nullable=False),
        sa.Column("account_id", sa.String(40), nullable=False),
        sa.Column("full_name", sa.String(200), nullable=True),
        sa.Column("role_title", sa.String(200), nullable=True),
        sa.Column("language", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "contact_id", name="pk_prospect_contacts"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "account_id"],
            ["prospect_accounts.tenant_id", "prospect_accounts.account_id"],
            name="fk_prospect_contacts_account",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(contact_id) <> '' AND "
            "btrim(account_id) <> ''",
            name="ck_prospect_contacts_core_nonblank",
        ),
        sa.CheckConstraint(
            "(full_name IS NULL OR btrim(full_name) <> '') AND "
            "(role_title IS NULL OR btrim(role_title) <> '') AND "
            "(language IS NULL OR btrim(language) <> '')",
            name="ck_prospect_contacts_optional_nonblank",
        ),
    )
    op.create_index(
        "ix_prospect_contacts_account",
        "prospect_contacts",
        ["tenant_id", "account_id", "created_at", "contact_id"],
    )
    op.create_table(
        "contact_points",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("contact_point_id", sa.String(40), nullable=False),
        sa.Column("contact_id", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("value", sa.String(320), nullable=False),
        sa.Column("value_hash", sa.String(64), nullable=False),
        sa.Column(
            "verification_status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'unverified'"),
        ),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verification_provider", sa.String(100), nullable=True),
        sa.Column("enrichment_cost_note", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "contact_point_id", name="pk_contact_points"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "contact_id"],
            ["prospect_contacts.tenant_id", "prospect_contacts.contact_id"],
            name="fk_contact_points_contact",
        ),
        sa.UniqueConstraint(
            "tenant_id", "kind", "value_hash", name="uq_contact_points_value_hash"
        ),
        sa.CheckConstraint("kind IN ('email','phone')", name="ck_contact_points_kind"),
        sa.CheckConstraint(
            "verification_status IN ('unverified','verified','risky','invalid')",
            name="ck_contact_points_verification_status",
        ),
        sa.CheckConstraint(
            "value_hash ~ '^[0-9a-f]{64}$'", name="ck_contact_points_value_hash"
        ),
        sa.CheckConstraint(
            "(verification_status = 'verified') = (verified_at IS NOT NULL)",
            name="ck_contact_points_verified_pair",
        ),
        sa.CheckConstraint(
            "(verification_status = 'unverified') = "
            "(verification_provider IS NULL)",
            name="ck_contact_points_provider_state",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(contact_point_id) <> '' AND "
            "btrim(contact_id) <> '' AND btrim(value) <> ''",
            name="ck_contact_points_core_nonblank",
        ),
        sa.CheckConstraint(
            "(verification_provider IS NULL OR btrim(verification_provider) <> '') "
            "AND (enrichment_cost_note IS NULL OR btrim(enrichment_cost_note) <> '')",
            name="ck_contact_points_optional_nonblank",
        ),
    )
    op.create_table(
        "contact_legal_basis",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("contact_point_id", sa.String(40), nullable=False),
        sa.Column("basis", sa.String(32), nullable=False),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("contact_type", sa.String(32), nullable=False),
        sa.Column("source", sa.String(100), nullable=False),
        sa.Column("source_url", sa.String(2000), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assessment_ref", sa.String(200), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id", "contact_point_id", name="pk_contact_legal_basis"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "contact_point_id"],
            ["contact_points.tenant_id", "contact_points.contact_point_id"],
            ondelete="CASCADE",
            name="fk_contact_legal_basis_contact_point",
        ),
        sa.CheckConstraint(
            "basis IN ('legitimate_interest','consent','existing_customer')",
            name="ck_contact_legal_basis_basis",
        ),
        sa.CheckConstraint(
            "subject_type IN ('legal_entity','sole_trader','natural_person')",
            name="ck_contact_legal_basis_subject_type",
        ),
        sa.CheckConstraint(
            "contact_type IN ('role_based','personal_business')",
            name="ck_contact_legal_basis_contact_type",
        ),
        sa.CheckConstraint(
            "basis <> 'legitimate_interest' OR "
            "(assessment_ref IS NOT NULL AND btrim(assessment_ref) <> '')",
            name="ck_contact_legal_basis_li_assessment",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(contact_point_id) <> '' AND "
            "btrim(source) <> ''",
            name="ck_contact_legal_basis_core_nonblank",
        ),
        sa.CheckConstraint(
            "(source_url IS NULL OR btrim(source_url) <> '') AND "
            "(assessment_ref IS NULL OR btrim(assessment_ref) <> '')",
            name="ck_contact_legal_basis_optional_nonblank",
        ),
    )
    op.create_table(
        "prospecting_erasure_suppressions",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("value_hash", sa.String(64), nullable=False),
        sa.Column("erased_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "value_hash", name="pk_prospecting_erasure_suppressions"
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> ''", name="ck_prospecting_erasure_tenant_nonblank"
        ),
        sa.CheckConstraint(
            "value_hash ~ '^[0-9a-f]{64}$'",
            name="ck_prospecting_erasure_value_hash",
        ),
    )
    op.execute(
        """
        CREATE FUNCTION prospecting_erasure_append_only_guard() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'prospecting erasure suppressions are append-only';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        "CREATE TRIGGER trg_prospecting_erasure_suppressions_append_only "
        "BEFORE UPDATE OR DELETE ON prospecting_erasure_suppressions "
        "FOR EACH ROW EXECUTE FUNCTION prospecting_erasure_append_only_guard();"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_prospecting_erasure_suppressions_append_only "
        "ON prospecting_erasure_suppressions;"
    )
    op.execute("DROP FUNCTION prospecting_erasure_append_only_guard();")
    op.drop_table("prospecting_erasure_suppressions")
    op.drop_table("contact_legal_basis")
    op.drop_table("contact_points")
    op.drop_table("prospect_contacts")
    op.drop_index("ix_prospect_accounts_name", table_name="prospect_accounts")
    op.drop_index("uq_prospect_accounts_domain", table_name="prospect_accounts")
    op.drop_table("prospect_accounts")
