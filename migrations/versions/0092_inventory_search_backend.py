"""库存任务固定搜索后端与保守请求预留；继承原表租户RLS。"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("acquisition_jobs", sa.Column("search_binding", postgresql.JSONB(), nullable=True))
    op.add_column("acquisition_jobs", sa.Column("search_reservations", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")))


def downgrade() -> None:
    op.execute("LOCK TABLE acquisition_jobs IN ACCESS EXCLUSIVE MODE")
    op.execute("DO $block$ BEGIN IF EXISTS (SELECT 1 FROM acquisition_jobs WHERE search_binding IS NOT NULL OR search_reservations <> '[]'::jsonb) THEN RAISE EXCEPTION 'search reservations prevent downgrade'; END IF; END $block$")
    op.drop_column("acquisition_jobs", "search_reservations")
    op.drop_column("acquisition_jobs", "search_binding")
