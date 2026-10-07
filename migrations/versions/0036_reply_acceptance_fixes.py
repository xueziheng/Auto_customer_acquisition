"""回复退订范围与耐久动作验收修复。

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_classifications",
        sa.Column("suppress_scope", sa.String(16), nullable=True),
    )
    op.execute(
        "UPDATE conversation_classifications SET suppress_scope = 'contact' "
        "WHERE category = 'unsubscribe'"
    )
    op.create_check_constraint(
        "ck_conversation_classifications_suppress_scope",
        "conversation_classifications",
        "(category = 'unsubscribe' AND suppress_scope IN ('contact','account')) "
        "OR (category <> 'unsubscribe' AND suppress_scope IS NULL)",
    )
    op.create_table(
        "conversation_reply_work",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("action_id", sa.String(32), nullable=False),
        sa.Column("message_id", sa.String(32), nullable=False),
        sa.Column("outbound_message_id", sa.String(256), nullable=False),
        sa.Column("enrollment_id", sa.String(40), nullable=False),
        sa.Column("account_id", sa.String(40), nullable=False),
        sa.Column("contact_point_id", sa.String(40), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("owner_queue", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "action_id", name="pk_conversation_reply_work"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "message_id",
            "action",
            name="uq_conversation_reply_work_message_action",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "idempotency_key",
            name="uq_conversation_reply_work_idempotency",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "message_id"],
            ["messages.tenant_id", "messages.message_id"],
            ondelete="RESTRICT",
            name="fk_conversation_reply_work_message",
        ),
        sa.CheckConstraint(
            "action IN ('start_qualification','mark_future_restart',"
            "'create_follow_up','intake_new_contact')",
            name="ck_conversation_reply_work_action",
        ),
        sa.CheckConstraint(
            "status IN ('pending','in_progress','completed','cancelled')",
            name="ck_conversation_reply_work_status",
        ),
        sa.CheckConstraint(
            "(action = 'start_qualification' AND owner_queue = 'need_qualification') OR "
            "(action = 'mark_future_restart' AND owner_queue = 'future_restart_review') OR "
            "(action = 'create_follow_up' AND owner_queue = 'follow_up') OR "
            "(action = 'intake_new_contact' AND "
            "owner_queue = 'verified_contact_intake_review')",
            name="ck_conversation_reply_work_queue",
        ),
    )
    op.create_index(
        "ix_conversation_reply_work_owner_queue",
        "conversation_reply_work",
        ["tenant_id", "status", "owner_queue", "created_at", "action_id"],
    )


def downgrade() -> None:
    op.drop_table("conversation_reply_work")
    op.drop_constraint(
        "ck_conversation_classifications_suppress_scope",
        "conversation_classifications",
        type_="check",
    )
    op.drop_column("conversation_classifications", "suppress_scope")
