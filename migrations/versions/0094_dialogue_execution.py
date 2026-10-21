"""普通对话按源消息唯一的执行租约，业务授权由独立绑定检查。"""
from alembic import op

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("\nCREATE TABLE dialogue_executions (\n\ttenant_id VARCHAR(40) NOT NULL, \n\tsource_message_id VARCHAR(40) NOT NULL, \n\tbinding_id VARCHAR(40) NOT NULL, \n\trun_id VARCHAR(40) NOT NULL, \n\tmodel_sequence INTEGER NOT NULL, \n\tstate VARCHAR(20) NOT NULL, \n\tversion INTEGER NOT NULL, \n\tlease_token VARCHAR(40), \n\tlease_until TIMESTAMP WITH TIME ZONE, \n\tplan_artifact_id VARCHAR(40), \n\tplan_sha256 VARCHAR(64), \n\tattempt_id VARCHAR(40), \n\tdeterministic_message_id VARCHAR(256), \n\tidempotency_key VARCHAR(200), \n\tprovider_ref VARCHAR(256), \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_dialogue_executions PRIMARY KEY (tenant_id, source_message_id), \n\tCONSTRAINT fk_dialogue_execution_message FOREIGN KEY(tenant_id, source_message_id) REFERENCES messages (tenant_id, message_id), \n\tCONSTRAINT uq_dialogue_execution_run UNIQUE (tenant_id, run_id), \n\tCONSTRAINT uq_dialogue_execution_attempt UNIQUE (tenant_id, attempt_id), \n\tCONSTRAINT uq_dialogue_execution_header UNIQUE (tenant_id, deterministic_message_id), \n\tCONSTRAINT uq_dialogue_execution_key UNIQUE (tenant_id, idempotency_key), \n\tCONSTRAINT ck_dialogue_execution_state CHECK (state IN ('queued','claimed','model_started','planned','sending','sent','review','cancelled','unknown')), \n\tCONSTRAINT ck_dialogue_execution_version CHECK (version >= 0 AND model_sequence >= 0), \n\tCONSTRAINT ck_dialogue_execution_plan CHECK ((plan_artifact_id IS NULL) = (plan_sha256 IS NULL)), \n\tCONSTRAINT ck_dialogue_execution_planned CHECK (state NOT IN ('planned','sending','sent') OR plan_artifact_id IS NOT NULL), \n\tCONSTRAINT ck_dialogue_execution_send CHECK (state NOT IN ('sending','sent') OR (attempt_id IS NOT NULL AND deterministic_message_id IS NOT NULL AND idempotency_key IS NOT NULL))\n)\n\n")
    op.execute("ALTER TABLE public.dialogue_executions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.dialogue_executions FORCE ROW LEVEL SECURITY")
    predicate = "current_user::text = 'tradeos_t_' || pg_catalog.substr(pg_catalog.encode(pg_catalog.sha256(pg_catalog.convert_to(tenant_id, 'UTF8')), 'hex'), 1, 48)"
    for policy, mode in (("tradeos_tenant_allow", "PERMISSIVE"), ("tradeos_tenant_guard", "RESTRICTIVE")):
        op.execute(f"CREATE POLICY {policy} ON public.dialogue_executions AS {mode} FOR ALL TO PUBLIC USING ({predicate}) WITH CHECK ({predicate})")
    op.execute("DO $block$ DECLARE account record; BEGIN FOR account IN SELECT rolname FROM pg_catalog.pg_roles WHERE rolname ~ '^tradeos_t_[0-9a-f]{48}$' LOOP EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.dialogue_executions TO %I', account.rolname); END LOOP; END $block$")


def downgrade() -> None:
    op.drop_table("dialogue_executions")
