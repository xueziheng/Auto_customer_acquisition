"""固定采集计划与累计时间，来源阶段复用原尝试账本及租户RLS。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0088"
down_revision = "0087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "acquisition_jobs",
        sa.Column(
            "source_plan",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.add_column(
        "acquisition_jobs",
        sa.Column("collection_deadline", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "acquisition_jobs",
        sa.Column(
            "collection_reserved_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    op.create_check_constraint(
        "ck_acquisition_collection_budget",
        "acquisition_jobs",
        "collection_reserved_seconds BETWEEN 0 AND 480",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_acquisition_collection_budget", "acquisition_jobs", type_="check"
    )
    op.drop_column("acquisition_jobs", "collection_reserved_seconds")
    op.drop_column("acquisition_jobs", "collection_deadline")
    op.drop_column("acquisition_jobs", "source_plan")
