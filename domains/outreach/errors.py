"""触达域固定安全错误。"""

from __future__ import annotations

from shared.errors import (
    InvalidStateTransition,
    PolicyViolation,
    TransientError,
    ValidationError,
)


class CampaignBoundaryInvalidError(ValidationError):
    """Campaign 边界不自洽。"""


class CampaignApprovalRequiredError(PolicyViolation):
    """Campaign 当前版本缺少有效审批。"""


class CampaignNotActiveError(PolicyViolation):
    """Campaign 当前状态不允许入组或准备消息。"""


class ContactNotEligibleError(PolicyViolation):
    """联系人当前资格不满足已批准边界。"""


class OutreachProviderUnavailableError(TransientError):
    """跨域安全快照暂时不可用。"""


class CampaignQuotaExceededError(PolicyViolation):
    """Campaign 当日额度已满。"""


class SendingIdentityUnavailableError(PolicyViolation):
    """没有满足 Campaign 边界的可用发件身份。"""


class AccountAlreadyEnrolledError(PolicyViolation):
    """该企业已有活跃 Enrollment。"""


class IdempotencyConflictError(ValidationError):
    """幂等键已绑定不同业务内容。"""


class MessageAttemptConflictError(PolicyViolation):
    """Message Attempt 的已记录结果与本次结果不一致。"""


class ReplyAlreadyReceivedError(PolicyViolation):
    """联系人已经回复，不能继续准备消息。"""


class SuppressedError(PolicyViolation):
    """联系人或企业已进入全局抑制。"""


class SequenceStepLimitError(PolicyViolation):
    """序列步数超过已批准边界。"""


class FirstStepMustBeDiscoveryError(ValidationError):
    """序列第一步必须是需求发现。"""


__all__ = [
    "AccountAlreadyEnrolledError",
    "CampaignApprovalRequiredError",
    "CampaignBoundaryInvalidError",
    "CampaignNotActiveError",
    "CampaignQuotaExceededError",
    "ContactNotEligibleError",
    "FirstStepMustBeDiscoveryError",
    "IdempotencyConflictError",
    "InvalidStateTransition",
    "MessageAttemptConflictError",
    "OutreachProviderUnavailableError",
    "ReplyAlreadyReceivedError",
    "SendingIdentityUnavailableError",
    "SequenceStepLimitError",
    "SuppressedError",
]
