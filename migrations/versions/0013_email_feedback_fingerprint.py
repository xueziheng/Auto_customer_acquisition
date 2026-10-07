"""0013 邮件反馈 receipt 增加安全 payload fingerprint。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_FINGERPRINT = "0" * 64


def upgrade() -> None:
    """向已存在的 receipt 增加非空指纹；旧行使用保留的 fail-closed 标记。"""
    op.add_column(
        "email_feedback_receipts",
        sa.Column(
            "item_fingerprint",
            sa.String(64),
            nullable=False,
            server_default=_LEGACY_FINGERPRINT,
        ),
    )
    op.alter_column(
        "email_feedback_receipts",
        "item_fingerprint",
        existing_type=sa.String(64),
        nullable=False,
        server_default=None,
    )
    op.create_check_constraint(
        "ck_email_feedback_receipt_fingerprint",
        "email_feedback_receipts",
        "item_fingerprint ~ '^[0-9a-f]{64}$'",
    )


def downgrade() -> None:
    """移除 receipt fingerprint，恢复 0012 精确结构。"""
    op.drop_constraint(
        "ck_email_feedback_receipt_fingerprint",
        "email_feedback_receipts",
        type_="check",
    )
    op.drop_column("email_feedback_receipts", "item_fingerprint")
