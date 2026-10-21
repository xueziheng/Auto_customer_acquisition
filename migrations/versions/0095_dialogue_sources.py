"""普通对话来源祖先与canonical消息映射。"""
from alembic import op

revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("\nCREATE TABLE dialogue_bindings (\n\ttenant_id VARCHAR(40) NOT NULL, \n\tbinding_id VARCHAR(40) NOT NULL, \n\taccount_id VARCHAR(40) NOT NULL, \n\tcontact_point_id VARCHAR(40) NOT NULL, \n\thypothesis_id VARCHAR(40) NOT NULL, \n\tsending_identity_id VARCHAR(40) NOT NULL, \n\tcampaign_id VARCHAR(40) NOT NULL, \n\tcampaign_version INTEGER NOT NULL, \n\tapproval_id VARCHAR(40) NOT NULL, \n\towner_employee_id VARCHAR(40) NOT NULL, \n\townership_locked_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tmailbox_operation_id VARCHAR(40) NOT NULL, \n\tsource_kind VARCHAR(20) NOT NULL, \n\tenrollment_id VARCHAR(40), \n\tbusiness_message_id VARCHAR(40), \n\tadmitted_message_id VARCHAR(40) NOT NULL, \n\tadmitted_source_sha256 VARCHAR(64) NOT NULL, \n\tstate VARCHAR(20) NOT NULL, \n\trounds_sent INTEGER NOT NULL, \n\tmodel_calls INTEGER NOT NULL, \n\tno_progress_rounds INTEGER NOT NULL, \n\tlatest_message_id VARCHAR(40) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_dialogue_bindings PRIMARY KEY (tenant_id, binding_id), \n\tCONSTRAINT uq_dialogue_binding_admission UNIQUE (tenant_id, admitted_message_id), \n\tCONSTRAINT uq_dialogue_binding_business UNIQUE (tenant_id, business_message_id), \n\tCONSTRAINT fk_dialogue_binding_account FOREIGN KEY(tenant_id, account_id) REFERENCES prospect_accounts (tenant_id, account_id), \n\tCONSTRAINT fk_dialogue_binding_contact FOREIGN KEY(tenant_id, contact_point_id) REFERENCES contact_points (tenant_id, contact_point_id), \n\tCONSTRAINT fk_dialogue_binding_hypothesis FOREIGN KEY(tenant_id, hypothesis_id) REFERENCES need_hypotheses (tenant_id, hypothesis_id), \n\tCONSTRAINT fk_dialogue_binding_sender FOREIGN KEY(tenant_id, sending_identity_id) REFERENCES sending_identities (tenant_id, identity_id), \n\tCONSTRAINT fk_dialogue_binding_campaign FOREIGN KEY(tenant_id, campaign_id, campaign_version) REFERENCES outreach_campaign_versions (tenant_id, campaign_id, version), \n\tCONSTRAINT fk_dialogue_binding_enrollment FOREIGN KEY(tenant_id, enrollment_id) REFERENCES outreach_enrollments (tenant_id, enrollment_id), \n\tCONSTRAINT fk_dialogue_binding_message FOREIGN KEY(tenant_id, admitted_message_id) REFERENCES messages (tenant_id, message_id), \n\tCONSTRAINT fk_dialogue_binding_owner FOREIGN KEY(tenant_id, owner_employee_id) REFERENCES employees (tenant_id, employee_id), \n\tCONSTRAINT ck_dialogue_binding_source CHECK ((source_kind = 'outreach' AND enrollment_id IS NOT NULL AND business_message_id IS NULL) OR (source_kind = 'inquiry' AND enrollment_id IS NULL AND business_message_id IS NOT NULL)), \n\tCONSTRAINT ck_dialogue_binding_state CHECK (state IN ('active','review','stopped')), \n\tCONSTRAINT ck_dialogue_binding_counters CHECK (rounds_sent >= 0 AND model_calls >= 0 AND no_progress_rounds >= 0)\n)\n\n")
    op.execute('\nCREATE TABLE dialogue_messages (\n\ttenant_id VARCHAR(40) NOT NULL, \n\tmessage_id VARCHAR(40) NOT NULL, \n\tbinding_id VARCHAR(40) NOT NULL, \n\tCONSTRAINT pk_dialogue_messages PRIMARY KEY (tenant_id, message_id), \n\tCONSTRAINT fk_dialogue_message_source FOREIGN KEY(tenant_id, message_id) REFERENCES messages (tenant_id, message_id), \n\tCONSTRAINT fk_dialogue_message_binding FOREIGN KEY(tenant_id, binding_id) REFERENCES dialogue_bindings (tenant_id, binding_id)\n)\n\n')
    op.create_foreign_key("fk_dialogue_execution_binding", "dialogue_executions", "dialogue_bindings", ["tenant_id", "binding_id"], ["tenant_id", "binding_id"])
    op.create_foreign_key("fk_dialogue_binding_approval", "dialogue_bindings", "approval_packages", ["tenant_id", "approval_id"], ["tenant_id", "approval_id"])
    op.create_foreign_key("fk_dialogue_binding_business", "dialogue_bindings", "business_mail_messages", ["tenant_id", "business_message_id"], ["tenant_id", "message_id"])
    predicate = "current_user::text = 'tradeos_t_' || pg_catalog.substr(pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(tenant_id, 'UTF8')), 'hex'), 1, 48)"
    for name in ("dialogue_bindings", "dialogue_messages"):
        op.execute(f"ALTER TABLE public.{name} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE public.{name} FORCE ROW LEVEL SECURITY")
        for policy, mode in (("tradeos_tenant_allow", "PERMISSIVE"), ("tradeos_tenant_guard", "RESTRICTIVE")):
            op.execute(f"CREATE POLICY {policy} ON public.{name} AS {mode} FOR ALL TO PUBLIC USING ({predicate}) WITH CHECK ({predicate})")
    op.execute("DO $block$ DECLARE account record; BEGIN FOR account IN SELECT rolname FROM pg_catalog.pg_roles WHERE rolname ~ '^tradeos_t_[0-9a-f]{48}$' LOOP EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.dialogue_bindings, public.dialogue_messages TO %I', account.rolname); END LOOP; END $block$")


def downgrade() -> None:
    op.drop_constraint("fk_dialogue_execution_binding", "dialogue_executions", type_="foreignkey")
    op.drop_table("dialogue_messages")
    op.drop_table("dialogue_bindings")
