"""触达域服务 —— **本域的公共 API**。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.outreach.models import SuppressionReason, SuppressionScope
from domains.outreach.schemas import (
    CampaignCreateRequest,
    CampaignView,
    EnrollmentView,
    SendAuthorization,
)
from shared.schemas.identifiers import (
    CampaignId,
    ContactPointId,
    EmployeeId,
    EnrollmentId,
    ProspectAccountId,
    TenantId,
)


@runtime_checkable
class OutreachService(Protocol):
    """触达服务。"""

    # --- Campaign 生命周期 ------------------------------------------------

    async def create_campaign(
        self, tenant_id: TenantId, request: CampaignCreateRequest
    ) -> CampaignId:
        """创建 Campaign（草稿态）。

        实现要求：
        - 调 ``CampaignBoundary.validate()``，有问题拒绝创建
        - 校验每个发件身份：角色必须是 COLD_OUTREACH 且认证通过
          （查 ``sending_identity`` 服务的结果由上层传入或经其
          公共接口获取）。**在创建时拦，不要等发送时**——发送时
          才发现意味着老板批准了一个跑不起来的东西。
        """
        ...

    async def submit_for_approval(
        self, tenant_id: TenantId, campaign_id: CampaignId
    ) -> None:
        """提交审批。审批本身走 ``domains/approvals``。"""
        ...

    async def activate(
        self, tenant_id: TenantId, campaign_id: CampaignId, approved_by: EmployeeId
    ) -> None:
        """激活。只能从 ``PENDING_APPROVAL`` 进入，且要有审批记录。"""
        ...

    async def pause(
        self, tenant_id: TenantId, campaign_id: CampaignId, reason: str
    ) -> None:
        """暂停。老板说「今天停止新发邮件」时走这里。

        暂停只停新发送，**不停止收回复**——已发出的邮件还会有人回，
        回复处理不能跟着停。
        """
        ...

    async def revise_boundary(
        self,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        request: CampaignCreateRequest,
    ) -> int:
        """修改边界 = 创建新版本并回到 ``PENDING_APPROVAL``。

        返回新版本号。**不允许原地改已批准的边界**——否则「老板批的」
        和「实际跑的」会悄悄分叉，审批失去意义。
        """
        ...

    # --- 入组与发送 -------------------------------------------------------

    async def enroll(
        self,
        tenant_id: TenantId,
        campaign_id: CampaignId,
        account_id: ProspectAccountId,
        contact_point_id: ContactPointId,
        verified: bool,
    ) -> EnrollmentId:
        """把联系人加入序列。

        实现要求（按顺序检查，任一失败拒绝入组）：
        1. Campaign 处于 ACTIVE
        2. ``verified`` 为 True（硬边界 6：未验证地址会产生硬退信，
           硬退信毁身份信誉）——``verified`` 由上层从 prospecting
           域查得后传入
        3. 联系人和企业都不在抑制名单
        4. 该企业没有其他活跃 enrollment（Ownership Lock 的触达侧：
           两个序列同时给一家公司发信，客户会收到两套说辞）
        5. 今日 ``new_contacts`` 额度未用完（原子递增）
        6. 分配发件身份（在 Campaign 的身份列表内轮询，考虑各身份
           今日剩余额度）
        """
        ...

    async def prepare_send(
        self, tenant_id: TenantId, enrollment_id: EnrollmentId
    ) -> SendAuthorization:
        """发送前综合检查，返回发送授权。

        **``stop_on_reply`` 的竞态在这里关闭**：回复可能在计划发送前
        几秒到达而事件尚未处理，所以发送前必须现查一次「该会话是否
        已有回复」，有则停止序列而不是发送。

        检查清单：
        1. enrollment 状态允许发下一步
        2. 会话无新回复（现查，不信缓存）
        3. 联系人/企业不在抑制名单（现查）
        4. Campaign 仍为 ACTIVE 且版本未变
        5. 今日 ``total_messages`` 额度未用完
        6. 发件身份今日额度未用完（调用方持 ``SendPermission`` 传入）

        返回的 ``SendAuthorization`` 带幂等键，``tool_gateway`` 执行
        发送时用它保证不重发。
        """
        ...

    async def record_sent(
        self,
        tenant_id: TenantId,
        enrollment_id: EnrollmentId,
        message_attempt_ref: str,
    ) -> None:
        """记录发送完成，推进序列步数，计算下一步时间。

        发布 ``MessageSent``。
        """
        ...

    async def stop_on_reply(
        self, tenant_id: TenantId, enrollment_id: EnrollmentId
    ) -> None:
        """回复到达，停止序列。``ReplyReceived`` 处理器调用。幂等。"""
        ...

    # --- 抑制名单 ---------------------------------------------------------

    async def suppress(
        self,
        tenant_id: TenantId,
        scope: SuppressionScope,
        target_id: str,
        reason: SuppressionReason,
        source_ref: str,
    ) -> None:
        """加入抑制名单。

        实现要求：
        - 同时停止目标的所有活跃 enrollment（跨所有 Campaign）
        - ``UNSUBSCRIBE`` 时判断范围：请求语义是「别再发给我们公司」
          则用 ACCOUNT 级
        - 发布 ``SuppressionAdded``
        - 幂等：重复抑制不报错
        """
        ...

    async def is_suppressed(
        self,
        tenant_id: TenantId,
        contact_point_id: ContactPointId | None,
        account_id: ProspectAccountId | None,
    ) -> bool:
        """查抑制状态。联系人级和企业级都查。

        **这个查询在发送关键路径上**，必须走索引、不允许降级为
        「查不到就放行」——抑制名单服务不可用时应该拒绝发送，
        不是继续发送。
        """
        ...

    # --- 查询 -------------------------------------------------------------

    async def get_campaign(
        self, tenant_id: TenantId, campaign_id: CampaignId
    ) -> CampaignView: ...

    async def list_due_enrollments(
        self, tenant_id: TenantId, limit: int
    ) -> list[EnrollmentView]:
        """列出到达发送时间的 enrollment，供 ``scheduler_worker`` 扫描。"""
        ...
