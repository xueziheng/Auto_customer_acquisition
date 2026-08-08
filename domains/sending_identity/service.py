"""发件身份域服务 —— **本域的公共 API**。

本域不发送邮件，只回答三个问题：能不能发、能发多少、现在该不该停。
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from domains.sending_identity.models import (
    AuthStatus,
    DomainReputation,
    ReputationWindow,
)
from domains.sending_identity.schemas import (
    IdentityRegisterRequest,
    IdentityView,
    SendPermission,
)
from shared.schemas.identifiers import SendingIdentityId, TenantId


@runtime_checkable
class SendingIdentityService(Protocol):
    """发件身份服务。"""

    async def register(
        self, tenant_id: TenantId, request: IdentityRegisterRequest
    ) -> SendingIdentityId:
        """登记新发件身份。

        实现要求：
        - 校验同一域名下的角色一致。**一个域名不能既是
          ``COLD_OUTREACH`` 又是 ``PRIMARY_BUSINESS``** ——
          冲突时抛 ``DomainRoleConflictError``。这是本域的核心约束，
          在登记时就要拦住，不能等到发送时。
        - 初始状态 ``CREATED``，不允许直接指定 ``ACTIVE``
        """
        ...

    async def verify_authentication(
        self, tenant_id: TenantId, identity_id: SendingIdentityId
    ) -> AuthStatus:
        """校验 SPF / DKIM / DMARC。

        实际 DNS 查询在 ``connectors/``，本域接收结果并落状态。

        实现要求：
        - 三项全过才进 ``WARMING``，部分通过仍留在 ``AUTH_PENDING``
        - 未通过时把 ``details`` 存下来，要具体到能照着修
        """
        ...

    async def start_warmup(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        target_daily_volume: int,
        started_on: date,
    ) -> None:
        """启动预热。

        实现要求：
        - 认证未通过时抛 ``AuthenticationNotVerifiedError``
        - 生成预热曲线：起点 ≤10 封/天，连续爬升
        - **不提供 ``skip_warmup`` 参数。** 有这个参数它就一定会被用，
          然后新域名会在第一周死掉。
        """
        ...

    async def check_send_permission(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        on_day: date,
        *,
        for_cold_outreach: bool,
    ) -> SendPermission:
        """**发送前必查。** 返回能否发送、今日剩余额度、原因。

        `tool_gateway` 在执行任何发送动作前调用这个方法。

        实现要求：
        - 状态不允许发送 → 拒绝，说明当前状态
        - ``for_cold_outreach`` 为真但角色不是 ``COLD_OUTREACH``
          → 拒绝，抛 ``ColdOutreachDomainViolation``。这是整个系统里
          最不能放过的一条检查。
        - 计算今日剩余：``warmup_plan.daily_limit_on(on_day)`` 减去
          今日已发数
        - 拒绝原因必须可读，会展示给老板和员工看
        """
        ...

    async def record_delivery_event(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        event_type: str,
        occurred_at: str,
    ) -> None:
        """记录投递事件（送达/退信/投诉/退订）。

        实现要求：
        - 更新滚动窗口计数
        - **随即评估熔断**（见 ``evaluate_reputation``）
        - 幂等：同一事件重复投递不重复计数。邮件服务商的
          webhook 经常重发，重复计数会导致误熔断。
        """
        ...

    async def evaluate_reputation(
        self, tenant_id: TenantId, identity_id: SendingIdentityId
    ) -> ReputationWindow:
        """评估信誉并**自动执行熔断**。

        实现要求：
        - 样本不足 ``minimum_sample`` 时不判定，直接返回
        - 超 throttle 阈值 → 自动转 ``THROTTLED``
        - 超 suspend 阈值 → 自动转 ``SUSPENDED``
        - 垃圾陷阱命中或黑名单出现 → 直接 ``SUSPENDED``
        - 发布 ``ReputationThresholdBreached``

        **不等人工审批。** 等人看到告警再处理，期间可能又发出去几千封，
        每一封都在加深损害。这是系统里少数「宁可误伤也要自动执行」
        的地方：误停一天的成本远小于域名被标记。
        """
        ...

    async def resume_from_throttle(
        self, tenant_id: TenantId, identity_id: SendingIdentityId
    ) -> None:
        """限流恢复。指标回到阈值内时可自动调用。"""
        ...

    async def resume_from_suspension(
        self,
        tenant_id: TenantId,
        identity_id: SendingIdentityId,
        approved_by: str,
        investigation_note: str,
    ) -> None:
        """停用恢复。**必须人工**，且要留排查记录。

        为什么不能自动：能自动恢复的停用起不到作用——问题根源
        （通常是联系人数据质量）没解决，恢复后立刻复发，
        而每一轮复发都在加深域名损害。
        """
        ...

    async def retire(
        self, tenant_id: TenantId, identity_id: SendingIdentityId, reason: str
    ) -> None:
        """退役身份，不可逆。

        冷开发域名是消耗品，退役是正常运营动作，不是事故。
        """
        ...

    # --- 查询 -----------------------------------------------------------

    async def get(
        self, tenant_id: TenantId, identity_id: SendingIdentityId
    ) -> IdentityView: ...

    async def list_available_for_campaign(
        self, tenant_id: TenantId
    ) -> list[IdentityView]:
        """可用于 Campaign 的身份。

        实现要求：只返回角色为 ``COLD_OUTREACH``、认证已通过、
        状态允许发送的身份。Campaign 创建界面只能从这个列表里选——
        这样「误选主业务邮箱」在界面层就不可能发生。
        """
        ...

    async def get_domain_reputation(
        self, tenant_id: TenantId, domain: str
    ) -> DomainReputation:
        """域名级聚合信誉。

        单个身份没超标但同域多个身份都在边缘时，域名整体已有风险。
        这个查询就是为了发现那种情况。
        """
        ...
