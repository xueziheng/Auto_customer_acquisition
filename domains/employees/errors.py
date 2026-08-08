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


class NoOwnershipLockError(PolicyViolation):
    """尝试转移一个没有归属锁的企业。

    必须先 ``resolve_owner`` 上锁，再 ``transfer``——没有 from_owner
    就无从记录「谁交接到谁」。
    """


class EmployeeNotFoundError(PolicyViolation):
    """员工不存在或不属于该租户。"""
