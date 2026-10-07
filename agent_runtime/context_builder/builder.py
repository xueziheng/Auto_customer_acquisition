"""Context Builder —— 按用户、角色、任务裁剪上下文。

「模型能处理所有数据」不等于把整个数据库塞给它。每次运行只装载
当前任务需要的数据，并显式声明工具白名单与黑名单。

权限约束见 ``docs/architecture/03-permissions.md``：
**模型的权限是发起用户权限的子集，永远不能超出。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from shared.schemas.identifiers import TenantId, UserId


@dataclass(frozen=True)
class BuiltContext:
    """装配好的上下文。

    字段：
        sections:       按优先级排列的内容段
        allowed_tools:  工具白名单
        blocked_tools:  工具黑名单与审计证据；Gateway 仍独立判权
        data_scope:     最小安全审计投影，不是权限凭证；
                        不得反序列化为域 actor 或 Gateway 授权
        token_estimate: 模型可见 JSON 的 UTF-8 字节预算单位，非真实用量
        truncation_log: 截断了什么。**悄悄丢数据比拒绝执行更危险**，
                        截断必须留痕并进 Run 记录
    """

    sections: list[dict[str, Any]]
    allowed_tools: tuple[str, ...]
    blocked_tools: tuple[str, ...]
    data_scope: dict[str, Any]
    token_estimate: int
    truncation_log: list[str] = field(default_factory=list)


LOADING_PRIORITY: tuple[str, ...] = (
    "task_objective_and_directive",
    "playbook_rules",
    "current_entity_summary",
    "relevant_history_evidence",
    "matching_supply_capability",
)
"""任务与完整规则必需；仅可选背景从尾部整段移除，不能拆开来源。"""


@runtime_checkable
class ContextBuilder(Protocol):
    """上下文构建器。"""

    async def build(
        self,
        tenant_id: TenantId,
        acting_user: UserId,
        task_objective: str,
        entity_refs: dict[str, str],
        skill_tool_requirements: tuple[str, ...],
        token_budget: int,
    ) -> BuiltContext:
        """装配上下文。

        实现要求：
        1. 取用户的角色与 ABAC 范围，据此过滤一切数据装载——
           销售会话里的 Agent 看不到其他团队的客户，和用户本人一致
        2. ``allowed_tools`` = 技能声明 ∩ 用户允许 ∩ 运行允许 ∩ 已注册工具，扣除显式禁止。
           **交集，不是并集。**
        3. 语义检索召回历史证据时限条数，且只作背景不作结论
        4. 必需内容超预算拒绝；背景按 ``LOADING_PRIORITY`` 从尾部整段移除，
           截断项写入 ``truncation_log``
        5. 不同租户的数据绝不进同一上下文（硬边界 8 的运行时形式）
        """
        ...
