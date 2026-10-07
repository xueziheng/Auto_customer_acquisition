"""0007 人工接管升级审计表。

接管升级是只增审计事实；复合外键保证 tenant_id 与 handoff 同属一个租户，
每个接管的每一级升级只允许记录一次。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """增加接管复合唯一键并创建只增升级审计表。"""
    op.create_unique_constraint(
        "uq_handoffs_tenant_handoff",
        "handoffs",
        ["tenant_id", "handoff_id"],
    )
    op.create_table(
        "handoff_escalations",
        sa.Column("escalation_id", sa.String(32), primary_key=True, nullable=False),
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("handoff_id", sa.String(32), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "handoff_id"],
            ["handoffs.tenant_id", "handoffs.handoff_id"],
            ondelete="RESTRICT",
            name="fk_handoff_escalations_handoff",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "handoff_id",
            "level",
            name="uq_handoff_escalations_tenant_handoff_level",
        ),
    )
    op.execute(
        """
CREATE FUNCTION handoff_escalations_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'handoff_escalations is append-only';
END;
$$ LANGUAGE plpgsql;
"""
    )
    op.execute(
        """
CREATE TRIGGER handoff_escalations_append_only
BEFORE UPDATE OR DELETE ON handoff_escalations
FOR EACH ROW EXECUTE FUNCTION handoff_escalations_append_only();
"""
    )


def downgrade() -> None:
    """精确恢复 0006：先删触发器与子表，再删 handoffs 新唯一键。"""
    op.execute(
        "DROP TRIGGER handoff_escalations_append_only ON handoff_escalations;"
    )
    op.drop_table("handoff_escalations")
    op.execute("DROP FUNCTION handoff_escalations_append_only();")
    op.drop_constraint(
        "uq_handoffs_tenant_handoff", "handoffs", type_="unique"
    )
