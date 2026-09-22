"""API 与 scheduler 的持久接管通知机械映射；每个调用方注入自己的仓储。"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime

from notification_gateway.jobs import (
    NotificationContext,
    NotificationJob,
    NotificationJobStore,
    NotificationKind,
)
from notification_gateway.models import NotificationPriority
from shared.errors import ValidationError
from shared.schemas.identifiers import NotificationJobId, new_id
from workflows.human_handoff.flow import HandoffEscalationNotice

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_TENANT_ID = re.compile(rf"tn_{_ULID}\Z")
_EMPLOYEE_ID = re.compile(rf"emp_{_ULID}\Z")
_HANDOFF_ID = re.compile(rf"hand_{_ULID}\Z")
_OPPORTUNITY_ID = re.compile(rf"opp_{_ULID}\Z")
_HANDOFF_LEVELS = frozenset(
    {"owner", "manager", "boss", "boss_reminder", "owner_pending", "owner_reminder"}
)


def _is_utc(value: datetime) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() == UTC.utcoffset(value)
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
            or not isinstance(notice.handoff_id, str)
            or _HANDOFF_ID.fullmatch(notice.handoff_id) is None
            or not isinstance(notice.opportunity_id, str)
            or _OPPORTUNITY_ID.fullmatch(notice.opportunity_id) is None
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
