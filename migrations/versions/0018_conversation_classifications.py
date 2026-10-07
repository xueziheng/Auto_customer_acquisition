"""0018 会话回复分类留痕表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CATEGORIES = (
    "'clear_interest','willing_to_continue','requests_materials','requests_quote',"
    "'requests_sample','provides_specification','no_current_need',"
    "'future_need_possible','refers_other_contact','rejection','unsubscribe',"
    "'bounce','auto_reply','complaint'"
)


def upgrade() -> None:
    """创建 tenant-bound 分类留痕表：每 (tenant, message) 至多一条（跨版本重评
    显式拒绝，唯一约束在并发下强制单行）；不含任何置信度/正文列。"""
    op.create_table(
        "conversation_classifications",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("message_id", sa.String(100), nullable=False),
        sa.Column("category", sa.String(40), nullable=False),
        sa.Column("classified_by", sa.String(100), nullable=False),
        sa.Column("classified_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id",
            "message_id",
            name="pk_conversation_classifications",
        ),
        sa.CheckConstraint(
            f"category IN ({_CATEGORIES})",
            name="ck_conversation_classifications_category",
        ),
    )


def downgrade() -> None:
    op.drop_table("conversation_classifications")
