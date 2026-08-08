"""检查管线。

顺序固定，理由见 AGENTS.md：便宜且否决率高的在前；
幂等必须在执行前——顺序错了会重复发送。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from shared.schemas.identifiers import IdempotencyKey, RunId, TenantId, UserId


@dataclass(frozen=True)
class ToolCallContext:
    """一次工具调用的上下文。

    字段：
        tenant_id, user_id
        run_id:           发起的 Agent Run（人工操作为 None）
        tool_id
        params
        idempotency_key:  manifest 要求 REQUIRED 时必填
        approval_ref:     已批准的审批引用（approval stage 校验）
        campaign_ref:     所属 Campaign（边界内发送的依据）
    """

    tenant_id: TenantId
    user_id: UserId
    tool_id: str
    params: dict[str, Any]
    run_id: RunId | None = None
    idempotency_key: IdempotencyKey | None = None
    approval_ref: str | None = None
    campaign_ref: str | None = None


@dataclass(frozen=True)
class CheckRejection:
    """结构化拒绝 —— **不是异常**。

    Agent 要靠它决定下一步：``remediation`` 说明能否补救以及怎么补
    （"联系方式未验证 → 先调 contact.verify"）。裸异常会被当成
    临时故障重试，那正是最不该发生的。

    字段：
        stage:        哪个 stage 拒的
        rule:         哪条规则
        reason:       人类可读原因
        remediation:  怎么补救（None = 不可补救，别再试）
    """

    stage: str
    rule: str
    reason: str
    remediation: str | None = None


@dataclass(frozen=True)
class ToolCallResult:
    """调用结果。

    ``rejected`` 与 ``output`` 互斥。``duplicate_of`` 非 None 表示
    幂等命中，``output`` 是上次的结果——这是正常路径，不是错误。
    """

    tool_id: str
    output: dict[str, Any] | None = None
    rejected: CheckRejection | None = None
    duplicate_of: str | None = None
    tool_call_id: str | None = None
    cost_note: str | None = None


@runtime_checkable
class CheckStage(Protocol):
    """检查 stage 接口。每个 stage 一个实现，见 ``checks/``。

    返回 None = 通过；返回 ``CheckRejection`` = 拒绝并终止管线。
    stage 内部**只读**——检查不产生副作用（幂等 stage 的占位写入
    是唯一例外，它必须原子）。
    """

    name: str

    async def check(self, ctx: ToolCallContext) -> CheckRejection | None: ...


STAGE_ORDER: tuple[str, ...] = (
    "tenant",
    "permission",
    "playbook",
    "country_policy",
    "suppression",
    "approval",
    "idempotency",
    "rate_limit",
)
"""Stage 顺序。**不可随意调换**：

- tenant/permission 是内存判断，最便宜，放最前
- suppression/idempotency 要查库，居中
- approval 可能等人工，放执行前最后一段
- idempotency 在 rate_limit 之前：幂等命中直接返回旧结果，
  不该消耗限额
"""


class ToolGateway:
    """网关入口。"""

    async def invoke(self, ctx: ToolCallContext) -> ToolCallResult:
        """执行一次工具调用。

        实现要求：
        1. 从注册表取 manifest + handler（未注册 → 拒绝）
        2. 按 ``manifest.checks`` 的交集依 ``STAGE_ORDER`` 逐个跑，
           任一拒绝立即返回（仍要写审计）
        3. 幂等命中 → 返回旧结果，不执行、不重复计费
        4. 执行 handler，捕获错误分类（可重试 / 不可重试）
        5. **写 tool_call 审计（含被拒的调用）**；审计写入失败必须
           让整个调用失败——审计不完整时继续发客户邮件是合规裸奔
        6. 记录成本（Phase 1 只记录，Phase 3 接结算）
        """
        raise NotImplementedError
