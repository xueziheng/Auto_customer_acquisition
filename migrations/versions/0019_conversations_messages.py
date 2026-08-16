"""0019 会话与消息持久化表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """conversations（tenant+account+channel 唯一）+ messages（external
    Message-ID 唯一，tenant-scoped FK）；不存 subject/body/正文（边界 4）。"""
    op.create_table(
        "conversations",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("conversation_id", sa.String(32), nullable=False),
        sa.Column("account_id", sa.String(32), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id", "conversation_id", name="pk_conversations"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "account_id",
            "channel",
            name="uq_conversations_tenant_account_channel",
        ),
    )
    op.create_table(
        "messages",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("message_id", sa.String(32), nullable=False),
        sa.Column("conversation_id", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(16), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("language", sa.String(16), nullable=True),
        sa.Column("raw_artifact_ref", sa.String(100), nullable=False),
        sa.Column("external_message_id", sa.String(256), nullable=False),
        sa.Column("outbound_message_id", sa.String(256), nullable=True),
        sa.PrimaryKeyConstraint(
            "tenant_id", "message_id", name="pk_messages"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "external_message_id",
            name="uq_messages_tenant_external_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.conversation_id"],
            ondelete="RESTRICT",
            name="fk_messages_conversation",
        ),
        sa.CheckConstraint(
            "direction IN ('inbound', 'outbound')",
            name="ck_messages_direction",
        ),
        sa.CheckConstraint(
            "length(btrim(external_message_id)) > 0",
            name="ck_messages_external_id_nonblank",
        ),
        sa.CheckConstraint(
            "length(btrim(raw_artifact_ref)) > 0",
            name="ck_messages_raw_ref_nonblank",
        ),
    )


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_table("conversations")
