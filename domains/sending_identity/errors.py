"""发件身份域的安全错误分类。

错误文本不得含地址、域名、引用、DNS 说明或调查记录；这些字段都可能进入
广泛可读的日志。调用方可凭异常类型选择安全的用户提示与处理路径。
"""

from shared.errors import PolicyViolation, ValidationError


class InvalidSendingDomainError(ValidationError):
    """发件域名不符合受限的域名形态。"""


class InvalidSendingAddressError(ValidationError):
    """发件地址不符合 Phase 1 的 ASCII 单域邮箱规则。"""


class InvalidConnectorReferenceError(ValidationError):
    """引用看起来像凭证、传输地址或不安全的自由文本。"""


class InvalidAuthenticationResultError(ValidationError):
    """认证结果没有完整且一致地表达 SPF/DKIM/DMARC 状态。"""


class InvalidDeliveryEventError(ValidationError):
    """投递事件不具备可安全、可幂等记录的 typed 元数据。"""


class DomainRoleConflictError(ValidationError):
    """同一租户的域名不能登记为多个角色。"""


class SendingIdentityNotFoundError(PolicyViolation):
    """指定身份不存在或不属于当前租户。"""


class ColdOutreachDomainViolation(PolicyViolation):
    """非冷开发域名绝不允许用于冷开发。"""


class AuthenticationNotVerifiedError(PolicyViolation):
    """SPF、DKIM、DMARC 未全部通过，不能进入预热或发送。"""


class IdentitySuspendedError(PolicyViolation):
    """身份处于停用状态，须走受限恢复流程。"""


class IdentityRetiredError(PolicyViolation):
    """退休身份不可恢复或继续使用。"""


class WarmupLimitExceededError(PolicyViolation):
    """已达到确定性预热或日发送额度。"""
