"""回复分类持久化逐字字段证据。

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversation_classifications",
        sa.Column(
            "candidate_fields",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(
        "ck_conversation_classifications_candidate_fields",
        "conversation_classifications",
        "jsonb_typeof(candidate_fields) = 'array'",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_conversation_classifications_candidate_fields",
        "conversation_classifications",
        type_="check",
    )
    op.drop_column("conversation_classifications", "candidate_fields")
