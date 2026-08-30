"""保留公开页面拒绝的三类安全原因。"""

from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None

_OLD = (
    "'validation','permission_denied','suppressed','approval_required',"
    "'idempotency_conflict','in_progress','rate_limited','provider_auth_required',"
    "'provider_permanent','provider_transient','reconciliation_required','unexpected'"
)
_NEW = (
    "'validation','permission_denied','suppressed','approval_required',"
    "'idempotency_conflict','in_progress','rate_limited','provider_auth_required',"
    "'provider_permanent','provider_transient','page_access_forbidden',"
    "'login_or_captcha','unsafe_redirect','reconciliation_required','unexpected'"
)


def _replace(table: str, constraint: str, column: str, values: str) -> None:
    op.drop_constraint(constraint, table, type_="check")
    op.create_check_constraint(
        constraint,
        table,
        f"{column} IS NULL OR {column} IN ({values})",
    )


def upgrade() -> None:
    _replace("tool_calls", "ck_tool_calls_error_category", "error_category", _NEW)
    _replace("tool_call_events", "ck_tool_call_events_category", "category", _NEW)


def downgrade() -> None:
    _replace("tool_call_events", "ck_tool_call_events_category", "category", _OLD)
    _replace("tool_calls", "ck_tool_calls_error_category", "error_category", _OLD)
