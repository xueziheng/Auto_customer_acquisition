"""独立保存已核实的套餐内免费资格；历史未知记录不自动放行。"""

import sqlalchemy as sa
from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "search_quota_accounts",
        sa.Column("included_credits_free", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_check_constraint(
        "ck_search_quota_accounts_included_credits",
        "search_quota_accounts",
        "NOT included_credits_free OR (usage_limit IS NOT NULL AND usage_used IS NOT NULL "
        "AND cost_status <> 'paid' AND paygo_enabled IS NOT TRUE)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_search_quota_accounts_included_credits", "search_quota_accounts", type_="check"
    )
    op.drop_column("search_quota_accounts", "included_credits_free")
