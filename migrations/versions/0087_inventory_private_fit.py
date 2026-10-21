"""企业客户搜索适配批次检查点，复用现有任务租户/RLS与租约。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0087"
down_revision = "0086"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "acquisition_jobs",
        sa.Column(
            "fit_batches",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("acquisition_jobs", "fit_batches")
