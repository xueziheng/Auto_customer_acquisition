"""发件身份域的事件契约声明。"""

from __future__ import annotations

from shared.events.catalog import (
    ComplaintReceived,
    MessageBounced,
    MessageDelivered,
    ReputationThresholdBreached,
    SendingIdentityActivated,
    SendingIdentitySuspended,
    SendingIdentityThrottled,
    UnsubscribeReceived,
)

PUBLISHES = (
    SendingIdentityActivated,
    SendingIdentityThrottled,
    SendingIdentitySuspended,
    ReputationThresholdBreached,
)
"""本域发布的事件。

``SendingIdentityThrottled`` / ``SendingIdentitySuspended`` 是**熔断的
广播**：``domains/outreach`` 订阅后立刻停用该身份下的所有序列发送，
``notification_gateway`` 同时告警。

熔断必须先落状态再发事件，不能反过来——事件投递失败时状态也要已经
生效，否则告警发出去了但发送还在继续。
"""

SUBSCRIBES = (
    MessageDelivered,
    MessageBounced,
    ComplaintReceived,
    UnsubscribeReceived,
)
"""本域订阅的事件。

四个投递事件都用于更新滚动窗口计数，并触发熔断评估。

处理器要求：
- **必须幂等**。邮件服务商 webhook 经常重发，重复计数会导致误熔断。
  用 ``dedup_key`` 去重。
- ``MessageBounced`` 要区分硬退信和软退信：硬退信计入信誉惩罚并
  应触发联系人抑制，软退信（临时性，如收件箱满）不应该。
  把两者混为一谈会让正常的临时退信拖垮身份状态。
"""
