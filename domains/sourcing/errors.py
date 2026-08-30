"""寻源域特有错误。"""

from __future__ import annotations

from shared.errors import PolicyViolation, ValidationError


class MissingEvidenceSnapshotError(ValidationError):
    """候选缺少证据快照。

    没有 URL + 哈希 + 快照的候选，页面改版后无法证明报价时看到的
    是什么——对内无法复盘，对供应商无法对质。直接拒绝提交。
    """


class CandidateLimitExceededError(PolicyViolation):
    """合格候选已达上限（3 个）。

    第四个"看起来也不错"的候选是拖延症，不是尽职调查。
    要新增先显式淘汰一个。
    """


class LadderSkipError(PolicyViolation):
    """未记录前五级检查就进入公开寻源。

    「直接上 1688 找」跳过了成本更低、确定性更高的自有供应梯级。
    """


class NoQualifiedCandidateError(PolicyViolation):
    """没有合格候选却试图完成案例。

    该走 ``fail_case``，让 NO_SUPPLY_FOUND 信号如实回流——
    把不合格候选硬算成完成，反馈闭环就被污染了。
    """


class SourcingThresholdNotMetError(PolicyViolation):
    """已验证需求完整度不足 3，不能启动 V2 寻源。"""


class SourcingPlanStaleError(PolicyViolation):
    """计划版本、状态或精确哈希已过期，必须重新确认。"""


class SourcingReviewStaleError(PolicyViolation):
    """审核绑定的 Case 版本已经变化，禁止覆盖新事实。"""


class SourcingHandoffInvariantError(ValidationError):
    """成本交接快照存在数量档、计价维度或来源路径歧义。"""
