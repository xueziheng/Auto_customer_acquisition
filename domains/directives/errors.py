"""老板指令域特有错误。"""

from __future__ import annotations

from shared.errors import PolicyViolation, ValidationError


class ProposalNotConfirmableError(PolicyViolation):
    """提案不在可确认状态（已决定或已过期）。

    过期提案不能确认：一周前的解析基于一周前的系统状态，
    「暂停 3 个 Campaign」的预计影响现在可能是 7 个。
    要重新提交让老板看新的影响。
    """


class MissingBehaviorChangesError(ValidationError):
    """提案缺少预计行为变化。

    没有它，老板确认的只是一堆字段名，发现不了误解析——
    而误解析的指令会静默改变全系统行为。解析器必须补算影响。
    """


class InvalidDiscoveryRatioError(ValidationError):
    """探索配比两项之和不是 100。"""


class NotAuthorizedToDirectError(PolicyViolation):
    """非 boss 角色试图确认指令。

    在服务层拦（不只 API 层）：Worker 和工作流也会走到这条路径。
    """
