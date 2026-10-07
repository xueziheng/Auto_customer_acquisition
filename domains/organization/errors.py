"""组织域特有错误。"""

from __future__ import annotations

from shared.errors import IdempotencyConflict, PolicyViolation


class PlaybookNotConfiguredError(PolicyViolation):
    """Playbook 未配置。

    排除品类和金额底线是老板的商业决策，代码不提供默认值。
    探索、打分、Campaign 在 Playbook 配置前一律拒绝启动。
    """


class PlaybookIdempotencyConflictError(IdempotencyConflict):
    """同一幂等键被用于不同 Playbook 业务内容。"""


class PlaybookApprovalFactInvalidError(PolicyViolation):
    """审批类型、版本、内容哈希、变更集或时间与候选不精确匹配。"""


class PlaybookActivationConflictError(PolicyViolation):
    """同一审批或版本的激活重放绑定了不同对应事实。"""


class PlaybookBaseVersionConflictError(PolicyViolation):
    """候选提交时捕获的 base 已不是当前生效版本。"""
