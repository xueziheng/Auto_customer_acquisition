"""0011 Outreach 发送 claim 状态与 typed 失败类别。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATE_FIELDS = (
    "(state='reserved' AND provider_ref IS NULL AND failure_category IS NULL "
    "AND send_claimed_at IS NULL) OR "
    "(state='sending' AND provider_ref IS NULL AND failure_category IS NULL "
    "AND send_claimed_at IS NOT NULL) OR "
    "(state='sent' AND provider_ref IS NOT NULL AND failure_category IS NULL) OR "
    "(state='failed_transient' AND provider_ref IS NULL AND failure_category IN "
    "('rate_limited','provider_transient','provider_auth_required')) OR "
    "(state='failed_permanent' AND provider_ref IS NULL AND failure_category IN "
    "('provider_permanent','identity_unavailable'))"
)


def upgrade() -> None:
    """增加持久发送线性化证据并收紧状态组合。"""
    op.add_column(
        "outreach_message_attempts",
        sa.Column("send_claimed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.drop_constraint(
        "ck_outreach_attempt_state_fields",
        "outreach_message_attempts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_outreach_attempt_state_fields",
        "outreach_message_attempts",
        _STATE_FIELDS,
    )


def downgrade() -> None:
    """仅允许可由 0009 表达的数据回退。"""
    op.execute(
        "UPDATE outreach_message_attempts SET state='reserved', "
        "send_claimed_at=NULL WHERE state='sending'"
    )
    op.execute(
        "UPDATE outreach_message_attempts SET failure_category='provider_transient' "
        "WHERE state='failed_transient' AND failure_category IN "
        "('rate_limited','provider_auth_required')"
    )
    op.execute(
        "UPDATE outreach_message_attempts SET failure_category='identity_unavailable' "
        "WHERE state='failed_permanent' AND failure_category='provider_permanent'"
    )
    op.drop_constraint(
        "ck_outreach_attempt_state_fields",
        "outreach_message_attempts",
        type_="check",
    )
    op.drop_column("outreach_message_attempts", "send_claimed_at")
    op.create_check_constraint(
        "ck_outreach_attempt_state_fields",
        "outreach_message_attempts",
        "(state='reserved' AND provider_ref IS NULL AND failure_category IS NULL) OR "
        "(state='sent' AND provider_ref IS NOT NULL AND failure_category IS NULL) OR "
        "(state='failed_transient' AND provider_ref IS NULL AND "
        "failure_category='provider_transient') OR "
        "(state='failed_permanent' AND provider_ref IS NULL AND "
        "failure_category='identity_unavailable')",
    )
