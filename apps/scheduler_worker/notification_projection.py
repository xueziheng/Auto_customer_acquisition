"""把受支持领域事件安全、幂等地投影为持久通知任务。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from infra.db.outbox import serialize
from notification_gateway.jobs import (
    NotificationContext,
    NotificationJob,
    NotificationJobStore,
    NotificationKind,
)
from notification_gateway.models import NotificationPriority
from shared.errors import TenantIsolationViolation, ValidationError
from shared.events.catalog import (
    ApprovalDecided,
    CommitmentOverdue,
    DomainEvent,
    HandoffQueueBacklogged,
    HandoffRequested,
    ReputationThresholdBreached,
    SendingIdentitySuspended,
)
from shared.schemas.identifiers import (
    EmployeeId,
    NotificationJobId,
    TenantId,
    new_id,
)
from workflows.human_handoff.flow import HandoffEscalationNotice

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_TENANT_ID = re.compile(rf"tn_{_ULID}\Z")
_EMPLOYEE_ID = re.compile(rf"emp_{_ULID}\Z")
_SUPPORTED_EVENTS = (
    HandoffRequested,
    HandoffQueueBacklogged,
    SendingIdentitySuspended,
    ReputationThresholdBreached,
    CommitmentOverdue,
    ApprovalDecided,
)
_HANDOFF_LEVELS = frozenset({"owner", "manager", "boss"})


@dataclass(frozen=True)
class NotificationAudienceMember:
    tenant_id: TenantId
    employee_id: EmployeeId

    def __post_init__(self) -> None:
        if (
            not isinstance(self.tenant_id, str)
            or _TENANT_ID.fullmatch(self.tenant_id) is None
            or not isinstance(self.employee_id, str)
            or _EMPLOYEE_ID.fullmatch(self.employee_id) is None
        ):
            raise ValidationError("通知受众成员无效")


class NotificationAudienceResolver(Protocol):
    async def recipients_for(
        self, tenant_id: TenantId, event: DomainEvent
    ) -> tuple[NotificationAudienceMember, ...]: ...


def notification_source_fingerprint(event: DomainEvent) -> str:
    """对事件类型与既有 canonical JSON 取 SHA-256，不返回原始载荷。"""
    if not isinstance(event, DomainEvent):
        raise ValidationError("通知事件无效")
    canonical = json.dumps(serialize(event), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{type(event).__name__}:{canonical}".encode()).hexdigest()


class NotificationProjectionHandler:
    def __init__(
        self,
        *,
        tenant_id: TenantId,
        audience: NotificationAudienceResolver,
        jobs: NotificationJobStore,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[str], str] = new_id,
    ) -> None:
        if not isinstance(tenant_id, str) or _TENANT_ID.fullmatch(tenant_id) is None:
            raise ValidationError("通知投影租户无效")
        self._tenant_id = tenant_id
        self._audience = audience
        self._jobs = jobs
        self._now = now
        self._id_factory = id_factory

    async def handle(self, event: DomainEvent) -> None:
        """先判白名单和租户，再逐收件人 durable enqueue。"""
        if type(event) not in _SUPPORTED_EVENTS:
            raise ValidationError("通知事件类型不受支持")
        if event.tenant_id != self._tenant_id:
            raise TenantIsolationViolation("跨租户通知投影被拒绝")
        context, priority = _project_context(event)
        fingerprint = notification_source_fingerprint(event)
        recipients = await self._audience.recipients_for(self._tenant_id, event)
        for recipient in recipients:
            if (
                not isinstance(recipient, NotificationAudienceMember)
                or recipient.tenant_id != self._tenant_id
            ):
                raise TenantIsolationViolation("通知受众租户不一致")
            created_at = self._now()
            if not _is_utc(created_at):
                raise ValidationError("通知任务时间无效")
            await self._jobs.enqueue(
                NotificationJob(
                    NotificationJobId(self._id_factory("njb")),
                    self._tenant_id,
                    recipient.employee_id,
                    priority,
                    context,
                    fingerprint,
                    type(event).__name__,
                    f"{fingerprint}:{recipient.employee_id}",
                    created_at,
                )
            )


class NotificationJobHandoffNotifier:
    """把 workflow notice 写入同一任务仓储，不在 scheduler 直投渠道。"""

    def __init__(
        self,
        jobs: NotificationJobStore,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        id_factory: Callable[[str], str] = new_id,
    ) -> None:
        self._jobs = jobs
        self._now = now
        self._id_factory = id_factory

    async def notify(self, notice: HandoffEscalationNotice) -> None:
        if (
            not isinstance(notice, HandoffEscalationNotice)
            or not isinstance(notice.tenant_id, str)
            or _TENANT_ID.fullmatch(notice.tenant_id) is None
            or not isinstance(notice.recipient_id, str)
            or _EMPLOYEE_ID.fullmatch(notice.recipient_id) is None
            or notice.level not in _HANDOFF_LEVELS
        ):
            raise ValidationError("人工接管通知无效")
        created_at = self._now()
        if not _is_utc(created_at):
            raise ValidationError("通知任务时间无效")
        fingerprint = hashlib.sha256(
            f"HandoffEscalationNotice:{notice.dedup_key}".encode()
        ).hexdigest()
        await self._jobs.enqueue(
            NotificationJob(
                NotificationJobId(self._id_factory("njb")),
                notice.tenant_id,
                notice.recipient_id,
                NotificationPriority.URGENT,
                NotificationContext(
                    NotificationKind.HANDOFF_ESCALATION,
                    str(notice.handoff_id),
                    str(notice.opportunity_id),
                    reason_code=notice.level,
                    level=None,
                ),
                fingerprint,
                "HandoffEscalationNotice",
                notice.dedup_key,
                created_at,
            )
        )


def _project_context(
    event: DomainEvent,
) -> tuple[NotificationContext, NotificationPriority]:
    if isinstance(event, HandoffRequested):
        if event.opportunity_id is None:
            raise ValidationError("人工接管通知缺少机会")
        return (
            NotificationContext(
                NotificationKind.HANDOFF_ESCALATION,
                str(event.handoff_id),
                str(event.opportunity_id),
                None,
                None,
            ),
            NotificationPriority.URGENT,
        )
    if isinstance(event, HandoffQueueBacklogged):
        if type(event.queue_depth) is not int or not 0 <= event.queue_depth <= 99:
            raise ValidationError("接管队列通知等级无效")
        return (
            NotificationContext(
                NotificationKind.HANDOFF_QUEUE_BACKLOGGED,
                "handoff_queue",
                None,
                None,
                event.queue_depth,
            ),
            NotificationPriority.URGENT,
        )
    if isinstance(event, SendingIdentitySuspended):
        return (
            NotificationContext(
                NotificationKind.SENDING_IDENTITY_SUSPENDED,
                str(event.sending_identity_id),
                None,
                event.reason,
                None,
            ),
            NotificationPriority.URGENT,
        )
    if isinstance(event, ReputationThresholdBreached):
        return (
            NotificationContext(
                NotificationKind.REPUTATION_THRESHOLD_BREACHED,
                str(event.sending_identity_id),
                None,
                f"{event.metric}:{event.severity}",
                None,
            ),
            NotificationPriority.NORMAL,
        )
    if isinstance(event, CommitmentOverdue):
        return (
            NotificationContext(
                NotificationKind.COMMITMENT_OVERDUE,
                event.commitment_id,
                None,
                None,
                None,
            ),
            NotificationPriority.URGENT,
        )
    if isinstance(event, ApprovalDecided):
        if event.decided_by is None:
            raise ValidationError("审批通知缺少决定人")
        return (
            NotificationContext(
                NotificationKind.APPROVAL_DECIDED,
                event.approval_id,
                str(event.decided_by),
                event.decision,
                None,
            ),
            NotificationPriority.NORMAL,
        )
    raise ValidationError("通知事件类型不受支持")


def _is_utc(value: datetime) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
        and value.utcoffset() == UTC.utcoffset(value)
    )
