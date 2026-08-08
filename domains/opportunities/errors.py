"""贸易机会域特有错误。"""

from __future__ import annotations

from shared.errors import PolicyViolation, ValidationError


class MissingLossReasonError(ValidationError):
    """终结机会时没给原因。

    守着反馈闭环：没有归因的失败机会对改进没有任何贡献。
    不要提供默认值（比如 ``UNKNOWN``）——默认值会被大量使用，
    然后归因统计就变成一堆 unknown，等于没做。
    """


class IncompleteHandoffPacketError(ValidationError):
    """接管包缺关键字段。

    至少要有 ``why_valuable``、``customer_verbatim``、``account_name``。

    为什么拦这个：不完整的接管包会被员工忽略，被忽略的接管会导致
    客户失联，而前面找客户、发信、验证需求的全部投入都归零。
    在创建时拦住，比事后问「为什么没人跟」有效。
    """


class SelfApprovalNotAllowedError(PolicyViolation):
    """员工不能审批自己负责机会的低利润报价。

    这条约束由 ``domains/approvals`` 强制，本域提供归属信息供判断。
    """


class HandoffAlreadyAcceptedError(PolicyViolation):
    """接管已被他人接受。

    并发场景：两个员工同时点「接受」。要拦住，否则两个人会同时联系
    同一个客户，说出不一致的话。
    """
