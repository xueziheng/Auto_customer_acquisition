"""会话域的事件契约声明。"""

from __future__ import annotations

from shared.events.catalog import ReplyReceived

PUBLISHES = (ReplyReceived,)
"""``ReplyReceived`` 在分类落库后发布（AUTO_REPLY 除外——自动回复
不算回复，不停序列也不进统计）。

事件携带 ``outbound_message_id``（被回复出站消息的 RFC Message-ID，与入站
``message_id`` 不同命名空间）；无出站关联时为 None，订阅方必须 fail-closed。

订阅方：``domains/outreach``（停序列）、``domains/demand``（提取需求）。
"""

SUBSCRIBES = ()
