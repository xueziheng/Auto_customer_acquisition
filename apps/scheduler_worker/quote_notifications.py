"""报价结果只入原持久通知队列，LOW由原通知worker仅投站内。"""

from collections.abc import Callable
from datetime import datetime

from notification_gateway.jobs import NotificationJob, NotificationJobStore
from notification_gateway.models import NotificationPriority
from notification_gateway.quote_results import (
    SOURCE_EVENT,
    quote_result_context,
    quote_result_fingerprint,
)
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import (
    EmployeeId,
    NotificationJobId,
    QuoteId,
    RunId,
    TenantId,
    new_id,
)


class NotificationJobQuoteApprovalNotifier:
    def __init__(
        self,
        jobs: NotificationJobStore,
        *,
        tenant_id: TenantId,
        now: Callable[[], datetime],
    ) -> None:
        self._jobs, self._tenant, self._now = jobs, tenant_id, now

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
        """稳定K指纹去重，保存安全ID与固定结果，不构造伪DomainEvent。"""
        if tenant_id != self._tenant:
            raise TenantIsolationViolation("跨租户报价通知被拒绝")
        context = quote_result_context(
            tenant_id, recipient_id, quote_id, run_id, outcome, idempotency_key
        )
        await self._jobs.enqueue(
            NotificationJob(
                NotificationJobId(new_id("njb")),
                tenant_id,
                recipient_id,
                NotificationPriority.LOW,
                context,
                quote_result_fingerprint(idempotency_key),
                SOURCE_EVENT,
                idempotency_key,
                self._now(),
            )
        )
