"""报价PDF派生分支与只增文件关联；不处理客户文件当前授权。"""

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

_BRANCHES = "(kind='email_draft' AND mime_type='application/vnd.tradeos.email-draft+json' AND subject_ref ~ '^enr_[0-7][0-9A-HJKMNP-TV-Z]{25}$' AND idempotency_key=subject_ref || ':' || sequence_number::text || ':' || 'draft' AND generated_by ~ '^[a-z][a-z0-9_-]{0,63}$') OR (kind='quote_pdf' AND mime_type='application/pdf' AND subject_ref ~ '^quo_[0-7][0-9A-HJKMNP-TV-Z]{25}$' AND generated_by='quote_pdf_v1' AND idempotency_key=subject_ref || ':' || sequence_number::text || ':quote_pdf:' || generated_by)"
_LEGACY = {
    "ck_artifacts_kind_mime": "kind='email_draft' AND mime_type='application/vnd.tradeos.email-draft+json'",
    "ck_artifacts_subject": "subject_ref ~ '^enr_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
    "ck_artifacts_idempotency": "idempotency_key = subject_ref || ':' || sequence_number::text || ':' || 'draft'",
    "ck_artifacts_generated_by": "generated_by ~ '^[a-z][a-z0-9_-]{0,63}$'",
}


def upgrade() -> None:
    """两分支互斥，SQL模板冻结在本迁移，不导入未来注册表。"""
    for name in _LEGACY:
        op.drop_constraint(name, "artifacts", type_="check")
    op.create_check_constraint("ck_artifacts_binding", "artifacts", _BRANCHES)
    op.create_table(
        "quotation_files",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("file_id", sa.String(40), nullable=False),
        sa.Column("quote_id", sa.String(40), nullable=False),
        sa.Column("quote_version", sa.Integer(), nullable=False),
        sa.Column("artifact_id", sa.String(32), nullable=False),
        sa.Column("quote_content_hash", sa.String(64), nullable=False),
        sa.Column("customer_content_hash", sa.String(64), nullable=False),
        sa.Column("artifact_hash", sa.String(64), nullable=False),
        sa.Column("template_version", sa.String(64), nullable=False),
        sa.Column("approval_run_id", sa.String(40), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "file_id", name="pk_quotation_files"),
        sa.UniqueConstraint("tenant_id", "quote_id", "template_version", name="uq_quote_file_template"),
        sa.ForeignKeyConstraint(["tenant_id", "quote_id"], ["quotations.tenant_id", "quotations.quote_id"], name="fk_quote_file_quote", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "artifact_id"], ["artifacts.tenant_id", "artifacts.artifact_id"], name="fk_quote_file_artifact", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["tenant_id", "quote_id"], ["quotation_approval_receipts.tenant_id", "quotation_approval_receipts.quote_id"], name="fk_quote_file_receipt", ondelete="RESTRICT"),
        sa.CheckConstraint("quote_version>0 AND size_bytes>0 AND isfinite(generated_at)", name="ck_quote_file_positive"),
        sa.CheckConstraint("template_version='quote_pdf_v1'", name="ck_quote_file_template"),
        sa.CheckConstraint("quote_content_hash ~ '^[0-9a-f]{64}$' AND customer_content_hash ~ '^[0-9a-f]{64}$' AND artifact_hash ~ '^[0-9a-f]{64}$'", name="ck_quote_file_hash"),
        sa.CheckConstraint("tenant_id ~ '^tn_[0-7][0-9A-HJKMNP-TV-Z]{25}$' AND file_id ~ '^qfl_[0-7][0-9A-HJKMNP-TV-Z]{25}$' AND quote_id ~ '^quo_[0-7][0-9A-HJKMNP-TV-Z]{25}$' AND artifact_id ~ '^art_[0-7][0-9A-HJKMNP-TV-Z]{25}$' AND approval_run_id ~ '^run_[0-7][0-9A-HJKMNP-TV-Z]{25}$'", name="ck_quote_file_ids"),
    )


def downgrade() -> None:
    """新业务数据存在时固定拒绝，不删除关联或派生产物换取降级。"""
    if op.get_bind().execute(sa.text(
        "SELECT EXISTS(SELECT 1 FROM quotation_files) OR EXISTS(SELECT 1 FROM artifacts WHERE kind='quote_pdf')"
    )).scalar():
        raise RuntimeError("quote_pdf_downgrade_refused")
    op.drop_table("quotation_files")
    op.drop_constraint("ck_artifacts_binding", "artifacts", type_="check")
    for name, expression in _LEGACY.items():
        op.create_check_constraint(name, "artifacts", expression)
