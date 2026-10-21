"""准备任务保存最初实际归属版本，防转交后补造授权。"""
import sqlalchemy as sa
from alembic import op

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("customer_preparation_items", sa.Column("ownership_locked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.execute("LOCK TABLE customer_preparation_items IN ACCESS EXCLUSIVE MODE")
    op.execute("DO $block$ BEGIN IF EXISTS (SELECT 1 FROM customer_preparation_items WHERE ownership_locked_at IS NOT NULL) THEN RAISE EXCEPTION 'preparation ownership binding prevents downgrade'; END IF; END $block$")
    op.drop_column("customer_preparation_items", "ownership_locked_at")
