"""持久化 DNS 认证检查请求状态机。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建 tenant-bound、幂等且不可删除/改绑的认证请求。"""
    op.create_table(
        "sending_auth_check_requests",
        sa.Column("tenant_id", sa.String(32), nullable=False),
        sa.Column("request_id", sa.String(32), nullable=False),
        sa.Column("sending_identity_id", sa.String(32), nullable=False),
        sa.Column("request_key", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint(
            "tenant_id", "request_id", name="pk_sending_auth_check_requests"
        ),
        sa.UniqueConstraint(
            "tenant_id", "request_key", name="uq_sending_auth_request_tenant_key"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "sending_identity_id"],
            ["sending_identities.tenant_id", "sending_identities.identity_id"],
            ondelete="RESTRICT",
            name="fk_sending_auth_request_identity",
        ),
        sa.CheckConstraint(
            "status IN ('requested','running','succeeded','failed')",
            name="ck_sending_auth_request_status",
        ),
        sa.CheckConstraint(
            "(status IN ('requested','running') AND completed_at IS NULL) OR "
            "(status IN ('succeeded','failed') AND completed_at IS NOT NULL)",
            name="ck_sending_auth_request_completion",
        ),
        sa.CheckConstraint(
            "request_id ~ '^acr_[0-7][0-9A-HJKMNP-TV-Z]{25}$' AND "
            "sending_identity_id ~ '^sid_[0-7][0-9A-HJKMNP-TV-Z]{25}$'",
            name="ck_sending_auth_request_ids",
        ),
        sa.CheckConstraint(
            "request_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$' AND "
            "lower(request_key) !~ '(bearer|token|secret|password|authorization)'",
            name="ck_sending_auth_request_key",
        ),
    )
    op.execute(
        "CREATE FUNCTION guard_sending_auth_request_mutation() RETURNS trigger AS $$ "
        "BEGIN "
        "IF TG_OP='DELETE' THEN RAISE EXCEPTION 'sending auth request immutable'; END IF; "
        "IF NEW.tenant_id<>OLD.tenant_id OR NEW.request_id<>OLD.request_id OR "
        "NEW.sending_identity_id<>OLD.sending_identity_id OR "
        "NEW.request_key<>OLD.request_key OR NEW.requested_at<>OLD.requested_at THEN "
        "RAISE EXCEPTION 'sending auth request immutable'; END IF; "
        "IF (OLD.status='requested' AND NEW.status='running' AND NEW.completed_at IS NULL) "
        "OR (OLD.status='running' AND NEW.status IN ('succeeded','failed') "
        "AND NEW.completed_at IS NOT NULL) THEN RETURN NEW; END IF; "
        "RAISE EXCEPTION 'sending auth request transition invalid'; "
        "END; $$ LANGUAGE plpgsql"
    )
    op.execute(
        "CREATE TRIGGER trg_sending_auth_request_guard BEFORE UPDATE OR DELETE ON "
        "sending_auth_check_requests FOR EACH ROW EXECUTE FUNCTION "
        "guard_sending_auth_request_mutation()"
    )


def downgrade() -> None:
    """移除认证请求表与守卫函数。"""
    op.drop_table("sending_auth_check_requests")
    op.execute("DROP FUNCTION IF EXISTS guard_sending_auth_request_mutation()")
