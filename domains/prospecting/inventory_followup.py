"""搜索请求的私有自动准备委托；公共候选库永远不接收此对象。"""

from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from domains.prospecting.inventory import InventoryFollowupScope, InventoryQuery
from shared.schemas.identifiers import EmployeeId, TenantId


class InventoryFollowupAuthorization(BaseModel):
    """当前公共服务核验后的安全引用；不含客户原文或模型输出。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: TenantId
    actor_employee_id: EmployeeId
    actor_user_id: str
    owner_employee_id: EmployeeId
    scope: InventoryFollowupScope
    policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    authorized_at: AwareDatetime
    query: InventoryQuery


class InventoryFollowupAuthority(Protocol):
    async def authorize(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        scope: InventoryFollowupScope,
        query: InventoryQuery,
    ) -> InventoryFollowupAuthorization:
        """重新读取当前员工、精确批准版本、负责人和知识引用；失败不得建任务。"""
        ...


class PreparationBinding(BaseModel):
    """持久任务租约，旧令牌或版本不能推进；result_refs仅容纳安全实体标识。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: TenantId
    allocation_id: str
    request_key_hash: str
    authorization: InventoryFollowupAuthorization
    result_refs: dict[str, str]
    ownership_locked_at: AwareDatetime | None = None


class PreparationClaim(PreparationBinding):
    """正在执行的租约；发送检查只读取Binding，不伪造processing授权。"""
    lease_token: str
    version: int
    stage: str


PREPARATION_BLOCK_REASONS = frozenset({
    "authorization_revoked", "authorization_changed", "ownership_changed",
    "evidence_unavailable", "evidence_invalid", "product_fit_missing",
    "contact_basis_missing", "country_policy_denied", "country_policy_unavailable",
    "country_policy_requirement_unsupported", "verification_unavailable",
    "verification_failed", "privacy_requested", "sender_unavailable",
    "enrollment_denied", "external_reconciliation_required", "preparation_failed",
})
