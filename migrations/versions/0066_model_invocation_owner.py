"""持锁后台恢复过期模型调用；不释放未知费用与槽位。"""
import sqlalchemy as sa
from alembic import op

revision = '0066'
down_revision = '0065'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('model_invocations', sa.Column('owner_id', sa.String(128)))
    op.add_column('model_invocations', sa.Column('lease_expires_at', sa.DateTime(timezone=True)))


def downgrade() -> None:
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM model_invocations WHERE owner_id IS NOT NULL)")).scalar():
        raise RuntimeError('已有模型租约，不能降级删除恢复依据')
    op.drop_column('model_invocations', 'lease_expires_at')
    op.drop_column('model_invocations', 'owner_id')
