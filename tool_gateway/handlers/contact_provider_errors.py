"""联系人 Provider connector 错误到 Gateway 固定分类的唯一映射。"""

from __future__ import annotations

from connectors.hunter.client import (
    HunterAuthRequiredError,
    HunterConnectorError,
    HunterPermanentError,
    HunterRateLimitedError,
    HunterTransientError,
    HunterUncertainError,
)
from shared.errors import ValidationError
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError


def map_contact_provider_error(error: BaseException) -> ToolGatewayError:
    if isinstance(error, HunterAuthRequiredError):
        return ToolGatewayError(ToolErrorCategory.PROVIDER_AUTH_REQUIRED)
    if isinstance(error, HunterRateLimitedError):
        return ToolGatewayError(
            ToolErrorCategory.RATE_LIMITED,
            retry_after_seconds=error.retry_after_seconds,
        )
    if isinstance(error, HunterPermanentError):
        return ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT)
    if isinstance(error, HunterTransientError):
        return ToolGatewayError(ToolErrorCategory.PROVIDER_TRANSIENT)
    if isinstance(error, HunterUncertainError):
        return ToolGatewayError(ToolErrorCategory.RECONCILIATION_REQUIRED)
    if isinstance(error, HunterConnectorError):
        return ToolGatewayError(ToolErrorCategory.PROVIDER_PERMANENT)
    raise ValidationError("联系人 Provider 错误类型无效")


__all__ = ("map_contact_provider_error",)

