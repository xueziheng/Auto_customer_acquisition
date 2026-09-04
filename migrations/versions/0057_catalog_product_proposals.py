"""Tenant-safe Catalog Product Proposal persistence skeleton.

Revision ID: 0057
Revises: 0056
Create Date: 2026-09-04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None

_QUOTE_MARKED = """(lower(btrim(coalesce(change_set_ref,''))) LIKE 'quote:%' OR
    lower(btrim(coalesce(proposed_change->>'schema_version',''))) LIKE 'quote-approval%')"""
_CATALOG_MARKED = """(lower(btrim(coalesce(change_set_ref,''))) LIKE 'catalog-policy:%' OR
    lower(btrim(coalesce(change_set_ref,''))) LIKE 'catalog-cultivation:%' OR
    lower(btrim(coalesce(proposed_change->>'schema_version',''))) LIKE 'catalog-policy%' OR
    lower(btrim(coalesce(proposed_change->>'schema_version',''))) LIKE 'catalog-cultivation%')"""

_QUOTE_BRANCH = """(contract_namespace='quote-approval-v1' AND request_hash ~ '^[0-9a-f]{64}$'
  AND expires_at_limit IS NOT NULL AND expires_at <= expires_at_limit
  AND proposed_change->>'schema_version'='quote-approval-v1'
  AND proposed_change->>'tenant_id'=tenant_id
  AND proposed_change->>'approval_type'=approval_type
  AND proposed_change->>'prepared_by'=proposed_by_employee
  AND proposed_change->>'submitted_owner_id'=owner_employee
  AND jsonb_typeof(proposed_change->'quote_version')='number'
  AND (proposed_change->>'quote_version') ~ '^[1-9][0-9]*$'
  AND proposed_change->>'quote_id' ~ '^quo_[0-9A-HJKMNP-TV-Z]{26}$'
  AND proposed_change->>'content_hash' ~ '^[0-9a-f]{64}$'
  AND approval_type IN ('quote_send','margin_floor_override','discount','delivery_commitment','payment_terms','certification_commitment')
  AND change_set_ref='quote:'||(proposed_change->>'quote_id')||':'||
      (proposed_change->>'content_hash')||':'||approval_type)"""

_CATALOG_POLICY_BRANCH = """(contract_namespace='catalog-policy-v1'
  AND request_hash ~ '^[0-9a-f]{64}$'
  AND expires_at_limit IS NOT NULL AND expires_at <= expires_at_limit
  AND approval_type='catalog_proposal_policy_change'
  AND proposed_change->>'schema_version'='catalog-policy-v1'
  AND proposed_change->>'tenant_id'=tenant_id
  AND proposed_change->>'approval_type'=approval_type
  AND proposed_change->>'policy_version_id' ~ '^cpv_'
  AND proposed_change->>'content_hash' ~ '^[0-9a-f]{64}$'
  AND proposed_change->>'request_hash'=request_hash
  AND change_set_ref='catalog-policy:'||(proposed_change->>'policy_version_id')||':'||
      (proposed_change->>'content_hash'))"""

_CATALOG_CULTIVATION_BRANCH = """(contract_namespace='catalog-cultivation-v1'
  AND request_hash ~ '^[0-9a-f]{64}$'
  AND expires_at_limit IS NOT NULL AND expires_at <= expires_at_limit
  AND approval_type='catalog_product_cultivation'
  AND proposed_change->>'schema_version'='catalog-cultivation-v1'
  AND proposed_change->>'tenant_id'=tenant_id
  AND proposed_change->>'approval_type'=approval_type
  AND proposed_change->>'proposal_id' ~ '^cpr_'
  AND proposed_change->>'policy_version_id' ~ '^cpv_'
  AND proposed_change->>'facts_hash' ~ '^[0-9a-f]{64}$'
  AND proposed_change->>'request_hash'=request_hash
  AND change_set_ref='catalog-cultivation:'||(proposed_change->>'proposal_id')||':'||
      (proposed_change->>'policy_version_id')||':'||(proposed_change->>'facts_hash'))"""

_LEGACY_APPROVAL_CHECK = f"""
  (contract_namespace IS NULL AND request_hash IS NULL AND NOT {_QUOTE_MARKED}) OR
  coalesce({_QUOTE_BRANCH},false)
