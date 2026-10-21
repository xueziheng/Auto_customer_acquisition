"""普通对话范围随不可变 Campaign 版本保存；旧版本默认关闭。"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outreach_campaign_versions", sa.Column(
        "ordinary_dialogue_policy", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("outreach_campaign_versions", "ordinary_dialogue_policy")
