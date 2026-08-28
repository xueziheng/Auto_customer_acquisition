"""仅报价结果通知的固定元数据与短员工兼容，不扩大其他通知/邮箱目录。"""

import hashlib
import re

from notification_gateway.jobs import NotificationContext, NotificationKind
from shared.errors import ValidationError

TITLE = "报价审批已有结果"
NEXT_STEP = "查看该版本报价审批结果"
SOURCE_EVENT = "QuoteApprovalResult"
OUTCOMES = frozenset({"approved", "rejected", "expired", "obsolete"})
_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_RECIPIENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,31}\Z")
_SECRET_MARKER = re.compile(
    r"(?:bearer|token|secret|password|authorization|akia|sk-)", re.IGNORECASE
)


def valid_quote_result_recipient(value: object) -> bool:
    """原样ASCII safe-label<=32且无既定凭证形态；只供新kind使用。"""
    return (
        type(value) is str
        and _RECIPIENT.fullmatch(value) is not None
        and _SECRET_MARKER.search(value) is None
    )


def quote_result_link(quote_id: str) -> str:
    return f"/costing-quotes/quotes/{quote_id}"


def quote_result_context(
    tenant_id: str,
    recipient_id: str,
    quote_id: str,
    run_id: str,
    outcome: str,
    idempotency_key: str,
) -> NotificationContext:
    """只接受服务器步骤固定结果和原幂等键，不接自由文案或地址。"""
    if (
        any(
            type(value) is not str or re.fullmatch(rf"{prefix}_{_ULID}", value) is None
            for prefix, value in (("tn", tenant_id), ("quo", quote_id), ("run", run_id))
        )
        or not valid_quote_result_recipient(recipient_id)
        or type(outcome) is not str
        or outcome not in OUTCOMES
        or idempotency_key != f"quote-approval-notify:{run_id}:{outcome}"
    ):
        raise ValidationError("报价结果通知无效")
    return NotificationContext(
        NotificationKind.QUOTE_APPROVAL_RESULT, quote_id, run_id, outcome, None
    )


def quote_result_fingerprint(idempotency_key: str) -> str:
    """稳定命名空间指纹，不包含当前时间或随机job ID。"""
    return hashlib.sha256(
        f"quote-approval-result:{idempotency_key}".encode()
    ).hexdigest()
