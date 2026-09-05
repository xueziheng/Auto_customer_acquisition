"""Campaign 精确版本的 canonical 审批事实；缺记录不借用客户端批准状态。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from domains.approvals.service import ApprovalService, ApprovalState, ApprovalType
from domains.outreach.schemas import CampaignApprovalSnapshot, CampaignApprovalState
from shared.errors import ValidationError
from shared.schemas.identifiers import ApprovalId, CampaignId, EmployeeId, TenantId


class CurrentCampaignApprovalReader:
    """只读取当前同租户、精确 change-set 的审批。"""

    def __init__(
        self,
        tenant_id: TenantId,
        approvals: ApprovalService,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._tenant, self._approvals, self._now = tenant_id, approvals, now

    async def get_campaign_approval(
        self, tenant_id: TenantId, campaign_id: CampaignId, version: int
    ) -> CampaignApprovalSnapshot | None:
        if tenant_id != self._tenant:
            raise ValidationError("当前审批事实不可用")
        ref = f"campaign:{campaign_id}:v{version}"
        view = await self._approvals.get_by_change_set(tenant_id, ref)
        if view is None:
            return None
        if (
            view.change_set_ref != ref
            or view.approval_type != ApprovalType.CAMPAIGN_BOUNDARY_CHANGE.value
        ):
            raise ValidationError("当前审批事实不可用")
        states = {
            ApprovalState.PENDING.value: CampaignApprovalState.PENDING,
            ApprovalState.APPROVED.value: CampaignApprovalState.APPROVED,
            ApprovalState.APPLIED.value: CampaignApprovalState.APPROVED,
            ApprovalState.REJECTED.value: CampaignApprovalState.REJECTED,
            ApprovalState.EXPIRED.value: CampaignApprovalState.EXPIRED,
            ApprovalState.APPLY_FAILED.value: CampaignApprovalState.REJECTED,
        }
        state = states.get(view.state)
        if state is None:
            raise ValidationError("当前审批事实不可用")
        if view.expires_at <= self._now() and state is CampaignApprovalState.PENDING:
            state = CampaignApprovalState.EXPIRED
        approved = state is CampaignApprovalState.APPROVED
        return CampaignApprovalSnapshot(
            tenant_id,
            campaign_id,
            version,
            ApprovalId(view.approval_id),
            state,
            EmployeeId(view.decided_by_employee)
            if approved and view.decided_by_employee
            else None,
            view.decided_at if approved else None,
        )
