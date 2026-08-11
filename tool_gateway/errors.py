"""Tool Gateway 与 Connector 的固定安全错误分类。"""

from __future__ import annotations

from enum import Enum

from shared.errors import TradeOSError, ValidationError


class ToolErrorCategory(str, Enum):
    VALIDATION = "validation"
    PERMISSION_DENIED = "permission_denied"
    SUPPRESSED = "suppressed"
    APPROVAL_REQUIRED = "approval_required"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    IN_PROGRESS = "in_progress"
    RATE_LIMITED = "rate_limited"
    PROVIDER_AUTH_REQUIRED = "provider_auth_required"
    PROVIDER_PERMANENT = "provider_permanent"
    PROVIDER_TRANSIENT = "provider_transient"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    UNEXPECTED = "unexpected"


class ToolCallStatus(str, Enum):
    RECEIVED = "received"
    CLAIMED = "claimed"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    FAILED_TRANSIENT = "failed_transient"
    FAILED_PERMANENT = "failed_permanent"


class DeliveryCertainty(str, Enum):
    SENT = "sent"
    DEFINITELY_NOT_SENT = "definitely_not_sent"
    UNKNOWN = "unknown"


_RETRYABLE_CATEGORIES = frozenset(
    {
        ToolErrorCategory.IN_PROGRESS,
        ToolErrorCategory.RATE_LIMITED,
        ToolErrorCategory.PROVIDER_TRANSIENT,
        ToolErrorCategory.RECONCILIATION_REQUIRED,
    }
)


class ToolGatewayError(TradeOSError):
    """只暴露 typed 分类与有界 Retry-After 的固定错误。"""

    def __init__(
        self,
        category: ToolErrorCategory,
        *,
        retry_after_seconds: int | None = None,
    ) -> None:
        if not isinstance(category, ToolErrorCategory):
            raise ValidationError("工具错误分类无效")
        if retry_after_seconds is not None and (
            not isinstance(retry_after_seconds, int)
            or isinstance(retry_after_seconds, bool)
            or not 1 <= retry_after_seconds <= 86_400
        ):
            raise ValidationError("工具重试时间无效")
        if retry_after_seconds is not None and category not in _RETRYABLE_CATEGORIES:
            raise ValidationError("不可重试错误不能携带重试时间")
        super().__init__("工具调用失败")
        self.category = category
        self.retry_after_seconds = retry_after_seconds
        self.is_retryable = category in _RETRYABLE_CATEGORIES
