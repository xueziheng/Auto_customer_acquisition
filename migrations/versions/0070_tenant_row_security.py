"""为全部业务表建立不可由请求 GUC 更改的企业角色隔离。"""
from __future__ import annotations

from alembic import op

revision = "0070"
down_revision = "0069"
branch_labels = None
depends_on = None

_TABLES = ('agent_sessions', 'agent_turns', 'approval_applications', 'approval_packages', 'artifacts', 'assistant_proposal_sources', 'auth_accounts', 'auth_rate_limits', 'auth_sessions', 'boss_directives', 'catalog_cultivation_cases', 'catalog_product_proposals', 'catalog_proposal_evaluations', 'catalog_proposal_policy_versions', 'catalog_reconciliation_checkpoints', 'commitments', 'company_playbook_activations', 'company_playbook_versions', 'contact_legal_basis', 'contact_points', 'conversation_classification_corrections', 'conversation_classifications', 'conversation_reply_work', 'conversations', 'cost_items', 'cost_scope_confirmations', 'cost_sheet_fx_rates', 'cost_sheets', 'costing_coverage', 'costing_policies', 'costing_price_evidence', 'costing_quote_bases', 'costing_quote_fx', 'country_policy_activations', 'country_policy_field_provenance', 'country_policy_versions', 'demand_signals', 'directive_proposals', 'directive_versions', 'email_feedback_cursors', 'email_feedback_quarantines', 'email_feedback_receipts', 'email_inbound_cursors', 'email_inbound_receipts', 'email_inbound_reviews', 'employee_confirmations', 'employees', 'extracted_facts', 'handoff_escalations', 'handoffs', 'in_app_notifications', 'loss_records', 'mailbox_accounts', 'mailbox_messages', 'margin_rules', 'messages', 'model_configuration_heads', 'model_configuration_versions', 'model_invocations', 'model_probes', 'model_quota_buckets', 'model_runtime_processes', 'model_slot_releases', 'need_cluster_members', 'need_clusters', 'need_hypotheses', 'need_unit_confirmations', 'notification_deliveries', 'notification_jobs', 'opportunities', 'outbox_deliveries', 'outbox_events', 'outreach_actions', 'outreach_campaign_versions', 'outreach_campaigns', 'outreach_daily_quotas', 'outreach_enrollments', 'outreach_message_attempts', 'outreach_sequence_steps', 'outreach_suppressions', 'ownership_locks', 'ownership_transfer_history', 'product_candidate_price_refs', 'product_candidate_sources', 'product_match_specs', 'product_variants', 'products', 'prospect_accounts', 'prospect_contacts', 'prospecting_erasure_suppressions', 'provenance_records', 'provider_readiness_events', 'quotation_approval_bindings', 'quotation_approval_receipts', 'quotation_evidence_refs', 'quotation_files', 'quotation_issuers', 'quotation_lines', 'quotation_send_receipts', 'quotation_state_events', 'quotations', 'quote_creation_operations', 'raw_artifacts', 'score_snapshots', 'search_quota_accounts', 'search_quota_reservations', 'search_quota_runs', 'sending_auth_check_requests', 'sending_auth_checks', 'sending_daily_counters', 'sending_domains', 'sending_identities', 'sending_identity_actions', 'sending_reputation_events', 'sending_send_reservations', 'sourcing_admissions', 'sourcing_candidate_drafts', 'sourcing_candidate_evidence', 'sourcing_candidates', 'sourcing_cases', 'sourcing_ladder_checks', 'sourcing_page_attempts', 'sourcing_priority_snapshots', 'sourcing_public_plans', 'sourcing_reviews', 'sourcing_search_executions', 'sourcing_search_reconciliations', 'sourcing_supply_options', 'supplier_price_records', 'suppliers', 'supply_capabilities', 'territory_assignments', 'tool_call_events', 'tool_calls', 'unsubscribe_tokens', 'validated_need_field_history', 'validated_needs', 'work_uploads', 'workflow_runs', 'workflow_steps')
_PREDICATE = (
    "current_user::text = 'tradeos_t_' || "
    "pg_catalog.substr(pg_catalog.encode(pg_catalog.sha256("
    "pg_catalog.convert_to(tenant_id, 'UTF8')), 'hex'), 1, 48)"
)


def upgrade() -> None:
    """固定迁移时的表集合；缺失或新增未覆盖业务表均拒绝升级。"""
    expected = ", ".join("'" + name + "'" for name in _TABLES)
    op.execute(f"""
        DO $block$
        DECLARE actual text[];
        BEGIN
            SELECT array_agg(c.relname::text ORDER BY c.relname::text) INTO actual
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
            WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
              AND a.attname = 'tenant_id' AND NOT a.attisdropped;
            IF EXISTS (
                SELECT 1 FROM pg_catalog.pg_class c
                JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_catalog.pg_attribute a ON a.attrelid = c.oid
                WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')
                  AND a.attname = 'tenant_id' AND NOT a.attisdropped AND NOT a.attnotnull
            ) THEN
                RAISE EXCEPTION 'nullable tenant column';
            END IF;
            IF actual IS DISTINCT FROM ARRAY[{expected}]::text[] THEN
                RAISE EXCEPTION 'tenant table coverage mismatch';
            END IF;
        END
        $block$
    """)
    for name in _TABLES:
        table = 'public."' + name + '"'
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY tradeos_tenant_allow ON {table} AS PERMISSIVE "
            f"FOR ALL TO PUBLIC USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )
        op.execute(
            f"CREATE POLICY tradeos_tenant_guard ON {table} AS RESTRICTIVE "
            f"FOR ALL TO PUBLIC USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
        )


def downgrade() -> None:
    """撤销策略前先撤销企业角色授权，回退不得暴露其他企业数据。"""
    op.execute("""
        DO $block$
        DECLARE account record;
        BEGIN
            FOR account IN SELECT rolname FROM pg_catalog.pg_roles
                WHERE rolname ~ '^tradeos_t_[0-9a-f]{48}$'
            LOOP
                EXECUTE format('REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM %I', account.rolname);
            END LOOP;
        END
        $block$
    """)
    for name in reversed(_TABLES):
        table = 'public."' + name + '"'
        op.execute(f"DROP POLICY tradeos_tenant_guard ON {table}")
        op.execute(f"DROP POLICY tradeos_tenant_allow ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
