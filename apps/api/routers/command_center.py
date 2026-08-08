"""Agent Command Center —— 自然语言指挥入口。

端点（实现时补齐签名与权限装饰）：

POST /command/message
    用户消息 → trade_manager 路由 → 回复 + 可能的 DirectiveProposal
    权限：所有角色（Agent 权限 = 用户权限子集，context_builder 保证）

GET  /command/proposals/{proposal_id}
POST /command/proposals/{proposal_id}/confirm     仅 boss
POST /command/proposals/{proposal_id}/reject      仅 boss
    指令提案确认界面必须并排展示：原话 / 系统理解 / 预计行为变化
"""

from __future__ import annotations
