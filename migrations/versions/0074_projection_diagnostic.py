"""来源尝试的可空固定投影诊断；旧记录保持NULL。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """沿既有租户表新增安全摘要，不保存原候选正文。"""
    op.add_column(
        "acquisition_attempts",
        sa.Column("diagnostic", JSONB(none_as_null=True), nullable=True),
    )


def downgrade() -> None:
    """仅移除诊断列，保留原来源与计量。"""
    op.drop_column("acquisition_attempts", "diagnostic")
