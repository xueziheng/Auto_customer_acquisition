"""Phase 1 员工工作上传、提取与人工确认版本链。

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "work_uploads",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("upload_id", sa.String(40), nullable=False),
        sa.Column("artifact_id", sa.String(40), nullable=False),
        sa.Column("employee_id", sa.String(40), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("customer_timezone", sa.String(100), nullable=False),
        sa.Column("account_id", sa.String(40), nullable=True),
        sa.Column("opportunity_id", sa.String(40), nullable=True),
        sa.Column("need_id", sa.String(40), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "upload_id", name="pk_work_uploads"),
        sa.UniqueConstraint(
            "tenant_id", "artifact_id", name="uq_work_uploads_artifact"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "artifact_id"],
            ["raw_artifacts.tenant_id", "raw_artifacts.artifact_id"],
            name="fk_work_uploads_artifact",
        ),
        sa.CheckConstraint(
            "source_kind IN ('chat_transcript','email_text','pdf_text',"
            "'spreadsheet_text','audio_transcript','image_ocr')",
            name="ck_work_uploads_source_kind",
        ),
        sa.CheckConstraint(
            "status IN ('uploaded','extracting','awaiting_confirmation',"
            "'confirmed','failed')",
            name="ck_work_uploads_status",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(upload_id) <> '' AND "
            "btrim(artifact_id) <> '' AND btrim(employee_id) <> '' AND "
            "btrim(customer_timezone) <> ''",
            name="ck_work_uploads_core_nonblank",
        ),
    )
    op.create_index(
        "ix_work_uploads_tenant_employee_created",
        "work_uploads",
        ["tenant_id", "employee_id", "created_at", "upload_id"],
    )
    op.create_table(
        "extracted_facts",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("extraction_id", sa.String(40), nullable=False),
        sa.Column("upload_id", sa.String(40), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("extracted_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "extraction_id", name="pk_extracted_facts"
        ),
        sa.UniqueConstraint("tenant_id", "upload_id", name="uq_extracted_facts_upload"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "upload_id"],
            ["work_uploads.tenant_id", "work_uploads.upload_id"],
            name="fk_extracted_facts_upload",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_extracted_facts_payload_jsonb",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(extraction_id) <> '' AND "
            "btrim(upload_id) <> '' AND btrim(extracted_by) <> ''",
            name="ck_extracted_facts_core_nonblank",
        ),
    )
    op.create_table(
        "employee_confirmations",
        sa.Column("tenant_id", sa.String(40), nullable=False),
        sa.Column("confirmation_id", sa.String(40), nullable=False),
        sa.Column("extraction_id", sa.String(40), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("confirmed_by", sa.String(40), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "confirmation_id", name="pk_employee_confirmations"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "extraction_id",
            name="uq_employee_confirmations_extraction",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "extraction_id"],
            ["extracted_facts.tenant_id", "extracted_facts.extraction_id"],
            name="fk_employee_confirmations_extraction",
        ),
        sa.CheckConstraint("revision > 0", name="ck_employee_confirmations_revision"),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_employee_confirmations_payload_jsonb",
        ),
        sa.CheckConstraint(
            "btrim(tenant_id) <> '' AND btrim(confirmation_id) <> '' AND "
            "btrim(extraction_id) <> '' AND btrim(confirmed_by) <> ''",
            name="ck_employee_confirmations_core_nonblank",
        ),
    )
    op.execute(
        "CREATE FUNCTION reject_work_intake_version_mutation() RETURNS trigger "
        "AS $$ BEGIN RAISE EXCEPTION 'work intake version is append-only' "
        "USING ERRCODE='23514'; END; $$ LANGUAGE plpgsql"
    )
    for table in ("extracted_facts", "employee_confirmations"):
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON "
            f"{table} FOR EACH ROW EXECUTE FUNCTION "
            "reject_work_intake_version_mutation()"
        )


def downgrade() -> None:
    for table in ("employee_confirmations", "extracted_facts"):
        op.execute(f"DROP TRIGGER trg_{table}_append_only ON {table}")
    op.execute("DROP FUNCTION reject_work_intake_version_mutation()")
    op.drop_table("employee_confirmations")
    op.drop_table("extracted_facts")
    op.drop_index("ix_work_uploads_tenant_employee_created", table_name="work_uploads")
    op.drop_table("work_uploads")
