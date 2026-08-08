"""触达域特有错误。"""

from __future__ import annotations

from shared.errors import PolicyViolation, ValidationError


class CampaignBoundaryInvalidError(ValidationError):
    """Campaign 边界不自洽。

    带上 ``CampaignBoundary.validate()`` 返回的全部问题，
    一次告诉用户所有要改的地方。
    """


class CampaignNotActiveError(PolicyViolation):
    """Campaign 不在 ACTIVE 状态，不能入组或发送。"""


class DailyQuotaExceededError(PolicyViolation):
    """超出当日额度。

    错误消息要区分是 ``new_contacts`` 还是 ``total_messages`` 超了，
    并带当前值和上限——这不是故障，是老板批准的边界在起作用，
    要让人一眼看懂。
    """


class AccountAlreadyEnrolledError(PolicyViolation):
    """该企业已有活跃序列（Ownership Lock 的触达侧）。

    两个序列同时给一家公司发信，客户会收到两套说辞，
    并把公司判断为管理混乱。
    """


class SequenceStepLimitError(PolicyViolation):
    """超出序列步数上限。老板批的是三封就是三封。"""


class FirstStepMustBeDiscoveryError(ValidationError):
    """序列第一步不是 DISCOVERY 意图。

    第一封邮件的目标是让客户说出他缺什么，不是卖货——
    这是本系统与普通群发工具的核心区别，不允许在配置层被绕掉。
    """
