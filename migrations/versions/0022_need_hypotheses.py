"""need_hypotheses / validated_needs / validated_need_field_history 表
（NeedHypothesis + ValidatedNeed 生命周期最小切片，spec 2026-08-17 §4）。

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-17
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

_HYPOTHESIS_STATUSES = "'inferred','contacting','validated','rejected'"
_NEED_STATUSES = (
    "'validated','sourcing_ready','handed_to_sourcing',"
    "'fulfilled','withdrawn','lost'"
)
_MUTABLE_NEED_FIELDS = (
    "application",
    "material",
    "size_spec",
    "quantity",
    "packaging",
    "destination",
    "required_by",
    "target_price",
    "current_supply_issue",
    "certification_required",
)


def upgrade() -> None:
    """创建 tenant-bound 三表：PK(tenant,id) + 部分唯一活跃假设索引 + FK + CHECK。"""
    op.create_table(
        "validated_needs",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("need_id", sa.String(40), nullable=False),
        sa.Column("account_id", sa.String(40), nullable=False),
        sa.Column("product_category", postgresql.JSONB(), nullable=False),
        sa.Column("source_message_id", sa.String(40), nullable=False),
        sa.Column("source_conversation_id", sa.String(40), nullable=True),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'validated'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_by", sa.String(40), nullable=True),
    )
    for field in _MUTABLE_NEED_FIELDS:
        op.add_column(
            "validated_needs",
            sa.Column(field, postgresql.JSONB(), nullable=True),
        )
    op.create_primary_key("pk_validated_needs", "validated_needs", ["tenant_id", "need_id"])
    op.create_check_constraint(
        "ck_validated_needs_status",
        "validated_needs",
        f"status IN ({_NEED_STATUSES})",
    )
    op.create_check_constraint(
        "ck_validated_needs_category_jsonb",
        "validated_needs",
        "jsonb_typeof(product_category) = 'object'",
    )
    op.create_check_constraint(
        "ck_validated_needs_core_nonblank",
        "validated_needs",
        "btrim(tenant_id) <> '' AND btrim(need_id) <> '' AND "
        "btrim(account_id) <> '' AND btrim(source_message_id) <> ''",
    )
    op.create_check_constraint(
        "ck_validated_needs_source_message_nonblank",
        "validated_needs",
        "btrim(source_message_id) <> ''",
    )
    for field in _MUTABLE_NEED_FIELDS:
        op.create_check_constraint(
            f"ck_validated_needs_{field}_jsonb",
            "validated_needs",
            f"({field} IS NULL) OR jsonb_typeof({field}) = 'object'",
        )
    op.create_table(
        "need_hypotheses",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("hypothesis_id", sa.String(40), nullable=False),
        sa.Column("account_id", sa.String(40), nullable=False),
        sa.Column("category", sa.String(200), nullable=False),
        sa.Column("reasoning", postgresql.JSONB(), nullable=False),
        sa.Column("signal_ids", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'inferred'"),
        ),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("validated_need_id", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "validated_need_id"],
            ["validated_needs.tenant_id", "validated_needs.need_id"],
            name="fk_need_hypotheses_validated_need",
        ),
        sa.PrimaryKeyConstraint("tenant_id", "hypothesis_id", name="pk_need_hypotheses"),
        sa.CheckConstraint(
            f"status IN ({_HYPOTHESIS_STATUSES})", name="ck_need_hypotheses_status"
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(hypothesis_id) <> '' AND "
            "btrim(account_id) <> '' AND btrim(category) <> ''",
            name="ck_need_hypotheses_core_nonblank",
        ),
        sa.CheckConstraint(
            "btrim(category) <> ''",
            name="ck_need_hypotheses_category_nonblank",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(reasoning) = 'object'",
            name="ck_need_hypotheses_reasoning_jsonb",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(signal_ids) = 'array'",
            name="ck_need_hypotheses_signal_ids_jsonb",
        ),
        sa.CheckConstraint(
            "(status = 'rejected') = "
            "(rejection_reason IS NOT NULL AND btrim(rejection_reason) <> '')",
            name="ck_need_hypotheses_rejection_reason",
        ),
        sa.CheckConstraint(
            "(status = 'validated') = (validated_need_id IS NOT NULL)",
            name="ck_need_hypotheses_validated_link",
        ),
    )
    op.create_index(
        "uq_need_hypotheses_active_account_category",
        "need_hypotheses",
        ["tenant_id", "account_id", "category"],
        unique=True,
        postgresql_where=sa.text("status IN ('inferred','contacting')"),
    )
    op.create_table(
        "validated_need_field_history",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("history_id", sa.String(40), nullable=False),
        sa.Column("need_id", sa.String(40), nullable=False),
        sa.Column("field_name", sa.String(64), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=True),
        sa.Column("new_value", sa.Text(), nullable=False),
        sa.Column("source_message_id", sa.String(40), nullable=False),
        sa.Column("changed_by", sa.String(40), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "need_id"],
            ["validated_needs.tenant_id", "validated_needs.need_id"],
            name="fk_validated_need_field_history_need",
        ),
        sa.PrimaryKeyConstraint(
            "tenant_id", "history_id", name="pk_validated_need_field_history"
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(history_id) <> '' AND "
            "btrim(need_id) <> '' AND btrim(field_name) <> '' AND "
            "btrim(new_value) <> '' AND btrim(source_message_id) <> ''",
            name="ck_validated_need_field_history_core_nonblank",
        ),
        sa.CheckConstraint(
            "btrim(field_name) <> ''",
            name="ck_validated_need_field_history_field_name_nonblank",
        ),
        sa.CheckConstraint(
            "btrim(new_value) <> ''",
            name="ck_validated_need_field_history_new_value_nonblank",
        ),
    )
    op.create_index(
        "ix_validated_need_field_history_need",
        "validated_need_field_history",
        ["tenant_id", "need_id", "changed_at"],
    )


def downgrade() -> None:
    op.drop_table("validated_need_field_history")
    op.drop_index("uq_need_hypotheses_active_account_category", table_name="need_hypotheses")
    op.drop_table("need_hypotheses")
    op.drop_table("validated_needs")
