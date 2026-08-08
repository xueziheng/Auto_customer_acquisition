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

``ReplyReceived``      → ``stop_on_reply``（幂等；发送侧还有二次检查
                         关竞态，见 ``prepare_send``）
``MessageBounced``     → 硬退信：抑制联系人并停序列；
                         软退信：计数，连续 3 次按硬退信处理
``ComplaintReceived``  → 立即抑制联系人（投诉是最强的"别烦我"信号）
``UnsubscribeReceived``→ 抑制；请求语义覆盖公司时用 ACCOUNT 级
``SendingIdentityThrottled`` / ``SendingIdentitySuspended``
                       → 挂起该身份名下所有序列
                         （``stop_all_for_identity``）

所有处理器必须幂等。
"""
