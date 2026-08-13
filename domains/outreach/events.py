"""触达域的事件契约声明。"""

from __future__ import annotations

from shared.events.catalog import (
    ComplaintReceived,
    MessageBounced,
    MessageSent,
    ReplyReceived,
    SendingIdentitySuspended,
    SendingIdentityThrottled,
    SuppressionAdded,
    UnsubscribeReceived,
)

PUBLISHES = (MessageSent, SuppressionAdded)

SUBSCRIBES = (
    ReplyReceived,
    MessageBounced,
    ComplaintReceived,
    UnsubscribeReceived,
    SendingIdentityThrottled,
    SendingIdentitySuspended,
)
"""本域订阅的事件与处理逻辑：

``ReplyReceived``      → ``stop_enrollment(REPLY)``（幂等；创建发送尝试前
                         仍须通过 ReplyStatusProvider 现查回复事实）
``MessageBounced``     → 硬退信：抑制联系人并停序列；
                         软退信：只记录 receipt，不永久抑制、不自动重试
``ComplaintReceived``  → 立即抑制联系人（投诉是最强的"别烦我"信号）
``UnsubscribeReceived``→ 抑制；请求语义覆盖公司时用 ACCOUNT 级
``SendingIdentityThrottled`` / ``SendingIdentitySuspended``
                       → 挂起该身份名下所有序列
                         （``stop_all_for_identity``）

所有处理器必须经公共 ``OutreachService``，并以事件指纹保证幂等。
"""
