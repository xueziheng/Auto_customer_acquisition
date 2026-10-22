"""显式区分手动入组，历史记录继续保持自动序列语义。"""

import sqlalchemy as sa
from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """模式只在新入组时选择，自动扫描必须排除 manual。"""
    op.add_column(
        "outreach_enrollments",
        sa.Column(
            "execution_mode",
            sa.String(16),
            nullable=False,
            server_default="automatic",
        ),
    )
    op.create_check_constraint(
        "ck_outreach_enrollment_execution_mode",
        "outreach_enrollments",
        "execution_mode IN ('automatic','manual')",
    )


def downgrade() -> None:
    """拒绝抹除人工发送意图，避免旧scheduler把手动记录当成自动任务。"""
    op.execute(sa.text("LOCK TABLE outreach_enrollments IN ACCESS EXCLUSIVE MODE"))
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM outreach_enrollments WHERE execution_mode='manual')"
        )
    ):
        raise RuntimeError("存在手动入组记录，不能无损降级0076")
    op.drop_constraint(
        "ck_outreach_enrollment_execution_mode", "outreach_enrollments", type_="check"
    )
    op.drop_column("outreach_enrollments", "execution_mode")
