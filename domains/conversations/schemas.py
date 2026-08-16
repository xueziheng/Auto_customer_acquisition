"""会话域对外 DTO。

回复分类枚举是本域最重要的公共资产（见本目录 AGENTS.md）：跨层（agent_runtime/
workflows）只能经 schemas 使用分类契约；models 是内部实现。动作映射
``REPLY_ACTIONS`` 仍是域内部数据（确定性动作由域/工作流消费，不随 DTO 外泄）。
"""

from __future__ import annotations

from domains.conversations.models import ReplyCategory

__all__ = ("ReplyCategory",)
