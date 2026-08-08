"""会话域的事件契约声明。"""

from __future__ import annotations

from shared.events.catalog import ReplyReceived

PUBLISHES = (ReplyReceived,)
"""``ReplyReceived`` 在分类落库后发布（AUTO_REPLY 除外——自动回复
不算回复，不停序列也不进统计）。

订阅方：``domains/outreach``（停序列）、``domains/demand``（提取需求）。
"""

SUBSCRIBES = ()
