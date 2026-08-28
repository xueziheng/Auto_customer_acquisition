"""贸易机会域服务 —— **本域的公共 API**。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from domains.opportunities.models import LossReason, OpportunityState
from domains.opportunities.permissions import Actor, OpportunityScope
from domains.opportunities.permissions import (
    OpportunityAction as OpportunityAction,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.opportunities.permissions import (
    OpportunityAuthorizer as OpportunityAuthorizer,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.opportunities.permissions import (
    ScopeLevel as ScopeLevel,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.opportunities.schemas import (
    HandoffCreateRequest,
    HandoffPacketView,
    HandoffQueueItemView,
    HandoffQueueStats,
    OpportunityCreateRequest,
    OpportunityView,
    ValidatedNeedEvidence,
)
from shared.schemas.identifiers import (
    EmployeeId,
    HandoffId,
    OpportunityId,
    TenantId,
    ValidatedNeedId,
)


@runtime_checkable
class OpportunityService(Protocol):
    """机会域服务。

    所有公开读写方法都必须带显式 ``actor``（``permissions.Actor``）并经
    注入的 authorizer 判权；拒绝抛 ``PermissionDenied``（fail closed）。
    """

    async def create_from_need(
        self,
        tenant_id: TenantId,
        request: OpportunityCreateRequest,
        evidence: ValidatedNeedEvidence,
        *,
        actor: Actor,
    ) -> OpportunityId | None:
        """从已验证需求创建机会。

        ``evidence`` 必须是客户本人明确表达的证据（等级 ≥
        ``CUSTOMER_INTEREST_REPLY`` 且来源为 conversation/upload/employee_input，
        ``EMPLOYEE_INPUT`` 必须带人工确认对）。不满足即抛 ``ValidationError``
        ——「已验证需求」是门槛不是标签，公开事件/员工猜测/Agent 推断不能
        仅凭标签进入。

        返回 None 表示**未通过硬门槛**，没有创建——这是正常结果，
        不是错误。调用方（通常是 ``NeedValidated`` 的处理器）应记录
        被拦原因，不要抛异常。

        实现要求：
        - 先经 authorizer 判权，再校验 evidence（任何仓储读取/副作用之前）
        - 调 ``scoring.check_gates``，不通过则存快照后返回 None
        - 通过则创建机会并发布 ``OpportunityQualified``
        - 同一 ``need_id`` 已有机会时返回既有 ID（幂等）
        """
        ...

    async def assign(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        owner: EmployeeId,
        assigned_by: EmployeeId,
        *,
        actor: Actor,
    ) -> None:
        """分配负责人（``assigned_by`` 人工审计主体**必填**；``assigned_at`` 由实现用注入时钟生成）。

        ``actor`` 是授权身份，``assigned_by`` 是业务审计主体——两者分离，
        禁止一个参数兼任两种身份。

        分配优先级由 ``domains/employees`` 的 Territory Matrix 决定，
        **本域不实现分配算法**——它需要国家、品类、语言、工作量等
        信息，属于那个域。这里只接收结果。
        """
        ...

    async def transition(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        target: OpportunityState,
        *,
        actor: Actor,
    ) -> None:
        """状态转换。非法转换抛 ``InvalidStateTransition``。

        不要提供「强制设置状态」的方法。状态机的价值在于它真的挡住
        非法路径；有后门等于没有状态机。
        """
        ...

    async def mark_lost(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        reason: LossReason | None,
        *,
        actor: Actor,
        confirmed_by: EmployeeId,
        confirmed_at: datetime,
        detail: str | None = None,
    ) -> None:
        """终结机会（**人工确认动作**）。``reason`` 为 None 抛 ``MissingLossReasonError``。

        ``actor`` 是授权身份；``confirmed_by``/``confirmed_at`` 是业务确认主体，
        两者分离——禁止一个参数兼任两种身份。

        实现要求：
        - 记录 ``died_at_state``（转入 lost 之前的状态）、``closed_by``/``closed_at``
        - 写 ``loss_records``（confirmed_by/confirmed_at 必填，与 ``confirmed_by``/``confirmed_at`` 一致）
        - 发布 ``OpportunityLost``
        - ``detail`` 存自由文本补充，尤其 ``LOST_TO_COMPETITOR``
          时要尽量记下输在哪（价格/交期/规格/信任）
        """
        ...

    async def mark_won(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        *,
        actor: Actor,
        confirmed_by: EmployeeId,
        confirmed_at: datetime,
    ) -> None:
        """终结机会为成交（**人工确认动作**，仅从 ``negotiating`` 转入）。

        ``actor`` 是授权身份；``confirmed_by`` 是业务确认主体，两者分离。

        实现要求：
        - 记录 ``closed_by``/``closed_at``
        - 发布 ``OpportunityWon``（closed_by 必填，防伪造）
        """
        ...

    # --- 人工接管 -------------------------------------------------------

    async def request_handoff(
        self,
        tenant_id: TenantId,
        request: HandoffCreateRequest,
        *,
        actor: Actor,
    ) -> HandoffId:
        """请求人工接管。

        实现要求：
        - **组装完整接管包**。缺 ``customer_verbatim`` / ``why_valuable`` /
          ``account_name`` 时拒绝创建——不完整的接管包会被员工忽略，
          而被忽略的接管等于客户流失。
        - ``customer_verbatim`` 存客户原话；``customer_verbatim_provenance``
          必须指向 conversation/upload/employee_input，并随接管包保存。
        - 发布 ``HandoffRequested``（SLA 计时从此开始）
        - 同一机会已有未完成接管时返回既有 ID（幂等）
        """
        ...

    async def accept_handoff(
        self,
        tenant_id: TenantId,
        handoff_id: HandoffId,
        accepted_by: EmployeeId,
        *,
        actor: Actor,
    ) -> None:
        """员工接受接管。发布 ``HandoffAccepted``，SLA 计时结束。

        ``actor`` 是授权身份；``accepted_by`` 是业务审计主体（事件中的
        ``accepted_by``），两者分离。
        """
        ...

    async def record_handoff_escalation(
        self,
        tenant_id: TenantId,
        handoff_id: HandoffId,
        level: int,
        escalated_at: datetime,
        *,
        actor: Actor,
    ) -> None:
        """只增记录一次接管升级；所有 actor（含 SYSTEM）均先走 typed 判权。"""
        ...

    async def get_handoff_packet(
        self, tenant_id: TenantId, handoff_id: HandoffId, *, actor: Actor
    ) -> HandoffPacketView:
        """读取接管包。"""
        ...

    async def get_queue_stats(
        self, tenant_id: TenantId, *, actor: Actor
    ) -> HandoffQueueStats:
        """待接管队列统计。

        实现要求：
        - 队列深度、最久等待时长、按员工分组的积压
        - 超阈值时发布 ``HandoffQueueBacklogged``

        Phase 1 只发通知。Phase 2 反压挂载点：接积分后据此自动降低
        探索类任务预算——队列积压 40 个高意向机会时继续花钱找新客户
        是在毁灭价值。
        """
        ...

    # --- 查询 -----------------------------------------------------------

    async def get(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        *,
        actor: Actor,
    ) -> OpportunityView:
        """读取机会。View 要带打分快照摘要——老板点「为什么是高意向」
        时要能展开看门槛和因子。"""
        ...

    async def get_by_need(
        self,
        tenant_id: TenantId,
        need_id: ValidatedNeedId,
        *,
        actor: Actor,
    ) -> OpportunityView | None:
        """按已验证需求精确读取机会；不存在返回 None，不按企业猜测。"""
        ...

    async def list_for_employee(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        *,
        actor: Actor,
        states: list[OpportunityState] | None = None,
        limit: int = 50,
    ) -> list[OpportunityView]:
        """某员工负责的机会。按 ABAC 范围过滤。"""
        ...

    async def list_opportunities(
        self,
        tenant_id: TenantId,
        actor: Actor,
        *,
        scope: OpportunityScope,
        states: list[OpportunityState] | None = None,
        limit: int = 50,
    ) -> list[OpportunityView]:
        """按调用身份的完整 ABAC scope 在 SQL 层列出机会。"""
        ...

    async def list_pending_handoffs(
        self,
        tenant_id: TenantId,
        actor: Actor,
        *,
        limit: int = 50,
    ) -> list[HandoffQueueItemView]:
        """按身份 scope 列出 pending 接管；保持最久等待优先。"""
        ...

    async def loss_reason_breakdown(
        self,
        tenant_id: TenantId,
        *,
        actor: Actor,
        since_days: int = 30,
    ) -> dict[str, dict[str, int]]:
        """按 ``(loss_reason, died_at_state)`` 交叉统计。

        **反馈闭环的主要出口。** 返回二维计数，因为单看原因不够：
        ``PRICE_TOO_HIGH`` 集中在 contacted 阶段说明筛选门槛该收紧，
        集中在 quoted 阶段说明成本或报价能力有问题。

        ``NEED_NOT_REAL`` 占比是最该盯的数——它直接衡量假设生成
        环节的质量。
        """
        ...
