"""回复工作可引用真实询盘绑定，无需伪造冷Enrollment。"""
import sqlalchemy as sa
from alembic import op

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("conversation_reply_work", sa.Column("dialogue_binding_id", sa.String(40), nullable=True))
    op.alter_column("conversation_reply_work", "enrollment_id", nullable=True)
    op.alter_column("conversation_reply_work", "outbound_message_id", nullable=True)
    op.create_foreign_key("fk_reply_work_dialogue", "conversation_reply_work", "dialogue_bindings", ["tenant_id", "dialogue_binding_id"], ["tenant_id", "binding_id"])
    op.create_check_constraint("ck_reply_work_source", "conversation_reply_work", "(enrollment_id IS NOT NULL AND outbound_message_id IS NOT NULL) OR dialogue_binding_id IS NOT NULL")


def downgrade() -> None:
    # 有真实无Enrollment询盘时禁止有损回退；必须由操作者先处理迁移策略。
    op.alter_column("conversation_reply_work", "enrollment_id", nullable=False)
    op.alter_column("conversation_reply_work", "outbound_message_id", nullable=False)
    op.drop_constraint("ck_reply_work_source", "conversation_reply_work", type_="check")
    op.drop_constraint("fk_reply_work_dialogue", "conversation_reply_work", type_="foreignkey")
    op.drop_column("conversation_reply_work", "dialogue_binding_id")
