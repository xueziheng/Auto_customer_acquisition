"""客户自动准备的具名发送permission适配；不增加stage或改变既有顺序。"""
from typing import Protocol

from shared.schemas.identifiers import EnrollmentId, TenantId
from tool_gateway.pipeline import (
    CheckRejection,
    CheckStage,
    ToolCallContext,
    ToolInvocationState,
)


class CustomerPreparationSendGuard(Protocol):
    async def require(self, tenant_id: TenantId, enrollment_id: EnrollmentId) -> None:
        """旧入组不新增范围；受托入组必须重新核验当前批准、归属、资料和国家政策。"""
        ...


class CustomerPreparationPermissionCheck:
    name = "permission"

    def __init__(self, original: CheckStage, guard: CustomerPreparationSendGuard) -> None:
        self.original, self.guard = original, guard

    async def check(self, ctx: ToolCallContext, state: ToolInvocationState) -> CheckRejection | None:
        rejected = await self.original.check(ctx, state)
        if rejected is not None:
            return rejected
        enrollment = ctx.params.get("enrollment_id")
        if not isinstance(enrollment, str):
            return CheckRejection(self.name, "permission:customer_preparation", "客户准备授权不可用")
        try:
            await self.guard.require(ctx.tenant_id, EnrollmentId(enrollment))
        except Exception:  # noqa: BLE001 -- 当前事实缺失或故障均失败关闭，原异常不泄露
            return CheckRejection(self.name, "permission:customer_preparation", "客户准备授权或当前国家政策已失效")
        return None
