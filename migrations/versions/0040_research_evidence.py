"""研究来源归属与跨线路幂等证据；旧记录保留原解释。

Revision ID: 0040
Revises: 0039
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "demand_signals",
        sa.Column("research_evidence", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "demand_signals",
        sa.Column("discovery_key", sa.String(64), nullable=False, server_default=""),
    )
    op.drop_constraint(
        "uq_demand_signals_source_identity", "demand_signals", type_="unique"
    )
    op.create_unique_constraint(
        "uq_demand_signals_source_identity",
        "demand_signals",
        [
            "tenant_id",
            "entity_name",
            "signal_type",
            "source_type",
            "source_id",
            "discovery_key",
        ],
    )
    op.execute("""
        CREATE FUNCTION protect_research_evidence() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.research_evidence IS DISTINCT FROM NEW.research_evidence
               OR OLD.discovery_key IS DISTINCT FROM NEW.discovery_key THEN
                RAISE EXCEPTION 'research evidence is immutable';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute("""
        CREATE TRIGGER research_evidence_immutable BEFORE UPDATE ON demand_signals
        FOR EACH ROW EXECUTE FUNCTION protect_research_evidence();
    """)


def downgrade() -> None:
    # 新跨线路记录可能与旧五列唯一键冲突，回滚前需人工导出/处置，绝不删证据。
    op.execute("DROP TRIGGER research_evidence_immutable ON demand_signals")
    op.execute("DROP FUNCTION protect_research_evidence()")
    op.drop_constraint(
        "uq_demand_signals_source_identity", "demand_signals", type_="unique"
    )
    op.create_unique_constraint(
        "uq_demand_signals_source_identity",
        "demand_signals",
        ["tenant_id", "entity_name", "signal_type", "source_type", "source_id"],
    )
    op.drop_column("demand_signals", "discovery_key")
    op.drop_column("demand_signals", "research_evidence")
