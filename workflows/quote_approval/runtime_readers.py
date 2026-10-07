"""运行时当前身份投影及显式关闭的发送回执端口；无数据库或Provider访问。"""

from domains.costing.service import CostingActor, CostingScope
from domains.quotations.errors import QuotationUnavailableError
from domains.quotations.service import QuoteSendReceipt
from shared.evidence_read import QuoteEvidenceContextReader
from shared.schemas.identifiers import EmployeeId, MessageAttemptId, TenantId
from shared.schemas.quote_facts import QuoteEmployeeFact


class CurrentQuotationActorReader:
    """复用公开当前员工事实，服务仍负责在职/角色/用途判权。"""

    def __init__(self, contexts: QuoteEvidenceContextReader) -> None:
        self._contexts = contexts

    async def read_current(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> QuoteEmployeeFact | None:
        fact = await self._contexts.read_actor(tenant_id, employee_id)
        if fact is not None and (fact.tenant_id, fact.employee_id) != (
            tenant_id,
            employee_id,
        ):
            raise QuotationUnavailableError("dependency_unavailable")
        return fact


class CurrentCostingActorReader:
    """当前员工投影为成本身份，不复制成本域角色允许矩阵。"""

    def __init__(self, contexts: QuoteEvidenceContextReader) -> None:
        self._actors = CurrentQuotationActorReader(contexts)

    async def read_current(
        self, tenant_id: TenantId, actor_id: EmployeeId
    ) -> CostingActor | None:
        fact = await self._actors.read_current(tenant_id, actor_id)
        if fact is None or not fact.is_active:
            return None
        return CostingActor(fact.employee_id, fact.role, CostingScope.TENANT)


class UnavailableQuoteSendReceiptReader:
    """本批未开放发送，下载或旧Outreach sent均不能自证精确报价回执。"""

    async def read(
        self, tenant_id: TenantId, attempt_id: MessageAttemptId, *, actor_id: EmployeeId
    ) -> QuoteSendReceipt | None:
        raise QuotationUnavailableError("dependency_unavailable")
