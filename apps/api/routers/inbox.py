"""Smart Inbox —— 统一处理客户回复。

GET  /inbox/conversations        会话列表（按分类/负责人过滤）
GET  /inbox/conversations/{id}   线程：消息 + 分类 + 提取的需求字段
POST /inbox/messages/{id}/correct-classification   人工纠正（原判保留）
POST /inbox/conversations/{id}/draft-reply         请求追问草稿
                                 （qualification_agent，最多两个主题）
"""

from __future__ import annotations
