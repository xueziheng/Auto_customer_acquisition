"""发件身份域特有错误。"""

from __future__ import annotations

from shared.errors import PolicyViolation, ValidationError


class ColdOutreachDomainViolation(PolicyViolation):
    """试图用非冷开发域名发冷邮件。

    **本域最重要的错误，整个系统里最不能放过的一条检查。**

    要防的事故：主域名因退信率或投诉率被标记后，倒下的不只是开发信，
    还有报价单、合同、样品通知、和现有客户的全部往来。客户不会告诉你
    「你的邮件我没收到」，他们只会觉得你不回消息。恢复要数周到数月。

    不提供任何绕过参数。哪怕「只发一次」也不行——一次群发就足够
    触发投诉阈值。
    """


class AuthenticationNotVerifiedError(PolicyViolation):
    """SPF / DKIM / DMARC 未全部通过就试图发送或预热。

    这是硬门槛，不是警告。认证不全的域名发冷邮件，投诉率会立刻偏高，
    等于主动损害自己的域名。

    错误消息要带上哪几项没过和具体原因，让人能照着修 DNS。
    """


class DomainRoleConflictError(ValidationError):
    """同一域名被登记为不同角色。

    在登记时就拦住，不能等到发送时——发送时才发现意味着 Campaign
    已经配好了，用户会倾向于「先跑起来再说」。
    """


class IdentitySuspendedError(PolicyViolation):
    """身份已被停用，不能发送。

    恢复需要人工排查并留记录，见 ``resume_from_suspension``。
    """


class WarmupLimitExceededError(PolicyViolation):
    """超出当日预热额度。

    这不是故障，是设计行为。错误消息要说清今天上限多少、已发多少、
    预热第几天、什么时候能放开——否则运营会以为系统坏了。
    """


class WarmupSkipNotAllowedError(PolicyViolation):
    """试图跳过预热。

    跳过预热是新域名最常见的死法：邮件服务商看到从未发信的域名突然
    日发上百封，直接判为垃圾源。
    """
