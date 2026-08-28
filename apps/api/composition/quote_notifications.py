"""API报价结果沿已有仅结构化日志出口，不宣称站内或邮件已投递。"""

from notification_gateway.models import Notification, NotificationPriority
from notification_gateway.quote_results import (
    NEXT_STEP,
    SOURCE_EVENT,
    TITLE,
    quote_result_context,
    quote_result_link,
)
from notification_gateway.router import NotificationRouter
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import EmployeeId, QuoteId, RunId, TenantId


class RuntimeQuoteApprovalNotifier:
    def __init__(self, router: NotificationRouter, *, tenant_id: TenantId) -> None:
        self._router, self._tenant = router, tenant_id

    async def notify(
        self,
        tenant_id: TenantId,
        quote_id: QuoteId,
        *,
        run_id: RunId,
        recipient_id: EmployeeId,
        outcome: str,
        idempotency_key: str,
    ) -> None:
        """只向原router发送固定LOW结果，跨运行租户零通知。"""
        if tenant_id != self._tenant:
            raise TenantIsolationViolation("跨租户报价通知被拒绝")
        context = quote_result_context(
            tenant_id, recipient_id, quote_id, run_id, outcome, idempotency_key
        )
        await self._router.dispatch(
            Notification(
                tenant_id,
                recipient_id,
                NotificationPriority.LOW,
                TITLE,
                context,
                SOURCE_EVENT,
                idempotency_key,
                next_step=NEXT_STEP,
                link=quote_result_link(quote_id),
            )
        )
