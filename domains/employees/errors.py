"""员工域特有错误。"""

from __future__ import annotations

from shared.errors import PolicyViolation


class OwnershipConflictError(PolicyViolation):
    """企业已被其他员工锁定。

    调用方应使用既有归属，而不是重试——重试也会失败，
    锁就是这么设计的。换负责人走 transfer。
    """


class NoAssignmentRuleError(PolicyViolation):
    """没有命中任何分配规则，客户进入经理待分配池。

    这不是错误路径的失败，是兜底路径的提示：调用方要通知经理
    有客户待领取。
    """
