"""审批域特有错误。

``ApprovalRequired`` 定义在 ``shared.errors``（tool_gateway 等
多处使用）。``SelfApprovalNotAllowedError`` 在
``domains/opportunities/errors.py`` 有同名概念——本域是执行方，
用这里的版本。
"""

from __future__ import annotations

from shared.errors import PolicyViolation


class SelfApprovalError(PolicyViolation):
    """提议人或业务负责人试图批准自己的审批。

    没有豁免参数。消除的是「赶指标时给自己开绿灯」的结构性诱惑，
    不是对某个人的不信任。
    """


class ApprovalExpiredError(PolicyViolation):
    """审批已过期。

    过期只能重新提交，不能补批——批准针对的是提交时的状态，
    一周后的世界已经变了（汇率、供应商价格、客户数量）。
    """


class ConflictingDecisionError(PolicyViolation):
    """对同一审批做出与已有决定不同的决定。

    重复投递同一决定是幂等放行；不同决定是冲突，必须人工看。
    """


class ContentImmutableError(PolicyViolation):
    """试图修改已提交审批包的内容字段。

    审批人批的是当时看到的内容。内容可变，审批就无意义。
    要改内容 = 撤销重提。
    """
