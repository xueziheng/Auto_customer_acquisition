"""Tavily 单部署账户免费额度和不可自动释放的预留。

Revision ID: 0039
Revises: 0038
"""

import sqlalchemy as sa
from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "search_quota_runs",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("run_id", sa.String(40), nullable=False),
        sa.Column("stop_reason", sa.String(32)),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "run_id", name="pk_search_quota_runs"),
        sa.CheckConstraint(
            "stop_reason IN ('quota_exhausted','usage_unknown','paid_enabled','request_uncertain','unsupported')",
            name="ck_search_quota_runs_stop_reason",
        ),
    )
    op.create_table(
        "search_quota_accounts",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("ceiling", sa.BigInteger()),
        sa.Column(
            "reservations", sa.BigInteger(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "cost_status",
            sa.String(16),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
        sa.Column("usage_limit", sa.BigInteger()),
        sa.Column("usage_used", sa.BigInteger()),
        sa.Column("paygo_enabled", sa.Boolean()),
        sa.Column("checked_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint(
            "tenant_id", "provider", name="pk_search_quota_accounts"
        ),
        sa.UniqueConstraint("provider", name="uq_search_quota_accounts_provider"),
        sa.CheckConstraint(
            "provider = 'tavily'", name="ck_search_quota_accounts_provider"
        ),
        sa.CheckConstraint(
            "ceiling IS NULL OR ceiling >= 0", name="ck_search_quota_accounts_ceiling"
        ),
        sa.CheckConstraint(
            "reservations >= 0", name="ck_search_quota_accounts_reservations"
        ),
        sa.CheckConstraint(
            "cost_status IN ('free','paid','unknown')",
            name="ck_search_quota_accounts_cost_status",
        ),
        sa.CheckConstraint(
            "usage_limit IS NULL OR usage_limit >= 0",
            name="ck_search_quota_accounts_usage_limit",
        ),
        sa.CheckConstraint(
            "usage_used IS NULL OR usage_used >= 0",
            name="ck_search_quota_accounts_usage_used",
        ),
    )
    op.create_table(
        "search_quota_reservations",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("run_id", sa.String(40), nullable=False),
        sa.Column("request_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "provider",
            "run_id",
            "request_key",
            name="pk_search_quota_reservations",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "provider"],
            ["search_quota_accounts.tenant_id", "search_quota_accounts.provider"],
            name="fk_search_quota_reservations_account",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "status IN ('reserved','uncertain','consumed')",
            name="ck_search_quota_reservations_status",
        ),
        sa.CheckConstraint(
            "request_key ~ '^[a-f0-9]{64}$'",
            name="ck_search_quota_reservations_request_key",
        ),
    )


def downgrade() -> None:
    op.drop_table("search_quota_runs")
    op.drop_table("search_quota_reservations")
    op.drop_table("search_quota_accounts")
