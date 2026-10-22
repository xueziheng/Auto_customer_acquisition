"""普通回复共享Campaign每日总量的不可退款预占标记。"""
import sqlalchemy as sa
from alembic import op

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("dialogue_executions", sa.Column("campaign_quota_reserved_at", sa.DateTime(timezone=True), nullable=True))

def downgrade() -> None:
    op.drop_column("dialogue_executions", "campaign_quota_reserved_at")
