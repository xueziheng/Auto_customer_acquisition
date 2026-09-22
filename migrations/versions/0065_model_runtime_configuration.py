"""模型配置当前版本、进程心跳与显式探测。"""

import sqlalchemy as sa
from alembic import op

revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "\nCREATE TABLE model_configuration_heads (\n\ttenant_id VARCHAR(128) NOT NULL, \n\tversion VARCHAR(128) NOT NULL, \n\tPRIMARY KEY (tenant_id), \n\tFOREIGN KEY(tenant_id, version) REFERENCES model_configuration_versions (tenant_id, version)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE model_runtime_processes (\n\ttenant_id VARCHAR(128) NOT NULL, \n\tprocess VARCHAR(16) NOT NULL, \n\tversion VARCHAR(128) NOT NULL, \n\tinstance_id VARCHAR(40) NOT NULL, \n\theartbeat_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tPRIMARY KEY (tenant_id, process), \n\tCONSTRAINT ck_model_runtime_process CHECK (process IN ('api','scheduler'))\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE model_probes (\n\ttenant_id VARCHAR(128) NOT NULL, \n\tturn_id VARCHAR(40) NOT NULL, \n\temployee_id VARCHAR(128) NOT NULL, \n\tconfiguration_version VARCHAR(128) NOT NULL, \n\tidempotency_key VARCHAR(128) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tverified_at TIMESTAMP WITH TIME ZONE, \n\tPRIMARY KEY (tenant_id, turn_id), \n\tUNIQUE (tenant_id, employee_id, configuration_version, idempotency_key), \n\tFOREIGN KEY(tenant_id, turn_id) REFERENCES agent_turns (tenant_id, turn_id), \n\tFOREIGN KEY(tenant_id, configuration_version) REFERENCES model_configuration_versions (tenant_id, version)\n)\n\n"
    )
    op.add_column(
        "agent_sessions",
        sa.Column(
            "session_kind", sa.String(16), nullable=False, server_default="conversation"
        ),
    )
    op.execute(
        "CREATE FUNCTION model_configuration_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION '模型配置版本不可改写'; END; $$"
    )
    op.execute(
        "CREATE TRIGGER trg_model_configuration_immutable BEFORE UPDATE OR DELETE ON model_configuration_versions FOR EACH ROW EXECUTE FUNCTION model_configuration_immutable()"
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM model_configuration_heads)"))
        .scalar()
    ):
        raise RuntimeError("已有模型配置，不可降级删除")
    op.execute(
        "DROP TRIGGER trg_model_configuration_immutable ON model_configuration_versions"
    )
    op.execute("DROP FUNCTION model_configuration_immutable()")
    op.drop_column("agent_sessions", "session_kind")
    op.drop_table("model_probes")
    op.drop_table("model_runtime_processes")
    op.drop_table("model_configuration_heads")
