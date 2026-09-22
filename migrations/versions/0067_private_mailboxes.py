"""本人全邮箱镜像，不改变既有客户会话与分类流程。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mailbox_accounts",
        sa.Column("tenant_id", sa.String(40), primary_key=True),
        sa.Column("mailbox_id", sa.String(40), primary_key=True),
        sa.Column("employee_id", sa.String(40), nullable=False),
        sa.Column("user_id", sa.String(40), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("phase", sa.String(20), nullable=False),
        sa.Column("cursor", sa.Text()),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("failure_code", sa.String(40)),
        sa.Column("sync_requested", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("tenant_id", "email", name="uq_mailbox_account_email"),
    )
    op.create_index(
        "ix_mailbox_owner", "mailbox_accounts", ["tenant_id", "employee_id"]
    )
    op.create_table(
        "mailbox_messages",
        sa.Column("tenant_id", sa.String(40), primary_key=True),
        sa.Column("mailbox_id", sa.String(40), primary_key=True),
        sa.Column("message_id", sa.String(64), primary_key=True),
        sa.Column("thread_id", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("sender", sa.Text(), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=False),
        sa.Column("labels", JSONB(), nullable=False),
        sa.Column("content", JSONB(), nullable=False),
        sa.Column("raw", JSONB(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "mailbox_id"],
            ["mailbox_accounts.tenant_id", "mailbox_accounts.mailbox_id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_mailbox_thread",
        "mailbox_messages",
        ["tenant_id", "mailbox_id", "thread_id", "occurred_at"],
    )


def downgrade() -> None:
    # 已导入的真实邮件不能通过降级隐式抹除。
    if (
        op.get_bind()
        .execute(sa.text("SELECT EXISTS (SELECT 1 FROM mailbox_accounts)"))
        .scalar()
    ):
        raise RuntimeError("已有邮箱镜像，拒绝破坏性降级")
    op.drop_table("mailbox_messages")
    op.drop_table("mailbox_accounts")