"""

_CATALOG_APPROVAL_CHECK = f"""
  (contract_namespace IS NULL AND request_hash IS NULL AND NOT {_QUOTE_MARKED}) OR
  coalesce({_QUOTE_BRANCH},false) OR
  coalesce({_CATALOG_POLICY_BRANCH},false) OR
  coalesce({_CATALOG_CULTIVATION_BRANCH},false)
"""


def _create_guards() -> None:
    op.execute(
        f"""
        CREATE FUNCTION guard_catalog_approval_namespace() RETURNS trigger AS $$
        BEGIN
          IF TG_OP='INSERT' AND NEW.contract_namespace IS NULL
             AND NEW.request_hash IS NULL AND {_CATALOG_MARKED.replace('change_set_ref', 'NEW.change_set_ref').replace('proposed_change', 'NEW.proposed_change')}
          THEN RAISE EXCEPTION 'catalog approval namespace required'; END IF;
          RETURN NEW;
        END; $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_catalog_approval_namespace
        BEFORE INSERT ON approval_packages
        FOR EACH ROW EXECUTE FUNCTION guard_catalog_approval_namespace();
        """
    )
    op.execute(
        """
        CREATE FUNCTION guard_catalog_policy_version() RETURNS trigger AS $$
        DECLARE approval approval_packages%ROWTYPE;
        BEGIN
          IF TG_OP='DELETE' THEN RAISE EXCEPTION 'catalog policy version is immutable'; END IF;
          IF TG_OP='INSERT' THEN
            IF NEW.state<>'pending_approval' OR NEW.approval_id IS NOT NULL
               OR NEW.activated_at IS NOT NULL OR NEW.terminal_at IS NOT NULL
            THEN RAISE EXCEPTION 'catalog policy invalid initial state'; END IF;
            RETURN NEW;
          END IF;
          IF (to_jsonb(OLD)-ARRAY['approval_id','state','activated_at','terminal_at'])
             IS DISTINCT FROM
             (to_jsonb(NEW)-ARRAY['approval_id','state','activated_at','terminal_at'])
             OR (OLD.approval_id IS NOT NULL AND NEW.approval_id IS DISTINCT FROM OLD.approval_id)
          THEN RAISE EXCEPTION 'catalog policy immutable fields changed'; END IF;
          IF NEW.approval_id IS NOT NULL THEN
            SELECT * INTO approval FROM approval_packages
            WHERE tenant_id=NEW.tenant_id AND approval_id=NEW.approval_id;
            IF NOT FOUND
               OR approval.contract_namespace IS DISTINCT FROM 'catalog-policy-v1'
               OR approval.approval_type IS DISTINCT FROM 'catalog_proposal_policy_change'
               OR approval.proposed_change->>'tenant_id' IS DISTINCT FROM NEW.tenant_id
               OR approval.proposed_change->>'policy_version_id' IS DISTINCT FROM NEW.policy_version_id
               OR approval.proposed_change->>'content_hash' IS DISTINCT FROM NEW.content_hash
               OR approval.proposed_change->>'request_hash' IS DISTINCT FROM approval.request_hash
               OR approval.change_set_ref IS DISTINCT FROM
                  'catalog-policy:'||NEW.policy_version_id||':'||NEW.content_hash
            THEN RAISE EXCEPTION 'catalog policy approval binding rejected'; END IF;
          END IF;
          IF OLD.state='pending_approval' AND NEW.state='pending_approval'
             AND OLD.approval_id IS NULL AND NEW.approval_id IS NOT NULL
             AND approval.state IN ('pending','approved','rejected','expired')
             AND NEW.activated_at IS NULL AND NEW.terminal_at IS NULL THEN RETURN NEW; END IF;
          IF OLD.state='pending_approval' AND NEW.state='active'
             AND NEW.approval_id IS NOT NULL AND NEW.activated_at IS NOT NULL
             AND approval.state='approved'
             AND NEW.terminal_at IS NULL THEN RETURN NEW; END IF;
          IF OLD.state='pending_approval' AND NEW.state='rejected'
             AND NEW.approval_id IS NOT NULL AND NEW.activated_at IS NULL
             AND approval.state='rejected'
             AND NEW.terminal_at IS NOT NULL THEN RETURN NEW; END IF;
          IF OLD.state='pending_approval' AND NEW.state='expired'
             AND NEW.approval_id IS NOT NULL AND NEW.activated_at IS NULL
             AND approval.state='expired'
             AND NEW.terminal_at IS NOT NULL THEN RETURN NEW; END IF;
          IF OLD.state='pending_approval' AND NEW.state='stale'
             AND NEW.approval_id IS NOT NULL AND NEW.activated_at IS NULL
             AND NEW.terminal_at IS NOT NULL THEN RETURN NEW; END IF;
          IF OLD.state='active' AND NEW.state='superseded'
             AND NEW.approval_id=OLD.approval_id
             AND approval.state IN ('approved','applied')
             AND NEW.activated_at=OLD.activated_at AND NEW.terminal_at IS NOT NULL
          THEN RETURN NEW; END IF;
          RAISE EXCEPTION 'catalog policy invalid transition';
        END; $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_catalog_policy_version_guard
        BEFORE INSERT OR UPDATE OR DELETE ON catalog_proposal_policy_versions
        FOR EACH ROW EXECUTE FUNCTION guard_catalog_policy_version();
        """
    )
    op.execute(
        """
        CREATE FUNCTION guard_catalog_evaluation() RETURNS trigger AS $$
        DECLARE policy_state text;
        BEGIN
          IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'catalog evaluation is immutable'; END IF;
          SELECT state INTO policy_state FROM catalog_proposal_policy_versions
          WHERE tenant_id=NEW.tenant_id AND policy_version_id=NEW.policy_version_id;
          IF policy_state IS DISTINCT FROM 'active'
          THEN RAISE EXCEPTION 'catalog evaluation requires active policy'; END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_catalog_evaluation_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON catalog_proposal_evaluations
        FOR EACH ROW EXECUTE FUNCTION guard_catalog_evaluation();
        """
    )
    op.execute(
        """
        CREATE FUNCTION guard_catalog_product_proposal() RETURNS trigger AS $$
        DECLARE approval approval_packages%ROWTYPE; evaluation_passed boolean;
          policy_state text;
        BEGIN
          IF TG_OP='DELETE' THEN RAISE EXCEPTION 'catalog product proposal is immutable'; END IF;
          IF TG_OP='INSERT' THEN
            IF NEW.state<>'awaiting_approval_submission' OR NEW.approval_id IS NOT NULL
               OR NEW.approval_request_hash IS NOT NULL OR NEW.updated_at<>NEW.created_at
            THEN RAISE EXCEPTION 'catalog proposal invalid initial state'; END IF;
            SELECT overall_passed INTO evaluation_passed
            FROM catalog_proposal_evaluations
            WHERE tenant_id=NEW.tenant_id AND evaluation_id=NEW.evaluation_id
              AND cluster_id=NEW.cluster_id
              AND policy_version_id=NEW.policy_version_id
              AND facts_hash=NEW.facts_hash;
            SELECT state INTO policy_state FROM catalog_proposal_policy_versions
            WHERE tenant_id=NEW.tenant_id AND policy_version_id=NEW.policy_version_id;
            IF evaluation_passed IS DISTINCT FROM true OR policy_state IS DISTINCT FROM 'active'
            THEN RAISE EXCEPTION 'catalog proposal prerequisites rejected'; END IF;
            RETURN NEW;
          END IF;
          IF (to_jsonb(OLD)-ARRAY['approval_id','approval_request_hash','state','updated_at'])
             IS DISTINCT FROM
             (to_jsonb(NEW)-ARRAY['approval_id','approval_request_hash','state','updated_at'])
             OR (OLD.approval_id IS NOT NULL AND NEW.approval_id IS DISTINCT FROM OLD.approval_id)
             OR (OLD.approval_request_hash IS NOT NULL AND NEW.approval_request_hash IS DISTINCT FROM OLD.approval_request_hash)
             OR NEW.updated_at < OLD.updated_at
          THEN RAISE EXCEPTION 'catalog proposal immutable fields changed'; END IF;
          IF NEW.approval_id IS NOT NULL THEN
            SELECT * INTO approval FROM approval_packages
            WHERE tenant_id=NEW.tenant_id AND approval_id=NEW.approval_id;
            IF NOT FOUND
               OR approval.contract_namespace IS DISTINCT FROM 'catalog-cultivation-v1'
               OR approval.approval_type IS DISTINCT FROM 'catalog_product_cultivation'
               OR approval.request_hash IS DISTINCT FROM NEW.approval_request_hash
               OR approval.proposed_change->>'request_hash' IS DISTINCT FROM NEW.approval_request_hash
               OR approval.proposed_change->>'tenant_id' IS DISTINCT FROM NEW.tenant_id
               OR approval.proposed_change->>'proposal_id' IS DISTINCT FROM NEW.proposal_id
               OR approval.proposed_change->>'policy_version_id' IS DISTINCT FROM NEW.policy_version_id
               OR approval.proposed_change->>'facts_hash' IS DISTINCT FROM NEW.facts_hash
               OR approval.change_set_ref IS DISTINCT FROM
                  'catalog-cultivation:'||NEW.proposal_id||':'||NEW.policy_version_id||':'||NEW.facts_hash
            THEN RAISE EXCEPTION 'catalog proposal approval binding rejected'; END IF;
          END IF;
          IF OLD.state='awaiting_approval_submission' AND NEW.state='pending_review'
             AND OLD.approval_id IS NULL AND OLD.approval_request_hash IS NULL
             AND NEW.approval_id IS NOT NULL AND NEW.approval_request_hash IS NOT NULL
             AND approval.state IN ('pending','approved','rejected','expired')
          THEN RETURN NEW; END IF;
          IF OLD.state='pending_review' AND NEW.state='cultivation_queued'
             AND NEW.approval_id=OLD.approval_id
             AND NEW.approval_request_hash=OLD.approval_request_hash
             AND approval.state='approved'
          THEN RETURN NEW; END IF;
          IF OLD.state='pending_review' AND NEW.state='rejected'
             AND NEW.approval_id=OLD.approval_id
             AND NEW.approval_request_hash=OLD.approval_request_hash
             AND approval.state='rejected'
          THEN RETURN NEW; END IF;
          IF OLD.state='pending_review' AND NEW.state='expired'
             AND NEW.approval_id=OLD.approval_id
             AND NEW.approval_request_hash=OLD.approval_request_hash
             AND approval.state='expired'
          THEN RETURN NEW; END IF;
          IF OLD.state='pending_review' AND NEW.state='stale'
             AND NEW.approval_id=OLD.approval_id
             AND NEW.approval_request_hash=OLD.approval_request_hash
          THEN RETURN NEW; END IF;
          RAISE EXCEPTION 'catalog proposal invalid transition';
        END; $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_catalog_product_proposal_guard
        BEFORE INSERT OR UPDATE OR DELETE ON catalog_product_proposals
        FOR EACH ROW EXECUTE FUNCTION guard_catalog_product_proposal();
        """
    )
    op.execute(
        """
        CREATE FUNCTION guard_catalog_cultivation_case() RETURNS trigger AS $$
        DECLARE proposal_state text; proposal_approval_id text;
          proposal_request_hash text; approval approval_packages%ROWTYPE;
        BEGIN
          IF TG_OP<>'INSERT' THEN RAISE EXCEPTION 'catalog cultivation case is immutable'; END IF;
          IF NEW.state<>'queued' THEN RAISE EXCEPTION 'catalog cultivation invalid initial state'; END IF;
          SELECT state,approval_id,approval_request_hash
          INTO proposal_state,proposal_approval_id,proposal_request_hash
          FROM catalog_product_proposals
          WHERE tenant_id=NEW.tenant_id AND proposal_id=NEW.proposal_id
            AND cluster_id=NEW.cluster_id AND policy_version_id=NEW.policy_version_id
            AND facts_hash=NEW.facts_hash;
          SELECT * INTO approval FROM approval_packages
          WHERE tenant_id=NEW.tenant_id AND approval_id=NEW.approval_id;
          IF proposal_state IS DISTINCT FROM 'cultivation_queued'
             OR proposal_approval_id IS DISTINCT FROM NEW.approval_id
             OR approval.contract_namespace IS DISTINCT FROM 'catalog-cultivation-v1'
             OR approval.approval_type IS DISTINCT FROM 'catalog_product_cultivation'
             OR approval.state IS DISTINCT FROM 'approved'
             OR approval.request_hash IS DISTINCT FROM proposal_request_hash
             OR approval.proposed_change->>'request_hash' IS DISTINCT FROM proposal_request_hash
             OR approval.proposed_change->>'tenant_id' IS DISTINCT FROM NEW.tenant_id
             OR approval.proposed_change->>'proposal_id' IS DISTINCT FROM NEW.proposal_id
             OR approval.proposed_change->>'policy_version_id' IS DISTINCT FROM NEW.policy_version_id
             OR approval.proposed_change->>'facts_hash' IS DISTINCT FROM NEW.facts_hash
             OR approval.change_set_ref IS DISTINCT FROM
                'catalog-cultivation:'||NEW.proposal_id||':'||NEW.policy_version_id||':'||NEW.facts_hash
          THEN RAISE EXCEPTION 'catalog cultivation approval binding rejected'; END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_catalog_cultivation_immutable
        BEFORE INSERT OR UPDATE OR DELETE ON catalog_cultivation_cases
        FOR EACH ROW EXECUTE FUNCTION guard_catalog_cultivation_case();
        """
    )


def upgrade() -> None:
    op.add_column(
        "validated_needs",
        sa.Column("recurring_requirement", postgresql.JSONB(), nullable=True),
    )
    op.create_check_constraint(
        "ck_validated_needs_recurring_requirement_jsonb",
        "validated_needs",
        "recurring_requirement IS NULL OR jsonb_typeof(recurring_requirement)='object'",
    )
    op.drop_constraint("ck_approval_quote_contract", "approval_packages", type_="check")
    op.create_check_constraint(
        "ck_approval_quote_contract", "approval_packages", _CATALOG_APPROVAL_CHECK
    )

    op.create_table(
        "catalog_proposal_policy_versions",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("policy_version_id", sa.String(40), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("base_active_version_id", sa.String(40), nullable=True),
        sa.Column("proposed_by", sa.String(40), nullable=False),
        sa.Column("creation_key", sa.String(200), nullable=False),
        sa.Column("creation_request_hash", sa.String(64), nullable=False),
        sa.Column("approval_id", sa.String(40), nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "policy_version_id", name="pk_catalog_proposal_policy_versions"),
        sa.UniqueConstraint("tenant_id", "proposed_by", "creation_key", name="uq_catalog_policy_creation_key"),
        sa.ForeignKeyConstraint(["tenant_id", "base_active_version_id"], ["catalog_proposal_policy_versions.tenant_id", "catalog_proposal_policy_versions.policy_version_id"], name="fk_catalog_policy_base_active", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "proposed_by"], ["employees.tenant_id", "employees.employee_id"], name="fk_catalog_policy_proposer", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "approval_id"], ["approval_packages.tenant_id", "approval_packages.approval_id"], name="fk_catalog_policy_approval", ondelete="RESTRICT"),
        sa.CheckConstraint("btrim(tenant_id)<>'' AND policy_version_id ~ '^cpv_' AND btrim(proposed_by)<>'' AND btrim(creation_key)<>''", name="ck_catalog_policy_core"),
        sa.CheckConstraint("jsonb_typeof(content)='object'", name="ck_catalog_policy_content_jsonb"),
        sa.CheckConstraint("content_hash ~ '^[0-9a-f]{64}$' AND creation_request_hash ~ '^[0-9a-f]{64}$'", name="ck_catalog_policy_hashes"),
        sa.CheckConstraint("state IN ('pending_approval','active','superseded','rejected','expired','stale')", name="ck_catalog_policy_state"),
        sa.CheckConstraint("(activated_at IS NULL OR activated_at>=created_at) AND (terminal_at IS NULL OR terminal_at>=created_at) AND (activated_at IS NULL OR terminal_at IS NULL OR terminal_at>=activated_at)", name="ck_catalog_policy_times"),
        sa.CheckConstraint("(state='pending_approval' AND activated_at IS NULL AND terminal_at IS NULL) OR (state='active' AND approval_id IS NOT NULL AND activated_at IS NOT NULL AND terminal_at IS NULL) OR (state='superseded' AND approval_id IS NOT NULL AND activated_at IS NOT NULL AND terminal_at IS NOT NULL) OR (state IN ('rejected','expired','stale') AND approval_id IS NOT NULL AND activated_at IS NULL AND terminal_at IS NOT NULL)", name="ck_catalog_policy_lifecycle"),
    )
    op.create_index("uq_catalog_policy_active", "catalog_proposal_policy_versions", ["tenant_id"], unique=True, postgresql_where=sa.text("state='active'"))
    op.create_index("uq_catalog_policy_approval", "catalog_proposal_policy_versions", ["tenant_id", "approval_id"], unique=True, postgresql_where=sa.text("approval_id IS NOT NULL"))

    op.create_table(
        "catalog_proposal_evaluations",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("evaluation_id", sa.String(40), nullable=False),
        sa.Column("cluster_id", sa.String(40), nullable=False),
        sa.Column("policy_version_id", sa.String(40), nullable=False),
        sa.Column("facts_hash", sa.String(64), nullable=False),
        sa.Column("facts", postgresql.JSONB(), nullable=False),
        sa.Column("rule_results", postgresql.JSONB(), nullable=False),
        sa.Column("overall_passed", sa.Boolean(), nullable=False),
        sa.Column("blocked_reason", sa.String(100), nullable=True),
        sa.Column("proposed_by_run", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "evaluation_id", name="pk_catalog_proposal_evaluations"),
        sa.UniqueConstraint("tenant_id", "cluster_id", "policy_version_id", "facts_hash", name="uq_catalog_evaluation_facts"),
        sa.UniqueConstraint("tenant_id", "evaluation_id", "cluster_id", "policy_version_id", "facts_hash", name="uq_catalog_evaluation_subject"),
        sa.ForeignKeyConstraint(["tenant_id", "cluster_id"], ["need_clusters.tenant_id", "need_clusters.cluster_id"], name="fk_catalog_evaluation_cluster", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "policy_version_id"], ["catalog_proposal_policy_versions.tenant_id", "catalog_proposal_policy_versions.policy_version_id"], name="fk_catalog_evaluation_policy", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "proposed_by_run"], ["workflow_runs.tenant_id", "workflow_runs.run_id"], name="fk_catalog_evaluation_run", ondelete="RESTRICT"),
        sa.CheckConstraint("evaluation_id ~ '^cpe_' AND facts_hash ~ '^[0-9a-f]{64}$' AND (blocked_reason IS NULL OR btrim(blocked_reason)<>'')", name="ck_catalog_evaluation_core"),
        sa.CheckConstraint("jsonb_typeof(facts)='object' AND jsonb_typeof(rule_results)='array'", name="ck_catalog_evaluation_jsonb"),
        sa.CheckConstraint("NOT (facts ? 'safe_total_quantity') OR facts->'safe_total_quantity'='null'::jsonb OR (jsonb_typeof(facts->'safe_total_quantity')='number' AND facts->>'safe_total_quantity' ~ '^(0|[1-9][0-9]*)$')", name="ck_catalog_evaluation_safe_quantity"),
        sa.CheckConstraint("(overall_passed AND blocked_reason IS NULL) OR NOT overall_passed", name="ck_catalog_evaluation_result"),
    )

    op.create_table(
        "catalog_product_proposals",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("proposal_id", sa.String(40), nullable=False),
        sa.Column("evaluation_id", sa.String(40), nullable=False),
        sa.Column("cluster_id", sa.String(40), nullable=False),
        sa.Column("policy_version_id", sa.String(40), nullable=False),
        sa.Column("facts_hash", sa.String(64), nullable=False),
        sa.Column("owner_employee", sa.String(40), nullable=False),
        sa.Column("proposed_by_run", sa.String(40), nullable=False),
        sa.Column("approval_id", sa.String(40), nullable=True),
        sa.Column("approval_request_hash", sa.String(64), nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "proposal_id", name="pk_catalog_product_proposals"),
        sa.UniqueConstraint("tenant_id", "evaluation_id", name="uq_catalog_product_proposal_evaluation"),
        sa.UniqueConstraint("tenant_id", "proposal_id", "cluster_id", "policy_version_id", "facts_hash", name="uq_catalog_product_proposal_subject"),
        sa.ForeignKeyConstraint(["tenant_id", "evaluation_id", "cluster_id", "policy_version_id", "facts_hash"], ["catalog_proposal_evaluations.tenant_id", "catalog_proposal_evaluations.evaluation_id", "catalog_proposal_evaluations.cluster_id", "catalog_proposal_evaluations.policy_version_id", "catalog_proposal_evaluations.facts_hash"], name="fk_catalog_product_proposal_evaluation", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "owner_employee"], ["employees.tenant_id", "employees.employee_id"], name="fk_catalog_product_proposal_owner", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "proposed_by_run"], ["workflow_runs.tenant_id", "workflow_runs.run_id"], name="fk_catalog_product_proposal_run", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "approval_id"], ["approval_packages.tenant_id", "approval_packages.approval_id"], name="fk_catalog_product_proposal_approval", ondelete="RESTRICT"),
        sa.CheckConstraint("proposal_id ~ '^cpr_' AND facts_hash ~ '^[0-9a-f]{64}$'", name="ck_catalog_product_proposal_core"),
        sa.CheckConstraint("approval_request_hash IS NULL OR approval_request_hash ~ '^[0-9a-f]{64}$'", name="ck_catalog_product_proposal_hash"),
        sa.CheckConstraint("state IN ('awaiting_approval_submission','pending_review','cultivation_queued','rejected','expired','stale')", name="ck_catalog_product_proposal_state"),
        sa.CheckConstraint("updated_at>=created_at", name="ck_catalog_product_proposal_times"),
        sa.CheckConstraint("(state='awaiting_approval_submission' AND approval_id IS NULL AND approval_request_hash IS NULL) OR (state<>'awaiting_approval_submission' AND approval_id IS NOT NULL AND approval_request_hash IS NOT NULL)", name="ck_catalog_product_proposal_lifecycle"),
    )
    op.create_index("uq_catalog_product_proposal_approval", "catalog_product_proposals", ["tenant_id", "approval_id"], unique=True, postgresql_where=sa.text("approval_id IS NOT NULL"))

    op.create_table(
        "catalog_cultivation_cases",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("cultivation_case_id", sa.String(40), nullable=False),
        sa.Column("proposal_id", sa.String(40), nullable=False),
        sa.Column("approval_id", sa.String(40), nullable=False),
        sa.Column("cluster_id", sa.String(40), nullable=False),
        sa.Column("policy_version_id", sa.String(40), nullable=False),
        sa.Column("facts_hash", sa.String(64), nullable=False),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default=sa.text("'queued'")),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "cultivation_case_id", name="pk_catalog_cultivation_cases"),
        sa.UniqueConstraint("tenant_id", "proposal_id", name="uq_catalog_cultivation_proposal"),
        sa.UniqueConstraint("tenant_id", "approval_id", name="uq_catalog_cultivation_approval"),
        sa.ForeignKeyConstraint(["tenant_id", "proposal_id", "cluster_id", "policy_version_id", "facts_hash"], ["catalog_product_proposals.tenant_id", "catalog_product_proposals.proposal_id", "catalog_product_proposals.cluster_id", "catalog_product_proposals.policy_version_id", "catalog_product_proposals.facts_hash"], name="fk_catalog_cultivation_proposal", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "approval_id"], ["approval_packages.tenant_id", "approval_packages.approval_id"], name="fk_catalog_cultivation_approval", ondelete="RESTRICT"),
        sa.CheckConstraint("cultivation_case_id ~ '^ccc_' AND facts_hash ~ '^[0-9a-f]{64}$'", name="ck_catalog_cultivation_core"),
        sa.CheckConstraint("jsonb_typeof(evidence_refs)='array'", name="ck_catalog_cultivation_evidence_jsonb"),
        sa.CheckConstraint("state='queued'", name="ck_catalog_cultivation_state"),
    )
    _create_guards()


def downgrade() -> None:
    op.execute(
        f"""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM catalog_cultivation_cases LIMIT 1)
             OR EXISTS (SELECT 1 FROM catalog_product_proposals LIMIT 1)
             OR EXISTS (SELECT 1 FROM catalog_proposal_evaluations LIMIT 1)
             OR EXISTS (SELECT 1 FROM catalog_proposal_policy_versions LIMIT 1)
             OR EXISTS (SELECT 1 FROM validated_needs WHERE recurring_requirement IS NOT NULL LIMIT 1)
             OR EXISTS (
               SELECT 1 FROM approval_packages
               WHERE NOT coalesce(({_LEGACY_APPROVAL_CHECK}),false) LIMIT 1
             )
          THEN RAISE EXCEPTION '0057 refuses destructive catalog downgrade'; END IF;
        END $$;
        """
    )
    op.execute("DROP TRIGGER trg_catalog_cultivation_immutable ON catalog_cultivation_cases")
    op.execute("DROP FUNCTION guard_catalog_cultivation_case()")
    op.execute("DROP TRIGGER trg_catalog_product_proposal_guard ON catalog_product_proposals")
    op.execute("DROP FUNCTION guard_catalog_product_proposal()")
    op.execute("DROP TRIGGER trg_catalog_evaluation_immutable ON catalog_proposal_evaluations")
    op.execute("DROP FUNCTION guard_catalog_evaluation()")
    op.execute("DROP TRIGGER trg_catalog_policy_version_guard ON catalog_proposal_policy_versions")
    op.execute("DROP FUNCTION guard_catalog_policy_version()")
    op.execute("DROP TRIGGER trg_catalog_approval_namespace ON approval_packages")
    op.execute("DROP FUNCTION guard_catalog_approval_namespace()")
    op.drop_table("catalog_cultivation_cases")
    op.drop_table("catalog_product_proposals")
    op.drop_table("catalog_proposal_evaluations")
    op.drop_table("catalog_proposal_policy_versions")
    op.drop_constraint("ck_approval_quote_contract", "approval_packages", type_="check")
    op.create_check_constraint("ck_approval_quote_contract", "approval_packages", _LEGACY_APPROVAL_CHECK)
    op.drop_constraint("ck_validated_needs_recurring_requirement_jsonb", "validated_needs", type_="check")
    op.drop_column("validated_needs", "recurring_requirement")
