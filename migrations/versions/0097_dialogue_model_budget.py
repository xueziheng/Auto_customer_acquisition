"""普通对话分类与规划共用持久provider轮预算。"""
from alembic import op

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.execute("\nCREATE TABLE dialogue_model_reservations (\n\ttenant_id VARCHAR(40) NOT NULL, \n\tbinding_id VARCHAR(40) NOT NULL, \n\tsource_message_id VARCHAR(40) NOT NULL, \n\trun_id VARCHAR(40) NOT NULL, \n\tcapability VARCHAR(40) NOT NULL, \n\tsequence INTEGER NOT NULL, \n\tprovider_round INTEGER NOT NULL, \n\temployee_id VARCHAR(40) NOT NULL, \n\tuser_id VARCHAR(128) NOT NULL, \n\tconfiguration_version VARCHAR(128) NOT NULL, \n\tmodel VARCHAR(128) NOT NULL, \n\treserved_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_dialogue_model_reservations PRIMARY KEY (tenant_id, run_id, capability, sequence, provider_round), \n\tCONSTRAINT fk_dialogue_model_binding FOREIGN KEY(tenant_id, binding_id) REFERENCES dialogue_bindings (tenant_id, binding_id), \n\tCONSTRAINT fk_dialogue_model_message FOREIGN KEY(tenant_id, source_message_id) REFERENCES messages (tenant_id, message_id), \n\tCONSTRAINT ck_dialogue_model_round CHECK (sequence >= 0 AND provider_round >= 0), \n\tCONSTRAINT ck_dialogue_model_capability CHECK (capability IN ('reply_qualification','ordinary_dialogue'))\n)\n\n")
    op.execute("ALTER TABLE public.dialogue_model_reservations ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.dialogue_model_reservations FORCE ROW LEVEL SECURITY")
    predicate = "current_user::text = 'tradeos_t_' || pg_catalog.substr(pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(tenant_id, 'UTF8')), 'hex'), 1, 48)"
    for policy, mode in (("tradeos_tenant_allow", "PERMISSIVE"), ("tradeos_tenant_guard", "RESTRICTIVE")):
        op.execute(f"CREATE POLICY {policy} ON public.dialogue_model_reservations AS {mode} FOR ALL TO PUBLIC USING ({predicate}) WITH CHECK ({predicate})")
    op.execute("DO $block$ DECLARE account record; BEGIN FOR account IN SELECT rolname FROM pg_catalog.pg_roles WHERE rolname ~ '^tradeos_t_[0-9a-f]{48}$' LOOP EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.dialogue_model_reservations TO %I', account.rolname); END LOOP; END $block$")


def downgrade() -> None:
    op.drop_table("dialogue_model_reservations")
