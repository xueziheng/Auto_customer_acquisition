"""成本完成reader只调用报价公开持久查询，不循环调用创建应用。"""

from domains.quotations.errors import (
    QuotationPermissionError,
    QuotationUnavailableError,
)
from domains.quotations.schemas import QuotationActor
from domains.quotations.service import QuotationActorReader, QuotationVersionService
from shared.schemas.identifiers import EmployeeId, TenantId
from shared.schemas.quote_creation import QuoteCreationCompletion


class PersistentQuoteCreationCompletionReader:
    """从真实报价生成完成事实；没有引用调用方quote_id或verified标志的入口。"""

    def __init__(
        self, quotations: QuotationVersionService, actors: QuotationActorReader
    ) -> None:
        """公开服务和真实当前员工reader均必填，无缺省后备。"""
        self._quotes, self._actors = quotations, actors

    async def read(
        self, tenant_id: TenantId, operation_id: str, *, actor_id: EmployeeId
    ) -> QuoteCreationCompletion | None:
        """锁外读取当前身份，再由报价域执行内部读取授权和完整性校验。"""
        try:
            actor = await self._actors.read_current(tenant_id, actor_id)
        except Exception:  # noqa: BLE001 -- 读取依赖异常不能变成成功receipt或泄露原文
            raise QuotationUnavailableError("dependency_unavailable") from None
        if (
            actor is None
            or actor.tenant_id != tenant_id
            or actor.employee_id != actor_id
            or not actor.is_active
        ):
            raise QuotationPermissionError("permission_denied")
        return await self._quotes.creation_completion(
            tenant_id,
            operation_id,
            actor=QuotationActor(employee_id=actor.employee_id, role=actor.role),
        )
