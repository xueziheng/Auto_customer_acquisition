"""Sourcing V2 案例、证据、审核与搜索恢复事实。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None

_CASE_STATES = (
    "'opened','discovering','verifying','candidates_ready','handed_to_costing','failed'"
)
_ACTIVE_CASE_STATES = "'opened','discovering','verifying','candidates_ready'"
_PLAN_STATES = (
    "'pending_confirmation','authorized','running','exhausted','blocked','completed'"
)
_STOP_CODES = (
    "'approval_required','quota_status_unknown','paid_usage_enabled','quota_exhausted',"
    "'provider_timeout','provider_rate_limited','page_access_forbidden','login_or_captcha',"
    "'unsafe_redirect','no_search_results','no_verifiable_supplier',"
    "'no_qualified_candidate','reconciliation_required','opportunity_required',"
    "'need_incomplete','plan_confirmation_required','free_quota_unavailable',"
    "'budget_exhausted','no_qualified_supply','manual_stop'"
)
_STOP_STAGES = (
    "'intake','plan','quota','provider','page','candidate','review','cost_handoff'"
)
_STOP_DETAIL_CHECK = (
    "stop_detail IS NULL OR (jsonb_typeof(stop_detail) = 'object' "
    "AND stop_detail ? 'stage' "
    "AND (stop_detail - 'stage' - 'query_index' - 'provider_http_status' "
    "- 'observed_count' - 'configured_limit') = '{}'::jsonb "
    "AND jsonb_typeof(stop_detail->'stage') = 'string' "
    f"AND stop_detail->>'stage' IN ({_STOP_STAGES}) "
    "AND (NOT stop_detail ? 'query_index' "
    "OR jsonb_typeof(stop_detail->'query_index') = 'null' OR CASE "
    "WHEN jsonb_typeof(stop_detail->'query_index') = 'number' "
    "THEN (stop_detail->>'query_index')::numeric >= 0 "
    "AND (stop_detail->>'query_index')::numeric = trunc((stop_detail->>'query_index')::numeric) "
    "ELSE false END) "
    "AND (NOT stop_detail ? 'provider_http_status' "
    "OR jsonb_typeof(stop_detail->'provider_http_status') = 'null' OR CASE "
    "WHEN jsonb_typeof(stop_detail->'provider_http_status') = 'number' "
    "THEN (stop_detail->>'provider_http_status')::numeric BETWEEN 100 AND 599 "
    "AND (stop_detail->>'provider_http_status')::numeric = "
    "trunc((stop_detail->>'provider_http_status')::numeric) ELSE false END) "
    "AND (NOT stop_detail ? 'observed_count' "
    "OR jsonb_typeof(stop_detail->'observed_count') = 'null' OR CASE "
    "WHEN jsonb_typeof(stop_detail->'observed_count') = 'number' "
    "THEN (stop_detail->>'observed_count')::numeric >= 0 "
    "AND (stop_detail->>'observed_count')::numeric = "
    "trunc((stop_detail->>'observed_count')::numeric) ELSE false END) "
    "AND (NOT stop_detail ? 'configured_limit' "
    "OR jsonb_typeof(stop_detail->'configured_limit') = 'null' OR CASE "
    "WHEN jsonb_typeof(stop_detail->'configured_limit') = 'number' "
    "THEN (stop_detail->>'configured_limit')::numeric >= 0 "
    "AND (stop_detail->>'configured_limit')::numeric = "
    "trunc((stop_detail->>'configured_limit')::numeric) ELSE false END))"
)


def upgrade() -> None:
    op.create_table(
        "sourcing_cases",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("case_id", sa.String(40), nullable=False),
        sa.Column("need_id", sa.String(40), nullable=False),
        sa.Column("opportunity_id", sa.String(32), nullable=True),
        sa.Column("workflow_version", sa.Integer(), nullable=False),
        sa.Column("trigger_key", sa.String(200), nullable=False),
        sa.Column("need_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("need_snapshot_hash", sa.String(64), nullable=False),
        sa.Column(
            "state", sa.String(32), nullable=False, server_default=sa.text("'opened'")
        ),
        sa.Column("ladder_checked_to", sa.Integer(), nullable=True),
        sa.Column("active_search_plan_id", sa.String(40), nullable=True),
        sa.Column(
            "sealed_candidate_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("candidate_set_hash", sa.String(64), nullable=True),
        sa.Column("candidates_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_code", sa.String(40), nullable=True),
        sa.Column("stop_detail", postgresql.JSONB(), nullable=True),
        sa.Column("assigned_to", sa.String(40), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "case_id", name="pk_sourcing_cases"),
        sa.UniqueConstraint(
            "tenant_id", "trigger_key", name="uq_sourcing_cases_trigger"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "need_id"],
            ["validated_needs.tenant_id", "validated_needs.need_id"],
            name="fk_sourcing_cases_need",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "opportunity_id"],
            ["opportunities.tenant_id", "opportunities.opportunity_id"],
            name="fk_sourcing_cases_opportunity",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            f"state IN ({_CASE_STATES})", name="ck_sourcing_cases_state"
        ),
        sa.CheckConstraint(
            "workflow_version >= 1 AND version >= 1", name="ck_sourcing_cases_versions"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(need_snapshot) = 'object'",
            name="ck_sourcing_cases_snapshot_json",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(sealed_candidate_ids) = 'array' AND "
            "NOT jsonb_path_exists(sealed_candidate_ids, "
            "'$[*] ? (@.type() != \"string\")') AND "
            "((jsonb_array_length(sealed_candidate_ids) = 0 "
            "AND candidate_set_hash IS NULL AND candidates_verified_at IS NULL) OR "
            "(jsonb_array_length(sealed_candidate_ids) > 0 "
            "AND candidate_set_hash ~ '^[0-9a-f]{64}$' "
            "AND candidates_verified_at IS NOT NULL))",
            name="ck_sourcing_cases_candidate_seal",
        ),
        sa.CheckConstraint(
            "need_snapshot_hash ~ '^[0-9a-f]{64}$'",
            name="ck_sourcing_cases_snapshot_hash",
        ),
        sa.CheckConstraint(
            "ladder_checked_to IS NULL OR ladder_checked_to BETWEEN 1 AND 7",
            name="ck_sourcing_cases_ladder",
        ),
        sa.CheckConstraint(
            f"stop_code IS NULL OR stop_code IN ({_STOP_CODES})",
            name="ck_sourcing_cases_stop_code",
        ),
        sa.CheckConstraint(
            _STOP_DETAIL_CHECK,
            name="ck_sourcing_cases_stop_detail_json",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(case_id) <> '' AND btrim(need_id) <> '' "
            "AND btrim(trigger_key) <> ''",
            name="ck_sourcing_cases_core_nonblank",
        ),
        sa.CheckConstraint(
            "(state = 'failed') = (failed_reason IS NOT NULL AND btrim(failed_reason) <> '')",
            name="ck_sourcing_cases_failure",
        ),
        sa.CheckConstraint(
            "(state = 'handed_to_costing') = (completed_at IS NOT NULL)",
            name="ck_sourcing_cases_completed_at",
        ),
    )
    op.create_index(
        "uq_sourcing_cases_active_need",
        "sourcing_cases",
        ["tenant_id", "need_id", "workflow_version"],
        unique=True,
        postgresql_where=sa.text(f"state IN ({_ACTIVE_CASE_STATES})"),
    )
    op.create_index(
        "ix_sourcing_cases_queue",
        "sourcing_cases",
        ["tenant_id", "state", "opened_at", "case_id"],
    )

    op.create_table(
        "sourcing_ladder_checks",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("check_id", sa.String(40), nullable=False),
        sa.Column("case_id", sa.String(40), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("rung", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(40), nullable=False),
        sa.Column("input_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("input_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("conclusion", sa.Text(), nullable=False),
        sa.Column("match_object_type", sa.String(40), nullable=True),
        sa.Column("match_object_id", sa.String(40), nullable=True),
        sa.Column("spec_comparisons", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=False),
        sa.Column("checked_by", sa.String(40), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "check_id", name="pk_sourcing_ladder_checks"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "case_id",
            "sequence_number",
            name="uq_sourcing_ladder_checks_sequence",
        ),
        sa.UniqueConstraint(
            "tenant_id", "case_id", "rung", name="uq_sourcing_ladder_checks_rung"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["sourcing_cases.tenant_id", "sourcing_cases.case_id"],
            name="fk_sourcing_ladder_checks_case",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "sequence_number = rung AND rung BETWEEN 1 AND 7",
            name="ck_sourcing_ladder_checks_order",
        ),
        sa.CheckConstraint(
            "outcome IN ('no_qualified_supply','qualified_supply_found')",
            name="ck_sourcing_ladder_checks_outcome",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(input_snapshot) = 'object'",
            name="ck_sourcing_ladder_checks_input_json",
        ),
        sa.CheckConstraint(
            "input_snapshot_hash ~ '^[0-9a-f]{64}$'",
            name="ck_sourcing_ladder_checks_input_hash",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(spec_comparisons) = 'array'",
            name="ck_sourcing_ladder_checks_comparisons_json",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(evidence_refs) = 'array'",
            name="ck_sourcing_ladder_checks_evidence_json",
        ),
        sa.CheckConstraint(
            "(match_object_type IS NULL) = (match_object_id IS NULL) AND "
            "btrim(conclusion) <> '' AND btrim(checked_by) <> ''",
            name="ck_sourcing_ladder_checks_core",
        ),
    )

    op.create_table(
        "sourcing_public_plans",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("plan_id", sa.String(40), nullable=False),
        sa.Column("case_id", sa.String(40), nullable=False),
        sa.Column("target_countries", postgresql.JSONB(), nullable=False),
        sa.Column("product_category", sa.String(100), nullable=False),
        sa.Column("queries", postgresql.JSONB(), nullable=False),
        sa.Column("max_search_queries", sa.Integer(), nullable=False),
        sa.Column("max_pages_read", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("search_depth", sa.String(16), nullable=False),
        sa.Column("usage_credits_remaining", sa.BigInteger(), nullable=False),
        sa.Column("worst_case_credits", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("expected_case_version", sa.Integer(), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("confirmed_by", sa.String(40), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("authorized_plan_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "plan_id", name="pk_sourcing_public_plans"
        ),
        sa.UniqueConstraint(
            "tenant_id", "case_id", "plan_id", name="uq_sourcing_public_plans_case_plan"
        ),
        sa.UniqueConstraint(
            "tenant_id", "case_id", "version", name="uq_sourcing_public_plans_version"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["sourcing_cases.tenant_id", "sourcing_cases.case_id"],
            name="fk_sourcing_public_plans_case",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(target_countries) = 'array' AND jsonb_array_length(target_countries) > 0",
            name="ck_sourcing_public_plans_countries_json",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(queries) = 'array' AND jsonb_array_length(queries) > 0 "
            'AND NOT jsonb_path_exists(queries, \'$[*] ? (@.type() != "object" || '
            '!exists(@.query_text) || @.query_text.type() != "string" || '
            '!exists(@.target_country) || @.target_country.type() != "string")\')',
            name="ck_sourcing_public_plans_queries_json",
        ),
        sa.CheckConstraint(
            "max_search_queries >= jsonb_array_length(queries) AND max_pages_read >= 1",
            name="ck_sourcing_public_plans_limits",
        ),
        sa.CheckConstraint(
            "usage_credits_remaining >= 0 AND worst_case_credits >= 1",
            name="ck_sourcing_public_plans_credits",
        ),
        sa.CheckConstraint(
            "provider = 'tavily' AND search_depth = 'basic'",
            name="ck_sourcing_public_plans_provider",
        ),
        sa.CheckConstraint(
            f"status IN ({_PLAN_STATES})", name="ck_sourcing_public_plans_status"
        ),
        sa.CheckConstraint(
            "version >= 1 AND expected_case_version >= 1",
            name="ck_sourcing_public_plans_versions",
        ),
        sa.CheckConstraint(
            "plan_hash ~ '^[0-9a-f]{64}$' AND (authorized_plan_hash IS NULL OR authorized_plan_hash ~ '^[0-9a-f]{64}$')",
            name="ck_sourcing_public_plans_hashes",
        ),
        sa.CheckConstraint(
            "(confirmed_by IS NULL AND confirmed_at IS NULL AND authorized_plan_hash IS NULL "
            "AND status = 'pending_confirmation') OR "
            "(confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL "
            "AND authorized_plan_hash = plan_hash AND status <> 'pending_confirmation')",
            name="ck_sourcing_public_plans_confirmation",
        ),
    )
    op.create_foreign_key(
        "fk_sourcing_cases_active_plan",
        "sourcing_cases",
        "sourcing_public_plans",
        ["tenant_id", "case_id", "active_search_plan_id"],
        ["tenant_id", "case_id", "plan_id"],
        ondelete="RESTRICT",
    )

    op.create_table(
        "sourcing_candidates",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("candidate_id", sa.String(40), nullable=False),
        sa.Column("case_id", sa.String(40), nullable=False),
        sa.Column("supplier_name", sa.String(300), nullable=False),
        sa.Column("source_platform", sa.String(100), nullable=True),
        sa.Column("product_title", sa.String(500), nullable=False),
        sa.Column("observed_facts", postgresql.JSONB(), nullable=False),
        sa.Column("supplier_claims", postgresql.JSONB(), nullable=False),
        sa.Column("match_inferences", postgresql.JSONB(), nullable=False),
        sa.Column("verified_specs", postgresql.JSONB(), nullable=False),
        sa.Column("indicative_price_tiers", postgresql.JSONB(), nullable=False),
        sa.Column("moq", sa.Integer(), nullable=True),
        sa.Column("price_unit", sa.String(50), nullable=True),
        sa.Column("currency", sa.CHAR(3), nullable=True),
        sa.Column("match_explanation", postgresql.JSONB(), nullable=True),
        sa.Column(
            "rejected", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("rejection_reasons", postgresql.JSONB(), nullable=False),
        sa.Column("verified_by", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "candidate_id", name="pk_sourcing_candidates"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "case_id",
            "candidate_id",
            name="uq_sourcing_candidates_case_candidate",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["sourcing_cases.tenant_id", "sourcing_cases.case_id"],
            name="fk_sourcing_candidates_case",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(observed_facts) = 'object'",
            name="ck_sourcing_candidates_observed_json",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(supplier_claims) = 'object'",
            name="ck_sourcing_candidates_claims_json",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(match_inferences) = 'object'",
            name="ck_sourcing_candidates_inferences_json",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(verified_specs) = 'array'",
            name="ck_sourcing_candidates_specs_json",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(indicative_price_tiers) = 'array' "
            "AND jsonb_array_length(indicative_price_tiers) > 0 AND "
            "NOT jsonb_path_exists(indicative_price_tiers, "
            '\'$[*] ? (@.type() != "object" || !exists(@.minimum_quantity) '
            '|| @.minimum_quantity.type() != "number" || !exists(@.amount) '
            '|| @.amount.type() != "string" || !exists(@.currency) '
            '|| @.currency.type() != "string" || !exists(@.unit) '
            '|| @.unit.type() != "string" || !exists(@.provenance) '
            '|| @.provenance.type() != "object" || !exists(@.evidence_ref) '
            '|| @.evidence_ref.type() != "string")\')',
            name="ck_sourcing_candidates_price_tiers_json",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(rejection_reasons) = 'array'",
            name="ck_sourcing_candidates_rejections_json",
        ),
        sa.CheckConstraint(
            "match_explanation IS NULL OR jsonb_typeof(match_explanation) = 'object'",
            name="ck_sourcing_candidates_match_json",
        ),
        sa.CheckConstraint(
            "moq IS NULL OR moq >= 1", name="ck_sourcing_candidates_moq"
        ),
        sa.CheckConstraint(
            "currency IS NULL OR currency ~ '^[A-Z]{3}$'",
            name="ck_sourcing_candidates_currency",
        ),
        sa.CheckConstraint(
            "btrim(supplier_name) <> '' AND btrim(product_title) <> ''",
            name="ck_sourcing_candidates_core_nonblank",
        ),
    )
    op.create_index(
        "ix_sourcing_candidates_case_created",
        "sourcing_candidates",
        ["tenant_id", "case_id", "created_at", "candidate_id"],
    )

    op.create_table(
        "sourcing_candidate_evidence",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("candidate_id", sa.String(40), nullable=False),
        sa.Column("artifact_id", sa.String(32), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "candidate_id",
            "artifact_id",
            name="pk_sourcing_candidate_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "candidate_id"],
            ["sourcing_candidates.tenant_id", "sourcing_candidates.candidate_id"],
            name="fk_sourcing_candidate_evidence_candidate",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "artifact_id"],
            ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"],
            name="fk_sourcing_candidate_evidence_artifact",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$' AND url ~ '^https?://'",
            name="ck_sourcing_candidate_evidence_locator",
        ),
    )
    op.create_index(
        "ix_sourcing_candidate_evidence_order",
        "sourcing_candidate_evidence",
        ["tenant_id", "candidate_id", "observed_at", "artifact_id"],
    )

    op.create_table(
        "sourcing_supply_options",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("option_id", sa.String(40), nullable=False),
        sa.Column("case_id", sa.String(40), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("product_id", sa.String(40), nullable=False),
        sa.Column("supplier_candidate_id", sa.String(40), nullable=True),
        sa.Column("is_qualified", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "option_id", name="pk_sourcing_supply_options"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "case_id",
            "option_id",
            name="uq_sourcing_supply_options_case_option",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "case_id",
            "option_id",
            "supplier_candidate_id",
            name="uq_sourcing_supply_options_candidate_path",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "case_id",
            "supplier_candidate_id",
            name="uq_sourcing_supply_options_supplier_candidate",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["sourcing_cases.tenant_id", "sourcing_cases.case_id"],
            name="fk_sourcing_supply_options_case",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id", "supplier_candidate_id"],
            [
                "sourcing_candidates.tenant_id",
                "sourcing_candidates.case_id",
                "sourcing_candidates.candidate_id",
            ],
            name="fk_sourcing_supply_options_candidate",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "(source = 'existing_product' AND supplier_candidate_id IS NULL) OR "
            "(source = 'supplier_candidate' AND supplier_candidate_id IS NOT NULL)",
            name="ck_sourcing_supply_options_source",
        ),
    )
    op.create_index(
        "uq_sourcing_supply_options_existing_product",
        "sourcing_supply_options",
        ["tenant_id", "case_id", "product_id"],
        unique=True,
        postgresql_where=sa.text("source = 'existing_product'"),
    )

    op.create_table(
        "sourcing_reviews",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("review_id", sa.String(40), nullable=False),
        sa.Column("case_id", sa.String(40), nullable=False),
        sa.Column("primary_option_id", sa.String(40), nullable=False),
        sa.Column("primary_selection", postgresql.JSONB(), nullable=False),
        sa.Column("alternate_option_ids", postgresql.JSONB(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("expected_case_version", sa.Integer(), nullable=False),
        sa.Column("submitted_by", sa.String(40), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_by", sa.String(40), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "review_id", name="pk_sourcing_reviews"),
        sa.UniqueConstraint("tenant_id", "case_id", name="uq_sourcing_reviews_case"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id", "primary_option_id"],
            [
                "sourcing_supply_options.tenant_id",
                "sourcing_supply_options.case_id",
                "sourcing_supply_options.option_id",
            ],
            name="fk_sourcing_reviews_primary_option",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(primary_selection) = 'object'",
            name="ck_sourcing_reviews_primary_json",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(alternate_option_ids) = 'array' AND jsonb_array_length(alternate_option_ids) <= 2 "
            "AND NOT jsonb_path_exists(alternate_option_ids, '$[*] ? (@.type() != \"string\")')",
            name="ck_sourcing_reviews_alternates_json",
        ),
        sa.CheckConstraint(
            "expected_case_version >= 1 AND btrim(reason) <> '' AND btrim(submitted_by) <> ''",
            name="ck_sourcing_reviews_core",
        ),
        sa.CheckConstraint(
            "(confirmed_by IS NULL) = (confirmed_at IS NULL)",
            name="ck_sourcing_reviews_confirmation",
        ),
    )

    op.create_table(
        "sourcing_search_executions",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("execution_id", sa.String(40), nullable=False),
        sa.Column("case_id", sa.String(40), nullable=False),
        sa.Column("plan_id", sa.String(40), nullable=False),
        sa.Column("run_id", sa.String(40), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("query_index", sa.Integer(), nullable=False),
        sa.Column("request_key", sa.String(64), nullable=False),
        sa.Column("query_hash", sa.String(64), nullable=False),
        sa.Column("locator_results", postgresql.JSONB(), nullable=False),
        sa.Column("provider_status", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id", "execution_id", name="pk_sourcing_search_executions"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "run_id",
            "plan_hash",
            "query_index",
            name="uq_sourcing_search_executions_query",
        ),
        sa.UniqueConstraint(
            "tenant_id", "request_key", name="uq_sourcing_search_executions_request"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id", "plan_id"],
            [
                "sourcing_public_plans.tenant_id",
                "sourcing_public_plans.case_id",
                "sourcing_public_plans.plan_id",
            ],
            name="fk_sourcing_search_executions_plan",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["workflow_runs.tenant_id", "workflow_runs.run_id"],
            name="fk_sourcing_search_executions_run",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "plan_hash ~ '^[0-9a-f]{64}$' AND request_key ~ '^[0-9a-f]{64}$'",
            name="ck_sourcing_search_executions_hashes",
        ),
        sa.CheckConstraint(
            "query_index >= 0 AND query_hash ~ '^[0-9a-f]{64}$'",
            name="ck_sourcing_search_executions_query",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(locator_results) = 'array'",
            name="ck_sourcing_search_executions_locators_json",
        ),
        sa.CheckConstraint(
            "provider_status IN ('succeeded','no_results','uncertain','failed')",
            name="ck_sourcing_search_executions_status",
        ),
        sa.CheckConstraint(
            "(provider_status IN ('succeeded','no_results')) = (completed_at IS NOT NULL)",
            name="ck_sourcing_search_executions_completed",
        ),
    )

    op.create_table(
        "sourcing_page_attempts",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("case_id", sa.String(40), nullable=False),
        sa.Column("plan_id", sa.String(40), nullable=False),
        sa.Column("run_id", sa.String(40), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("query_index", sa.Integer(), nullable=False),
        sa.Column("result_index", sa.Integer(), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "run_id",
            "plan_hash",
            "query_index",
            "result_index",
            name="pk_sourcing_page_attempts",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id", "plan_id"],
            [
                "sourcing_public_plans.tenant_id",
                "sourcing_public_plans.case_id",
                "sourcing_public_plans.plan_id",
            ],
            name="fk_sourcing_page_attempts_plan",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["workflow_runs.tenant_id", "workflow_runs.run_id"],
            name="fk_sourcing_page_attempts_run",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "plan_hash ~ '^[0-9a-f]{64}$' AND query_index >= 0 AND result_index >= 0",
            name="ck_sourcing_page_attempts_binding",
        ),
    )

    op.create_table(
        "sourcing_candidate_drafts",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("draft_id", sa.String(40), nullable=False),
        sa.Column("case_id", sa.String(40), nullable=False),
        sa.Column("run_id", sa.String(40), nullable=False),
        sa.Column("plan_id", sa.String(40), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("query_index", sa.Integer(), nullable=False),
        sa.Column("result_index", sa.Integer(), nullable=False),
        sa.Column("source_key", sa.String(64), nullable=False),
        sa.Column("supplier_name", sa.String(300), nullable=True),
        sa.Column("product_title", sa.String(500), nullable=True),
        sa.Column("specs", postgresql.JSONB(), nullable=False),
        sa.Column("moq", sa.Integer(), nullable=True),
        sa.Column("indicative_price_tiers", postgresql.JSONB(), nullable=False),
        sa.Column("rejection_codes", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_url", sa.Text(), nullable=False),
        sa.Column("evidence_observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("evidence_artifact_ref", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "draft_id", name="pk_sourcing_candidate_drafts"
        ),
        sa.UniqueConstraint(
            "tenant_id", "source_key", name="uq_sourcing_candidate_drafts_source"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "case_id",
            "run_id",
            "plan_hash",
            "query_index",
            "result_index",
            "evidence_artifact_ref",
            name="uq_sourcing_candidate_drafts_location",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "case_id", "plan_id"],
            [
                "sourcing_public_plans.tenant_id",
                "sourcing_public_plans.case_id",
                "sourcing_public_plans.plan_id",
            ],
            name="fk_sourcing_candidate_drafts_plan",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "run_id"],
            ["workflow_runs.tenant_id", "workflow_runs.run_id"],
            name="fk_sourcing_candidate_drafts_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "evidence_artifact_ref"],
            ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"],
            name="fk_sourcing_candidate_drafts_artifact",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "plan_hash ~ '^[0-9a-f]{64}$' AND source_key ~ '^[0-9a-f]{64}$' AND evidence_hash ~ '^[0-9a-f]{64}$'",
            name="ck_sourcing_candidate_drafts_hashes",
        ),
        sa.CheckConstraint(
            "query_index >= 0 AND result_index >= 0",
            name="ck_sourcing_candidate_drafts_indexes",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(specs) = 'array' AND jsonb_typeof(indicative_price_tiers) = 'array' AND jsonb_typeof(rejection_codes) = 'array'",
            name="ck_sourcing_candidate_drafts_json",
        ),
    )

    op.create_table(
        "sourcing_search_reconciliations",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("reconciliation_id", sa.String(40), nullable=False),
        sa.Column("execution_id", sa.String(40), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("provider_usage_artifact_ref", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reconciled_by", sa.String(40), nullable=True),
        sa.Column("reconciled_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id", "reconciliation_id", name="pk_sourcing_search_reconciliations"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "execution_id",
            name="uq_sourcing_search_reconciliations_execution",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "execution_id"],
            [
                "sourcing_search_executions.tenant_id",
                "sourcing_search_executions.execution_id",
            ],
            name="fk_sourcing_search_reconciliations_execution",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "provider_usage_artifact_ref"],
            ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"],
            name="fk_sourcing_search_reconciliations_artifact",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('required','confirmed_consumed','confirmed_not_consumed')",
            name="ck_sourcing_search_reconciliations_status",
        ),
        sa.CheckConstraint(
            "provider_usage_artifact_ref ~ '^art_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_sourcing_search_reconciliations_artifact",
        ),
        sa.CheckConstraint(
            "btrim(reason) <> ''", name="ck_sourcing_search_reconciliations_reason"
        ),
        sa.CheckConstraint(
            "(status = 'required' AND reconciled_by IS NULL AND reconciled_at IS NULL) OR "
            "(status <> 'required' AND reconciled_by IS NOT NULL AND reconciled_at IS NOT NULL)",
            name="ck_sourcing_search_reconciliations_resolution",
        ),
    )

    op.execute(
        "CREATE FUNCTION guard_sourcing_ladder_check() RETURNS trigger AS $$ "
        "BEGIN IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'immutable sourcing ladder check' USING ERRCODE='23514'; END IF; "
        "IF NEW.rung > 1 AND NOT EXISTS (SELECT 1 FROM sourcing_ladder_checks WHERE tenant_id=NEW.tenant_id AND case_id=NEW.case_id AND rung=NEW.rung-1) "
        "THEN RAISE EXCEPTION 'sourcing ladder rung skipped' USING ERRCODE='23514'; END IF; "
        "IF EXISTS (SELECT 1 FROM sourcing_ladder_checks WHERE tenant_id=NEW.tenant_id AND case_id=NEW.case_id AND rung < NEW.rung AND outcome='qualified_supply_found') "
        "THEN RAISE EXCEPTION 'qualified sourcing ladder hit is terminal' USING ERRCODE='23514'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_sourcing_ladder_check BEFORE INSERT OR UPDATE OR DELETE ON sourcing_ladder_checks "
        "FOR EACH ROW EXECUTE FUNCTION guard_sourcing_ladder_check()"
    )
    op.execute(
        "CREATE FUNCTION guard_sourcing_review_options() RETURNS trigger AS $$ DECLARE alternate jsonb; BEGIN "
        "FOR alternate IN SELECT value FROM jsonb_array_elements(NEW.alternate_option_ids) LOOP "
        "IF alternate #>> '{}' = NEW.primary_option_id OR NOT EXISTS (SELECT 1 FROM sourcing_supply_options "
        "WHERE tenant_id=NEW.tenant_id AND case_id=NEW.case_id AND option_id=alternate #>> '{}') "
        "THEN RAISE EXCEPTION 'invalid sourcing alternate option' USING ERRCODE='23514'; END IF; END LOOP; "
        "IF (SELECT count(*) FROM jsonb_array_elements_text(NEW.alternate_option_ids)) <> "
        "(SELECT count(DISTINCT value) FROM jsonb_array_elements_text(NEW.alternate_option_ids)) "
        "THEN RAISE EXCEPTION 'duplicate sourcing alternate option' USING ERRCODE='23514'; END IF; RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_sourcing_review_options BEFORE INSERT OR UPDATE ON sourcing_reviews "
        "FOR EACH ROW EXECUTE FUNCTION guard_sourcing_review_options()"
    )
    op.execute(
        "CREATE FUNCTION guard_sourcing_reconciliation_audit() RETURNS trigger AS $$ BEGIN "
        "IF TG_OP <> 'INSERT' THEN RAISE EXCEPTION 'immutable sourcing reconciliation audit' USING ERRCODE='23514'; END IF; "
        "RETURN NEW; END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_sourcing_reconciliation_audit BEFORE UPDATE OR DELETE ON sourcing_search_reconciliations "
        "FOR EACH ROW EXECUTE FUNCTION guard_sourcing_reconciliation_audit()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_sourcing_reconciliation_audit ON sourcing_search_reconciliations"
    )
    op.execute("DROP FUNCTION guard_sourcing_reconciliation_audit()")
    op.execute("DROP TRIGGER trg_sourcing_review_options ON sourcing_reviews")
    op.execute("DROP FUNCTION guard_sourcing_review_options()")
    op.execute("DROP TRIGGER trg_sourcing_ladder_check ON sourcing_ladder_checks")
    op.execute("DROP FUNCTION guard_sourcing_ladder_check()")
    op.drop_table("sourcing_search_reconciliations")
    op.drop_table("sourcing_candidate_drafts")
    op.drop_table("sourcing_page_attempts")
    op.drop_table("sourcing_search_executions")
    op.drop_table("sourcing_reviews")
    op.drop_index(
        "uq_sourcing_supply_options_existing_product",
        table_name="sourcing_supply_options",
    )
    op.drop_table("sourcing_supply_options")
    op.drop_table("sourcing_candidate_evidence")
    op.drop_table("sourcing_candidates")
    op.drop_constraint(
        "fk_sourcing_cases_active_plan", "sourcing_cases", type_="foreignkey"
    )
    op.drop_table("sourcing_public_plans")
    op.drop_table("sourcing_ladder_checks")
    op.drop_index("ix_sourcing_cases_queue", table_name="sourcing_cases")
    op.drop_index("uq_sourcing_cases_active_need", table_name="sourcing_cases")
    op.drop_table("sourcing_cases")
