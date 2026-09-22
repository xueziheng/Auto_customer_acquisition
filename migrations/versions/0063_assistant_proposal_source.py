"""研究提案与会话来源的原子唯一绑定。"""

import sqlalchemy as sa
from alembic import op

revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assistant_proposal_sources",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("source_turn_id", sa.String(40), nullable=False),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("proposal_id", sa.String(40), nullable=False),
        sa.Column("request_hmac", sa.String(256), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "source_turn_id", "source_version"),
        sa.UniqueConstraint("tenant_id", "proposal_id"),
        sa.CheckConstraint(
            "source_version >= 1", name="ck_assistant_proposal_source_version"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "proposal_id"],
            ["directive_proposals.tenant_id", "directive_proposals.proposal_id"],
            deferrable=True,
            initially="DEFERRED",
        ),
    )
    op.execute(
        """CREATE FUNCTION assistant_proposal_sources_immutable() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION '研究提案来源不可改写'; END; $$"""
    )
    op.execute(
        "CREATE TRIGGER trg_assistant_proposal_sources_immutable BEFORE UPDATE OR DELETE ON assistant_proposal_sources FOR EACH ROW EXECUTE FUNCTION assistant_proposal_sources_immutable()"
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM assistant_proposal_sources)"))
        .scalar()
    ):
        raise RuntimeError("已有研究提案来源，不可降级删除")
    op.drop_table("assistant_proposal_sources")
    op.execute("DROP FUNCTION assistant_proposal_sources_immutable()")
