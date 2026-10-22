"""不可变 Campaign 版本保存显式自动客户准备范围，旧版本保持关闭。"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("outreach_campaign_versions", sa.Column("customer_agent_policy", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("outreach_campaign_versions", "customer_agent_policy")
