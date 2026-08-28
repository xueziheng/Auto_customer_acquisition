"""来源工具专用tenant/permission stages。"""

from shared.evidence_read import QuoteEvidenceAccess
from shared.schemas.evidence_read import QuoteEvidenceError
from shared.schemas.identifiers import EmployeeId, TenantId
from tool_gateway.errors import ToolGatewayError
from tool_gateway.handlers.quote_evidence import map_evidence_error, read_request
from tool_gateway.handlers.quote_evidence_slots import QuoteEvidenceResultSlot
from tool_gateway.pipeline import CheckRejection, ToolCallContext, ToolInvocationState


class QuoteEvidenceTenantCheck:
    name = "tenant"

    def __init__(self, tenant_id: TenantId) -> None:
        self._tenant = tenant_id

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        del state
        try:
            read_request(ctx)
        except QuoteEvidenceError:
            return CheckRejection(
                self.name, "tenant:evidence_request", "来源请求不合法"
            )
        if ctx.tenant_id != self._tenant:
            return CheckRejection(
                self.name, "tenant:evidence_tenant", "当前无权读取来源"
            )
        return None


class QuoteEvidencePermissionCheck:
    name = "permission"

    def __init__(
        self, access: QuoteEvidenceAccess, slot: QuoteEvidenceResultSlot
    ) -> None:
        self._access, self._slot = access, slot

    async def check(
        self, ctx: ToolCallContext, state: ToolInvocationState
    ) -> CheckRejection | None:
        try:
            request = read_request(ctx)
            state.preflight = await self._access.authorize(
                ctx.tenant_id,
                request.source_ref,
                actor_id=EmployeeId(str(ctx.user_id)),
                scope=request.scope,
            )
            return None
        except QuoteEvidenceError as error:
            self._slot.put_failure(error.code)
            raise ToolGatewayError(map_evidence_error(error.code)) from None
        except Exception:  # noqa: BLE001 - 元数据依赖失败固定码
            self._slot.put_failure("source_unavailable")
            raise ToolGatewayError(map_evidence_error("source_unavailable")) from None
