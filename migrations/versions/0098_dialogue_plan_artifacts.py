"""普通对话生成计划有独立来源绑定，不冒充冷邮件草稿或Raw。"""
from alembic import op

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None
_OLD = "(kind='email_draft' AND mime_type='application/vnd.tradeos.email-draft+json' AND subject_ref ~ '^enr_[0-7][0-9A-HJKMNP-TV-Z]{25}$' AND idempotency_key=subject_ref || ':' || sequence_number::text || ':' || 'draft' AND generated_by ~ '^[a-z][a-z0-9_-]{0,63}$') OR (kind='quote_pdf' AND mime_type='application/pdf' AND subject_ref ~ '^quo_[0-7][0-9A-HJKMNP-TV-Z]{25}$' AND generated_by='quote_pdf_v1' AND idempotency_key=subject_ref || ':' || sequence_number::text || ':quote_pdf:' || generated_by)"
_NEW = "(kind='email_draft' AND mime_type='application/vnd.tradeos.email-draft+json' AND subject_ref ~ '^enr_[0-7][0-9A-HJKMNP-TV-Z]{25}$' AND idempotency_key=subject_ref || ':' || sequence_number::text || ':' || 'draft' AND generated_by ~ '^[a-z][a-z0-9_-]{0,63}$') OR (kind='quote_pdf' AND mime_type='application/pdf' AND subject_ref ~ '^quo_[0-7][0-9A-HJKMNP-TV-Z]{25}$' AND generated_by='quote_pdf_v1' AND idempotency_key=subject_ref || ':' || sequence_number::text || ':quote_pdf:' || generated_by) OR (kind='dialogue_plan' AND mime_type='application/vnd.tradeos.dialogue-plan+json' AND subject_ref ~ '^msg_[0-7][0-9A-HJKMNP-TV-Z]{25}$' AND generated_by='ordinary_dialogue_v1' AND idempotency_key=subject_ref || ':' || sequence_number::text || ':' || 'dialogue_plan')"

def upgrade() -> None:
    op.drop_constraint("ck_artifacts_binding", "artifacts", type_="check")
    op.create_check_constraint("ck_artifacts_binding", "artifacts", _NEW)

def downgrade() -> None:
    op.drop_constraint("ck_artifacts_binding", "artifacts", type_="check")
    op.create_check_constraint("ck_artifacts_binding", "artifacts", _OLD)
