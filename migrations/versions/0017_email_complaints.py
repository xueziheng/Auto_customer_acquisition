"""0017 邮件投诉：receipt kind 与 target 词表加入 complaint。"""

from collections.abc import Sequence

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KIND_WITH_COMPLAINT = (
    "kind IN ('hard_bounce','soft_bounce','unparseable','complaint')"
)
_TARGET_WITH_COMPLAINT = (
    "(result='quarantined' AND kind='unparseable' AND attempt_id IS NULL "
    "AND enrollment_id IS NULL AND account_id IS NULL "
    "AND contact_point_id IS NULL AND sending_identity_id IS NULL) OR "
    "(result IN ('applied','recorded') AND "
    "kind IN ('hard_bounce','soft_bounce','complaint') "
    "AND attempt_id IS NOT NULL AND enrollment_id IS NOT NULL "
    "AND account_id IS NOT NULL AND contact_point_id IS NOT NULL "
    "AND sending_identity_id IS NOT NULL)"
)
_KIND_LEGACY = "kind IN ('hard_bounce','soft_bounce','unparseable')"
_TARGET_LEGACY = (
    "(result='quarantined' AND kind='unparseable' AND attempt_id IS NULL "
    "AND enrollment_id IS NULL AND account_id IS NULL "
    "AND contact_point_id IS NULL AND sending_identity_id IS NULL) OR "
    "(result IN ('applied','recorded') AND kind IN ('hard_bounce','soft_bounce') "
    "AND attempt_id IS NOT NULL AND enrollment_id IS NOT NULL "
    "AND account_id IS NOT NULL AND contact_point_id IS NOT NULL "
    "AND sending_identity_id IS NOT NULL)"
)


def upgrade() -> None:
    """把 complaint 加入 receipt 词表；约束名保持不变，仅放宽内容。"""
    op.drop_constraint(
        "ck_email_feedback_receipt_kind",
        "email_feedback_receipts",
        type_="check",
    )
    op.drop_constraint(
        "ck_email_feedback_receipt_target",
        "email_feedback_receipts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_email_feedback_receipt_kind",
        "email_feedback_receipts",
        _KIND_WITH_COMPLAINT,
    )
    op.create_check_constraint(
        "ck_email_feedback_receipt_target",
        "email_feedback_receipts",
        _TARGET_WITH_COMPLAINT,
    )


def downgrade() -> None:
    """恢复 0016 精确词表；现有 complaint 行必须已不存在（append-only）。"""
    op.drop_constraint(
        "ck_email_feedback_receipt_kind",
        "email_feedback_receipts",
        type_="check",
    )
    op.drop_constraint(
        "ck_email_feedback_receipt_target",
        "email_feedback_receipts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_email_feedback_receipt_kind",
        "email_feedback_receipts",
        _KIND_LEGACY,
    )
    op.create_check_constraint(
        "ck_email_feedback_receipt_target",
        "email_feedback_receipts",
        _TARGET_LEGACY,
    )
