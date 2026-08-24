"""合规域稳定错误类型。"""

from __future__ import annotations

from shared.errors import IdempotencyConflict, PolicyViolation


class CountryPolicyNotConfiguredError(PolicyViolation):
    """精确国家键没有已激活政策；系统不生成法律默认值。"""


class CountryPolicyIdempotencyConflictError(IdempotencyConflict):
    """同一幂等键对应了不同国家政策候选内容。"""


class CountryPolicyApprovalFactInvalidError(PolicyViolation):
    """窄审批事实与精确候选版本不一致。"""


class CountryPolicyActivationConflictError(PolicyViolation):
    """审批或版本重放绑定了不同激活事实。"""


class CountryPolicyBaseVersionConflictError(PolicyViolation):
    """候选基准已不是该国家当前激活版本。"""


__all__ = (
    "CountryPolicyActivationConflictError",
    "CountryPolicyApprovalFactInvalidError",
    "CountryPolicyBaseVersionConflictError",
    "CountryPolicyIdempotencyConflictError",
    "CountryPolicyNotConfiguredError",
)
