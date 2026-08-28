"""成本资料的纯用途权限，复用成本矩阵而不添加CRM门。"""

from shared.errors import PermissionDenied
from shared.schemas.evidence_read import QuoteEvidenceError
from shared.schemas.identifiers import TenantId
from shared.schemas.quote_facts import QuoteEmployeeFact

from .permissions import (
    CostingAction,
    CostingActor,
    CostingScope,
    Phase1CostingAuthorizer,
)


def require_pricing_source_access(
    tenant_id: TenantId, actor: QuoteEmployeeFact
) -> None:
    """仅当前在职且同tenant四成本角色；不批准业务写入。"""
    if actor.tenant_id != tenant_id or actor.is_active is not True:
        raise QuoteEvidenceError("permission_denied")
    try:
        Phase1CostingAuthorizer(tenant_id).require(
            CostingActor(actor.employee_id, actor.role, CostingScope.TENANT),
            CostingAction.EVIDENCE_CONFIRM,
            tenant_id,
        )
    except PermissionDenied:
        raise QuoteEvidenceError("permission_denied") from None
