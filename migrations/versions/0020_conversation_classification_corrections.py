"""0020 会话分类纠正留痕表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CATEGORIES = (
    "'clear_interest','willing_to_continue','requests_materials','requests_quote',"
    "'requests_sample','provides_specification','no_current_need',"
    "'future_need_possible','refers_other_contact','rejection','unsubscribe',"
    "'bounce','auto_reply','complaint'"
)


def upgrade() -> None:
    """创建 tenant-bound 纠正留痕表：PK(tenant, correction_id)，
    UNIQUE(tenant, message, corrected_by, corrected_category) 作为 DB 幂等键。"""
    op.create_table(
        "conversation_classification_corrections",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("correction_id", sa.String(32), nullable=False),
        sa.Column("message_id", sa.String(100), nullable=False),
        sa.Column("corrected_category", sa.String(40), nullable=False),
        sa.Column("corrected_by", sa.String(100), nullable=False),
        sa.Column("corrected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "correction_id",
            name="pk_conversation_classification_corrections",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "message_id",
            "corrected_by",
            "corrected_category",
            name="uq_conversation_classification_corrections_idem",
        ),
        sa.CheckConstraint(
            f"corrected_category IN ({_CATEGORIES})",
            name="ck_conversation_classification_corrections_category",
        ),
    )


def downgrade() -> None:
    op.drop_table("conversation_classification_corrections")
