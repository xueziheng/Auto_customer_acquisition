"""Stage: 幂等。

- manifest 要求 REQUIRED 但 ctx 没带键 → 拒绝（调用方 bug）
- 键已完成 → 短路返回旧结果（由管线处理，不算拒绝）
- 键正在执行 → 拒绝并提示稍后（不排队——排队会积压重试风暴）
- 新键 → 原子占位后放行

键必须在数据库（唯一约束），不能只在 Redis——丢键等于重复发送。
这是管线里唯一允许写入的 stage（占位记录），写入必须原子。
"""

from __future__ import annotations

from tool_gateway.pipeline import CheckRejection, ToolCallContext


class IdempotencyCheck:
    name = "idempotency"

    async def check(self, ctx: ToolCallContext) -> CheckRejection | None:
        raise NotImplementedError
