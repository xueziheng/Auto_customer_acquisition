"""普通对话进展与人工核对原因，只保存有界元数据。"""
import sqlalchemy as sa
from alembic import op

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("conversation_reply_work", sa.Column("review_reason", sa.String(64), nullable=True))
    op.add_column("dialogue_executions", sa.Column("need_progress_sha256", sa.String(64), nullable=True))
    op.add_column("dialogue_bindings", sa.Column("last_need_progress_sha256", sa.String(64), nullable=True))

def downgrade() -> None:
    op.drop_column("dialogue_bindings", "last_need_progress_sha256")
    op.drop_column("dialogue_executions", "need_progress_sha256")
    op.drop_column("conversation_reply_work", "review_reason")
