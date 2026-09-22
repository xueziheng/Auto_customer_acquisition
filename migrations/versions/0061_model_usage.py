"""模型实际用量、持久配额和非秘密版本配置。"""

from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "\nCREATE TABLE model_invocations (\n\ttenant_id VARCHAR(128) NOT NULL, \n\tinvocation_id VARCHAR(40) NOT NULL, \n\tuser_id VARCHAR(128) NOT NULL, \n\temployee_id VARCHAR(128) NOT NULL, \n\trun_id VARCHAR(128) NOT NULL, \n\tturn_id VARCHAR(128), \n\tcapability VARCHAR(40) NOT NULL, \n\tconfiguration_version VARCHAR(128) NOT NULL, \n\tsequence INTEGER NOT NULL, \n\trequest_hmac VARCHAR(64) NOT NULL, \n\tprovider VARCHAR(16) NOT NULL, \n\tmodel VARCHAR(128) NOT NULL, \n\tstate VARCHAR(16) NOT NULL, \n\tinput_tokens BIGINT, \n\tcached_input_tokens BIGINT, \n\toutput_tokens BIGINT, \n\tslot_released BOOLEAN NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tdispatched_at TIMESTAMP WITH TIME ZONE, \n\tfinished_at TIMESTAMP WITH TIME ZONE, \n\tCONSTRAINT pk_model_invocations PRIMARY KEY (tenant_id, invocation_id), \n\tCONSTRAINT uq_model_invocation_identity UNIQUE (tenant_id, run_id, capability, configuration_version, sequence), \n\tCONSTRAINT ck_model_invocation_state CHECK (state IN ('reserved','dispatched','succeeded','rejected','invalid','unknown')), \n\tCONSTRAINT ck_model_invocation_identity CHECK (sequence >= 0 AND request_hmac ~ '^[0-9a-f]{64}$'), \n\tCONSTRAINT ck_model_invocation_usage CHECK ((input_tokens IS NULL OR input_tokens >= 0) AND (cached_input_tokens IS NULL OR cached_input_tokens >= 0) AND (output_tokens IS NULL OR output_tokens >= 0) AND (input_tokens IS NULL OR cached_input_tokens IS NULL OR cached_input_tokens <= input_tokens))\n)\n\n"
    )
    op.execute(
        "CREATE INDEX ix_model_invocations_quota ON model_invocations (tenant_id, created_at, employee_id)"
    )
    op.execute(
        "\nCREATE TABLE model_quota_buckets (\n\ttenant_id VARCHAR(128) NOT NULL, \n\tscope_key VARCHAR(160) NOT NULL, \n\twindow_started_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tcalls BIGINT NOT NULL, \n\tCONSTRAINT pk_model_quota_buckets PRIMARY KEY (tenant_id, scope_key), \n\tCONSTRAINT ck_model_quota_calls CHECK (calls >= 0)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE model_slot_releases (\n\ttenant_id VARCHAR(128) NOT NULL, \n\tinvocation_id VARCHAR(40) NOT NULL, \n\toperator_id VARCHAR(128) NOT NULL, \n\treason VARCHAR(40) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_model_slot_releases PRIMARY KEY (tenant_id, invocation_id), \n\tCONSTRAINT fk_model_slot_release_invocation FOREIGN KEY(tenant_id, invocation_id) REFERENCES model_invocations (tenant_id, invocation_id) ON DELETE RESTRICT\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE model_configuration_versions (\n\ttenant_id VARCHAR(128) NOT NULL, \n\tversion VARCHAR(128) NOT NULL, \n\tmodel VARCHAR(128) NOT NULL, \n\tlimits JSONB NOT NULL, \n\texport_enabled BOOLEAN NOT NULL, \n\tcreated_by VARCHAR(128) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tCONSTRAINT pk_model_configuration_versions PRIMARY KEY (tenant_id, version)\n)\n\n"
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM model_invocations) OR
         EXISTS (SELECT 1 FROM model_configuration_versions) THEN
        RAISE EXCEPTION 'model usage/configuration exists; refuse lossy downgrade';
      END IF;
    END $$""")
    op.drop_table("model_configuration_versions")
    op.drop_table("model_slot_releases")
    op.drop_table("model_quota_buckets")
    op.drop_table("model_invocations")
