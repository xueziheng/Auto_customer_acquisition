"""0014 不可变 Artifact metadata。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TENANT = "tenant_id ~ '^tn_[0-7][0-9A-HJKMNP-TV-Z]{25}$'"
_ARTIFACT = "artifact_id ~ '^art_[0-7][0-9A-HJKMNP-TV-Z]{25}$'"
_HASH = "content_hash ~ '^[0-9a-f]{64}$'"
_RAW_KIND_MIME = (
    "(kind='email_raw' AND mime_type='message/rfc822') OR "
    "(kind='chat_screenshot' AND mime_type IN "
    "('image/png','image/jpeg','image/webp')) OR "
    "(kind='pdf' AND mime_type='application/pdf') OR "
    "(kind='word' AND mime_type="
    "'application/vnd.openxmlformats-officedocument.wordprocessingml.document') OR "
    "(kind='excel' AND mime_type IN "
    "('application/vnd.openxmlformats-officedocument.spreadsheetml.sheet','text/csv')) OR "
    "(kind='web_snapshot' AND mime_type='text/html') OR "
    "(kind='image' AND mime_type IN ('image/png','image/jpeg','image/webp')) OR "
    "(kind='audio' AND mime_type IN ('audio/mpeg','audio/wav','audio/mp4'))"
)


def upgrade() -> None:
    """增加 Raw 与 Generated 两张严格分离的 metadata 表。"""
    op.create_table(
        "raw_artifacts",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("artifact_id", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("object_key", sa.String(128), nullable=False),
        sa.Column("uploaded_by", sa.String(32), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "artifact_id", name="pk_raw_artifacts"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "kind",
            "content_hash",
            name="uq_raw_artifacts_tenant_kind_hash",
        ),
        sa.CheckConstraint(_TENANT, name="ck_raw_artifacts_tenant"),
        sa.CheckConstraint(_ARTIFACT, name="ck_raw_artifacts_id"),
        sa.CheckConstraint(_HASH, name="ck_raw_artifacts_hash"),
        sa.CheckConstraint("size_bytes > 0", name="ck_raw_artifacts_size"),
        sa.CheckConstraint(
            _RAW_KIND_MIME, name="ck_raw_artifacts_kind_mime"
        ),
        sa.CheckConstraint(
            "object_key = 'raw/' || tenant_id || '/' || artifact_id",
            name="ck_raw_artifacts_object_key",
        ),
        sa.CheckConstraint(
            "uploaded_by IS NULL OR uploaded_by ~ "
            "'^usr_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_raw_artifacts_uploader",
        ),
    )
    op.create_table(
        "artifacts",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("artifact_id", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("object_key", sa.String(128), nullable=False),
        sa.Column("workflow_run_id", sa.String(32), nullable=False),
        sa.Column("subject_ref", sa.String(32), nullable=False),
        sa.Column("sequence_number", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("generated_by", sa.String(64), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "artifact_id", name="pk_artifacts"),
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_artifacts_tenant_key"
        ),
        sa.CheckConstraint(_TENANT, name="ck_artifacts_tenant"),
        sa.CheckConstraint(_ARTIFACT, name="ck_artifacts_id"),
        sa.CheckConstraint(_HASH, name="ck_artifacts_hash"),
        sa.CheckConstraint("size_bytes > 0", name="ck_artifacts_size"),
        sa.CheckConstraint(
            "kind='email_draft' AND mime_type="
            "'application/vnd.tradeos.email-draft+json'",
            name="ck_artifacts_kind_mime",
        ),
        sa.CheckConstraint(
            "object_key = 'generated/' || tenant_id || '/' || artifact_id",
            name="ck_artifacts_object_key",
        ),
        sa.CheckConstraint(
            "workflow_run_id ~ '^run_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_artifacts_run",
        ),
        sa.CheckConstraint(
            "subject_ref ~ '^enr_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_artifacts_subject",
        ),
        sa.CheckConstraint(
            "sequence_number > 0", name="ck_artifacts_sequence"
        ),
        sa.CheckConstraint(
            "idempotency_key = subject_ref || ':' || sequence_number::text "
            "|| ':' || 'draft'",
            name="ck_artifacts_idempotency",
        ),
        sa.CheckConstraint(
            "generated_by ~ '^[a-z][a-z0-9_-]{0,63}$'",
            name="ck_artifacts_generated_by",
        ),
    )


def downgrade() -> None:
    """只移除 Artifact Store 0014 新增的两张表。"""
    op.drop_table("artifacts")
    op.drop_table("raw_artifacts")
