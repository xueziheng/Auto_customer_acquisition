"""员工私有会话及同事务持久执行意图。"""

from alembic import op

revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "\nCREATE TABLE agent_sessions (\n\ttenant_id VARCHAR(128) NOT NULL, \n\tsession_id VARCHAR(40) NOT NULL, \n\tuser_id VARCHAR(128) NOT NULL, \n\temployee_id VARCHAR(128) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tversion INTEGER NOT NULL, \n\tCONSTRAINT pk_agent_sessions PRIMARY KEY (tenant_id, session_id)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE agent_turns (\n\ttenant_id VARCHAR(128) NOT NULL, \n\tturn_id VARCHAR(40) NOT NULL, \n\tsession_id VARCHAR(40) NOT NULL, \n\trun_id VARCHAR(40) NOT NULL, \n\tidempotency_key VARCHAR(128) NOT NULL, \n\trequest_hmac VARCHAR(64) NOT NULL, \n\tinput_text TEXT NOT NULL, \n\tobject_refs JSONB NOT NULL, \n\tresult JSONB, \n\tstate VARCHAR(32) NOT NULL, \n\tdispatch_state VARCHAR(16) NOT NULL, \n\tturn_kind VARCHAR(16) NOT NULL, \n\tcreated_at TIMESTAMP WITH TIME ZONE NOT NULL, \n\tattempt_of VARCHAR(40), \n\tproposal_id VARCHAR(128), \n\terror_code VARCHAR(40), \n\tCONSTRAINT pk_agent_turns PRIMARY KEY (tenant_id, turn_id), \n\tCONSTRAINT uq_agent_turn_request UNIQUE (tenant_id, session_id, idempotency_key), \n\tCONSTRAINT uq_agent_turn_run UNIQUE (tenant_id, run_id), \n\tCONSTRAINT fk_agent_turn_session FOREIGN KEY(tenant_id, session_id) REFERENCES agent_sessions (tenant_id, session_id) ON DELETE RESTRICT, \n\tCONSTRAINT fk_agent_turn_attempt FOREIGN KEY(tenant_id, attempt_of) REFERENCES agent_turns (tenant_id, turn_id) ON DELETE RESTRICT, \n\tCONSTRAINT ck_agent_turn_state CHECK (state IN ('queued','running','awaiting_input','proposal_ready','completed','blocked','failed','unknown','cancelled')), \n\tCONSTRAINT ck_agent_turn_dispatch CHECK (dispatch_state IN ('pending','bound') AND turn_kind IN ('conversation','model_probe'))\n)\n\n"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_agent_turn_active ON agent_turns (tenant_id, session_id) WHERE state IN ('queued','running')"
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM agent_sessions) THEN RAISE EXCEPTION 'assistant data exists; refuse lossy downgrade'; END IF; END $$"
    )
    op.drop_table("agent_turns")
    op.drop_table("agent_sessions")
