"""成本报价的政策、价格依据、完整性与报价汇率只增确认。

Revision ID: 0041
Revises: 0040
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def _create(name: str, identity: str, content_hash: str, *extra: object) -> None:
    """四类确认共享租户/幂等约束，不创建平行业务表。"""
    op.create_table(
        name,
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column(identity, sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column(content_hash, sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("confirmed_by", sa.String(40), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", identity, name=f"pk_{name}"),
        sa.UniqueConstraint("tenant_id", "idempotency_key", name=f"uq_{name}_key"),
        sa.CheckConstraint(
            f"request_hash ~ '^[0-9a-f]{{64}}$' AND {content_hash} ~ '^[0-9a-f]{{64}}$'",
            name=f"ck_{name}_hash",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(confirmed_by) <> '' AND btrim(idempotency_key) <> '' "
            "AND jsonb_typeof(payload) = 'object'",
            name=f"ck_{name}_confirmation",
        ),
        *extra,
    )
    op.execute(
        f"CREATE TRIGGER trg_{name}_immutable BEFORE UPDATE OR DELETE ON {name} "
        "FOR EACH ROW EXECUTE FUNCTION reject_costing_evidence_mutation()"
    )


def _artifact(name: str) -> tuple[object, ...]:
    """来源始终通过复合外键绑定当前租户原始资料。"""
    return (
        sa.Column("artifact_id", sa.String(32), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "artifact_id"],
            ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"],
            name=f"fk_{name}_artifact",
            ondelete="RESTRICT",
        ),
    )


def upgrade() -> None:
    """增量创建四张表，不改历史利润记录与旧成本汇率。"""
    op.execute("""
        CREATE FUNCTION reject_costing_evidence_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'immutable costing evidence'; END;
        $$ LANGUAGE plpgsql
    """)
    _create(
        "costing_policies",
        "policy_id",
        "content_hash",
        sa.Column("category", sa.String(200), nullable=True),
        sa.Column("effective_from", sa.DateTime(timezone=True), nullable=False),
        *_artifact("costing_policies"),
    )
    op.create_index(
        "ix_costing_policies_effective",
        "costing_policies",
        ["tenant_id", "category", "effective_from"],
    )
    _create(
        "costing_price_evidence",
        "evidence_id",
        "evidence_hash",
        sa.Column("opportunity_id", sa.String(40), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "opportunity_id"],
            ["opportunities.tenant_id", "opportunities.opportunity_id"],
            name="fk_costing_price_evidence_opportunity",
            ondelete="RESTRICT",
        ),
        *_artifact("costing_price_evidence"),
    )
    _create(
        "costing_coverage",
        "coverage_id",
        "content_hash",
        sa.Column("cost_sheet_id", sa.String(40), nullable=False),
        sa.Column("sheet_hash", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "cost_sheet_id"],
            ["cost_sheets.tenant_id", "cost_sheets.cost_sheet_id"],
            name="fk_costing_coverage_sheet",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "sheet_hash ~ '^[0-9a-f]{64}$'", name="ck_costing_coverage_sheet_hash"
        ),
    )
    _create("costing_quote_fx", "fx_id", "content_hash", *_artifact("costing_quote_fx"))


def downgrade() -> None:
    """仅移除本迁移四表和它们专属的只增函数。"""
    for name in (
        "costing_quote_fx",
        "costing_coverage",
        "costing_price_evidence",
        "costing_policies",
    ):
        op.drop_table(name)
    op.execute("DROP FUNCTION reject_costing_evidence_mutation()")
