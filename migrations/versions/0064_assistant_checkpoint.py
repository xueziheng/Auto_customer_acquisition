"""会话生成检查点与受信来源闭包；不在工作流审计中保存正文。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agent_turns",
        sa.Column(
            "context_refs",
            JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "agent_turns", sa.Column("checkpoint_sequence", sa.Integer(), nullable=True)
    )
    op.create_check_constraint(
        "ck_assistant_checkpoint_sequence",
        "agent_turns",
        "checkpoint_sequence IS NULL OR checkpoint_sequence IN (0,1)",
    )


def downgrade() -> None:
    if (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM agent_turns WHERE checkpoint_sequence IS NOT NULL)"
            )
        )
        .scalar()
    ):
        raise RuntimeError("已有生成检查点，不可降级删除")
    op.drop_constraint("ck_assistant_checkpoint_sequence", "agent_turns", type_="check")
    op.drop_column("agent_turns", "checkpoint_sequence")
    op.drop_column("agent_turns", "context_refs")
