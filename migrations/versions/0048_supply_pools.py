"""正式产品、候选产品、供应能力与供应商价格池。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "suppliers",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("supplier_id", sa.String(40), nullable=False),
        sa.Column("name", sa.String(300), nullable=False),
        sa.Column("normalized_name", sa.String(300), nullable=False),
        sa.Column("region", sa.String(100), nullable=True),
        sa.Column("platform_refs", postgresql.JSONB(), nullable=False),
        sa.Column("capability_tags", postgresql.JSONB(), nullable=False),
        sa.Column("verification", sa.String(24), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "supplier_id", name="pk_suppliers"),
        sa.UniqueConstraint(
            "tenant_id", "normalized_name", name="uq_suppliers_normalized_name"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(platform_refs) = 'array'",
            name="ck_suppliers_platform_refs_json",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(capability_tags) = 'array'",
            name="ck_suppliers_capability_tags_json",
        ),
        sa.CheckConstraint(
            "verification IN ('unverified','basic_checked','transacted')",
            name="ck_suppliers_verification",
        ),
        sa.CheckConstraint(
            "btrim(name) <> '' AND btrim(normalized_name) <> ''",
            name="ck_suppliers_core_nonblank",
        ),
    )
    op.create_index(
        "ix_suppliers_capability_tags",
        "suppliers",
        ["tenant_id", "verification"],
        postgresql_using="btree",
    )

    op.create_table(
        "products",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("product_id", sa.String(40), nullable=False),
        sa.Column("pool", sa.String(24), nullable=False),
        sa.Column("candidate_status", sa.String(24), nullable=True),
        sa.Column("name_zh", sa.String(300), nullable=False),
        sa.Column("name_en", sa.String(300), nullable=False),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("normalized_category", sa.String(100), nullable=False),
        sa.Column("spec_summary", sa.Text(), nullable=True),
        sa.Column("moq", sa.Integer(), nullable=True),
        sa.Column("lead_time_days_min", sa.Integer(), nullable=True),
        sa.Column("lead_time_days_max", sa.Integer(), nullable=True),
        sa.Column("supplier_id", sa.String(40), nullable=True),
        sa.Column("internal_cost_amount", sa.Numeric(28, 12), nullable=True),
        sa.Column("internal_cost_currency", sa.CHAR(3), nullable=True),
        sa.Column("internal_cost_basis", sa.Text(), nullable=True),
        sa.Column("internal_cost_unit", sa.String(50), nullable=True),
        sa.Column("internal_cost_source_ref", sa.String(32), nullable=True),
        sa.Column("allowed_price_min_amount", sa.Numeric(28, 12), nullable=True),
        sa.Column("allowed_price_min_currency", sa.CHAR(3), nullable=True),
        sa.Column("allowed_price_max_amount", sa.Numeric(28, 12), nullable=True),
        sa.Column("allowed_price_max_currency", sa.CHAR(3), nullable=True),
        sa.Column("sellable_markets", postgresql.JSONB(), nullable=False),
        sa.Column("customizable", sa.Boolean(), nullable=False),
        sa.Column("selling_points", postgresql.JSONB(), nullable=False),
        sa.Column("known_issues", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "product_id", name="pk_products"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "supplier_id"],
            ["suppliers.tenant_id", "suppliers.supplier_id"],
            name="fk_products_supplier",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "internal_cost_source_ref"],
            ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"],
            name="fk_products_internal_cost_artifact",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "pool IN ('formal','candidate','capability')", name="ck_products_pool"
        ),
        sa.CheckConstraint(
            "(pool = 'candidate' AND candidate_status IS NOT NULL "
            "AND candidate_status IN ('source_only','partial','not_approved')) OR "
            "(pool <> 'candidate' AND candidate_status IS NULL)",
            name="ck_products_candidate_status",
        ),
        sa.CheckConstraint("moq IS NULL OR moq >= 1", name="ck_products_moq"),
        sa.CheckConstraint(
            "(lead_time_days_min IS NULL AND lead_time_days_max IS NULL) OR "
            "(lead_time_days_min IS NOT NULL AND lead_time_days_max IS NOT NULL "
            "AND lead_time_days_min >= 0 AND lead_time_days_max >= lead_time_days_min)",
            name="ck_products_lead_time",
        ),
        sa.CheckConstraint(
            "(internal_cost_amount IS NULL AND internal_cost_currency IS NULL "
            "AND internal_cost_basis IS NULL AND internal_cost_unit IS NULL "
            "AND internal_cost_source_ref IS NULL) OR "
            "(internal_cost_amount IS NOT NULL AND internal_cost_amount >= 0 "
            "AND internal_cost_currency IS NOT NULL "
            "AND internal_cost_currency ~ '^[A-Z]{3}$' "
            "AND internal_cost_basis IS NOT NULL AND btrim(internal_cost_basis) <> '' "
            "AND internal_cost_unit IS NOT NULL AND btrim(internal_cost_unit) <> '' "
            "AND internal_cost_source_ref IS NOT NULL)",
            name="ck_products_internal_cost_complete",
        ),
        sa.CheckConstraint(
            "(allowed_price_min_amount IS NULL AND allowed_price_min_currency IS NULL) OR "
            "(allowed_price_min_amount IS NOT NULL AND allowed_price_min_amount >= 0 "
            "AND allowed_price_min_currency IS NOT NULL "
            "AND allowed_price_min_currency ~ '^[A-Z]{3}$')",
            name="ck_products_allowed_min_pair",
        ),
        sa.CheckConstraint(
            "(allowed_price_max_amount IS NULL AND allowed_price_max_currency IS NULL) OR "
            "(allowed_price_max_amount IS NOT NULL AND allowed_price_max_amount >= 0 "
            "AND allowed_price_max_currency IS NOT NULL "
            "AND allowed_price_max_currency ~ '^[A-Z]{3}$')",
            name="ck_products_allowed_max_pair",
        ),
        sa.CheckConstraint(
            "allowed_price_min_amount IS NULL OR allowed_price_max_amount IS NULL OR "
            "(allowed_price_min_currency = allowed_price_max_currency "
            "AND allowed_price_min_amount <= allowed_price_max_amount)",
            name="ck_products_allowed_range",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(sellable_markets) = 'array' AND jsonb_typeof(selling_points) = 'array' "
            "AND jsonb_typeof(known_issues) = 'array'",
            name="ck_products_lists_json",
        ),
        sa.CheckConstraint(
            "btrim(name_zh) <> '' AND btrim(name_en) <> '' AND btrim(category) <> '' "
            "AND btrim(normalized_category) <> ''",
            name="ck_products_core_nonblank",
        ),
    )
    op.create_index(
        "ix_products_pool_category",
        "products",
        ["tenant_id", "pool", "normalized_category", "product_id"],
    )

    op.create_table(
        "product_match_specs",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("product_id", sa.String(40), nullable=False),
        sa.Column("normalized_spec_name", sa.String(100), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("evidence_ref", sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "product_id",
            "normalized_spec_name",
            name="pk_product_match_specs",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["products.tenant_id", "products.product_id"],
            name="fk_product_match_specs_product",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "evidence_ref"],
            ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"],
            name="fk_product_match_specs_artifact",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "btrim(normalized_spec_name) <> '' AND btrim(value) <> ''",
            name="ck_product_match_specs_nonblank",
        ),
    )

    op.create_table(
        "product_variants",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("variant_id", sa.String(40), nullable=False),
        sa.Column("product_id", sa.String(40), nullable=False),
        sa.Column("sku", sa.String(100), nullable=False),
        sa.Column("attributes", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "variant_id", name="pk_product_variants"),
        sa.UniqueConstraint("tenant_id", "sku", name="uq_product_variants_sku"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["products.tenant_id", "products.product_id"],
            name="fk_product_variants_product",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "btrim(sku) <> '' AND jsonb_typeof(attributes) = 'object'",
            name="ck_product_variants_core",
        ),
    )

    op.create_table(
        "supply_capabilities",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("capability_id", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(100), nullable=False),
        sa.Column("normalized_kind", sa.String(100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("proof_refs", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "capability_id", name="pk_supply_capabilities"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "normalized_kind",
            "capability_id",
            name="uq_supply_capabilities_kind_id",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(proof_refs) = 'array'",
            name="ck_supply_capabilities_proof_json",
        ),
        sa.CheckConstraint(
            "btrim(kind) <> '' AND btrim(normalized_kind) <> '' AND btrim(description) <> ''",
            name="ck_supply_capabilities_core",
        ),
    )
    op.create_index(
        "ix_supply_capabilities_kind",
        "supply_capabilities",
        ["tenant_id", "normalized_kind", "capability_id"],
    )

    op.create_table(
        "product_candidate_sources",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("product_id", sa.String(40), nullable=False),
        sa.Column("sourcing_case_id", sa.String(40), nullable=False),
        sa.Column("supplier_candidate_id", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "product_id", name="pk_product_candidate_sources"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "sourcing_case_id",
            "supplier_candidate_id",
            name="uq_product_candidate_sources_origin",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            ["products.tenant_id", "products.product_id"],
            name="fk_product_candidate_sources_product",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "sourcing_case_id", "supplier_candidate_id"],
            [
                "sourcing_candidates.tenant_id",
                "sourcing_candidates.case_id",
                "sourcing_candidates.candidate_id",
            ],
            name="fk_product_candidate_sources_candidate",
            ondelete="RESTRICT",
        ),
    )

    op.create_table(
        "product_candidate_price_refs",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("product_id", sa.String(40), nullable=False),
        sa.Column("minimum_quantity", sa.Integer(), nullable=False),
        sa.Column("unit_amount", sa.Numeric(28, 12), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("unit", sa.String(50), nullable=False),
        sa.Column("artifact_id", sa.String(32), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "product_id",
            "minimum_quantity",
            name="pk_product_candidate_price_refs",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "product_id"],
            [
                "product_candidate_sources.tenant_id",
                "product_candidate_sources.product_id",
            ],
            name="fk_product_candidate_price_refs_source",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "artifact_id"],
            ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"],
            name="fk_product_candidate_price_refs_artifact",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "minimum_quantity >= 1 AND unit_amount > 0",
            name="ck_product_candidate_price_refs_positive",
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$' AND btrim(unit) <> ''",
            name="ck_product_candidate_price_refs_unit",
        ),
    )

    op.create_table(
        "supplier_price_records",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("price_record_id", sa.String(40), nullable=False),
        sa.Column("supplier_id", sa.String(40), nullable=False),
        sa.Column("product_desc", sa.Text(), nullable=False),
        sa.Column("quantity_tier", sa.Integer(), nullable=False),
        sa.Column("unit_amount", sa.Numeric(28, 12), nullable=False),
        sa.Column("currency", sa.CHAR(3), nullable=False),
        sa.Column("basis", sa.String(16), nullable=False),
        sa.Column("artifact_id", sa.String(32), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id", "price_record_id", name="pk_supplier_price_records"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "supplier_id",
            "product_desc",
            "quantity_tier",
            "observed_at",
            "artifact_id",
            name="uq_supplier_price_records_observation",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "supplier_id"],
            ["suppliers.tenant_id", "suppliers.supplier_id"],
            name="fk_supplier_price_records_supplier",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "artifact_id"],
            ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"],
            name="fk_supplier_price_records_artifact",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "quantity_tier >= 1 AND unit_amount > 0",
            name="ck_supplier_price_records_positive",
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$' AND basis IN ('indicative','quoted')",
            name="ck_supplier_price_records_price",
        ),
        sa.CheckConstraint(
            "btrim(product_desc) <> ''", name="ck_supplier_price_records_description"
        ),
        sa.CheckConstraint(
            "(basis = 'quoted' AND valid_until IS NOT NULL AND valid_until > observed_at) OR "
            "(basis = 'indicative' AND valid_until IS NULL)",
            name="ck_supplier_price_records_validity",
        ),
    )
    op.create_index(
        "ix_supplier_price_records_lookup",
        "supplier_price_records",
        ["tenant_id", "supplier_id", "observed_at", "price_record_id"],
    )

    op.create_foreign_key(
        "fk_sourcing_supply_options_product",
        "sourcing_supply_options",
        "products",
        ["tenant_id", "product_id"],
        ["tenant_id", "product_id"],
        ondelete="RESTRICT",
    )
    op.execute(
        "CREATE FUNCTION guard_supplier_price_record_audit() RETURNS trigger AS $$ "
        "BEGIN RAISE EXCEPTION 'immutable supplier price record' USING ERRCODE='23514'; "
        "END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_supplier_price_record_audit BEFORE UPDATE OR DELETE "
        "ON supplier_price_records FOR EACH ROW "
        "EXECUTE FUNCTION guard_supplier_price_record_audit()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_supplier_price_record_audit ON supplier_price_records")
    op.execute("DROP FUNCTION guard_supplier_price_record_audit()")
    op.drop_constraint(
        "fk_sourcing_supply_options_product",
        "sourcing_supply_options",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_supplier_price_records_lookup", table_name="supplier_price_records"
    )
    op.drop_table("supplier_price_records")
    op.drop_table("product_candidate_price_refs")
    op.drop_table("product_candidate_sources")
    op.drop_index("ix_supply_capabilities_kind", table_name="supply_capabilities")
    op.drop_table("supply_capabilities")
    op.drop_table("product_variants")
    op.drop_table("product_match_specs")
    op.drop_index("ix_products_pool_category", table_name="products")
    op.drop_table("products")
    op.drop_index("ix_suppliers_capability_tags", table_name="suppliers")
    op.drop_table("suppliers")
