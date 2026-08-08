"""CapabilityAgent 统一基类与 Change Set。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shared.schemas.identifiers import ChangeSetId, RunId, TenantId, UserId


@dataclass(frozen=True)
class AgentTask:
    """交给能力的一个任务。

    字段：
        tenant_id, run_id
        acting_user:   以谁的身份运行（模型权限 = 该用户权限的子集）
        objective:     任务目标（结构化）
        inputs:        任务输入
        skill_ids:     本次加载的技能（skill_router 选定）
    """

    tenant_id: TenantId
    run_id: RunId
    acting_user: UserId
    objective: str
    inputs: dict[str, Any] = field(default_factory=dict)
    skill_ids: tuple[str, ...] = ()


@dataclass
class ChangeSet:
    """业务变更集 —— Agent 工作的输出形式。

    **Agent 不直接改库。** 变更集先经 Guardrails，低风险自动应用，
    高风险走审批。这层间接的价值：可以先给人看「打算做什么」、
    可以整批回滚、可以在应用前再验一遍。

    字段：
        change_set_id, tenant_id, run_id
        changes:      按域分组的变更条目，每条含
                      (domain, operation, payload, risk_level)
        summary:      给人看的变更摘要
    """

    change_set_id: ChangeSetId
    tenant_id: TenantId
    run_id: RunId
    changes: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""


class CapabilityAgent:
    """能力基类。九个能力都继承它。

    子类只实现 ``run``：读上下文 → 调模型（经统一封装）→
    产出 ChangeSet。子类**不做**的事：
    - 不直接调 domains 的 repository
    - 不直接调外部 SDK（走 tool_gateway）
    - 不自己拼上下文（用 context_builder 给的）
    - 不绕过 guardrails 落库

    构造参数：
        model:          模型标识。**是参数不是路由**——只有一家
                        供应商时建 model_router 是空转
        model_client:   统一模型调用封装（计量、超时、重试在这里）
        gateway:        tool_gateway 入口
        guardrails:     输出护栏
    """

    name: str = "capability"

    def __init__(
        self,
        model: str,
        model_client: Any,
        gateway: Any,
        guardrails: Any,
    ) -> None:
        raise NotImplementedError

    async def run(self, task: AgentTask, context: Any) -> ChangeSet:
        """执行任务。子类实现。

        实现约定：
        - 每次模型调用都记入 Run（模型、token、耗时、目的）
        - 产出的每条变更标 risk_level，供应用阶段分流
        - 失败要区分可重试与不可重试，不可重试的返回带解释的
          空 ChangeSet 而不是抛裸异常
        """
        raise NotImplementedError
