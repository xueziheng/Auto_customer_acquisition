"""持久化通知任务和只读站内收件箱。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """建立可重试的任务投影与仅可标记已读的收件箱行。"""
    op.create_table("notification_jobs", sa.Column("tenant_id", sa.String(32), nullable=False), sa.Column("notification_job_id", sa.String(32), nullable=False), sa.Column("source_event_fingerprint", sa.String(64), nullable=False), sa.Column("source_event", sa.String(100), nullable=False), sa.Column("recipient_employee_id", sa.String(32), nullable=False), sa.Column("priority", sa.String(16), nullable=False), sa.Column("context_kind", sa.String(64), nullable=False), sa.Column("primary_id", sa.String(100), nullable=False), sa.Column("secondary_id", sa.String(100)), sa.Column("reason_code", sa.String(100)), sa.Column("level", sa.Integer()), sa.Column("dedup_key", sa.String(200), nullable=False), sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")), sa.Column("available_at", sa.DateTime(timezone=True), nullable=False), sa.Column("lease_owner", sa.String(100)), sa.Column("lease_token", sa.String(32)), sa.Column("lease_expires_at", sa.DateTime(timezone=True)), sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")), sa.Column("last_error", sa.String(100)), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("completed_at", sa.DateTime(timezone=True)), sa.PrimaryKeyConstraint("tenant_id", "notification_job_id", name="pk_notification_jobs"), sa.UniqueConstraint("tenant_id", "source_event_fingerprint", "recipient_employee_id", "context_kind", name="uq_notification_jobs_source_recipient_kind"), sa.CheckConstraint("status IN ('pending','processing','completed','rejected')", name="ck_notification_jobs_status"), sa.CheckConstraint("priority IN ('urgent','normal','low')", name="ck_notification_jobs_priority"), sa.CheckConstraint("attempt_count >= 0", name="ck_notification_jobs_attempt_count"))
    op.create_index("ix_notification_jobs_tenant_due", "notification_jobs", ["tenant_id", "status", "available_at"])
    op.create_table("in_app_notifications", sa.Column("tenant_id", sa.String(32), nullable=False), sa.Column("notification_id", sa.String(32), nullable=False), sa.Column("recipient_employee_id", sa.String(32), nullable=False), sa.Column("priority", sa.String(16), nullable=False), sa.Column("title", sa.String(200), nullable=False), sa.Column("context_kind", sa.String(64), nullable=False), sa.Column("primary_id", sa.String(100), nullable=False), sa.Column("secondary_id", sa.String(100)), sa.Column("reason_code", sa.String(100)), sa.Column("level", sa.Integer()), sa.Column("relative_link", sa.String(500)), sa.Column("source_job_id", sa.String(32), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("read_at", sa.DateTime(timezone=True)), sa.PrimaryKeyConstraint("tenant_id", "notification_id", name="pk_in_app_notifications"), sa.UniqueConstraint("tenant_id", "source_job_id", name="uq_in_app_notifications_source_job"), sa.ForeignKeyConstraint(["tenant_id", "source_job_id"], ["notification_jobs.tenant_id", "notification_jobs.notification_job_id"], ondelete="RESTRICT", name="fk_in_app_notifications_job"), sa.CheckConstraint("priority IN ('urgent','normal','low')", name="ck_in_app_notifications_priority"))
    op.create_index("ix_in_app_notifications_recipient_created", "in_app_notifications", ["tenant_id", "recipient_employee_id", "created_at", "notification_id"])
    op.execute("CREATE FUNCTION guard_in_app_notification_update() RETURNS trigger AS $$ BEGIN IF NEW.read_at IS NOT NULL AND OLD.read_at IS NULL AND NEW.tenant_id=OLD.tenant_id AND NEW.notification_id=OLD.notification_id AND NEW.recipient_employee_id=OLD.recipient_employee_id AND NEW.priority=OLD.priority AND NEW.title=OLD.title AND NEW.context_kind=OLD.context_kind AND NEW.primary_id=OLD.primary_id AND NEW.secondary_id IS NOT DISTINCT FROM OLD.secondary_id AND NEW.reason_code IS NOT DISTINCT FROM OLD.reason_code AND NEW.level IS NOT DISTINCT FROM OLD.level AND NEW.relative_link IS NOT DISTINCT FROM OLD.relative_link AND NEW.source_job_id=OLD.source_job_id AND NEW.created_at=OLD.created_at THEN RETURN NEW; END IF; RAISE EXCEPTION 'in_app_notifications immutable'; END; $$ LANGUAGE plpgsql")
    op.execute("CREATE TRIGGER trg_in_app_notification_immutable BEFORE UPDATE OR DELETE ON in_app_notifications FOR EACH ROW EXECUTE FUNCTION guard_in_app_notification_update()")


def downgrade() -> None:
    """移除本迁移所有表与触发器函数。"""
    op.drop_table("in_app_notifications")
    op.execute("DROP FUNCTION IF EXISTS guard_in_app_notification_update()")
    op.drop_table("notification_jobs")
