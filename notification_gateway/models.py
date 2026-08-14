"""通知事件模型与渠道 Protocol。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from shared.errors import ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId

if TYPE_CHECKING:
    from notification_gateway.jobs import NotificationContext
    from shared.schemas.identifiers import NotificationJobId


class NotificationPriority(str, Enum):
    URGENT = "urgent"
    """接管请求、身份熔断、承诺逾期升级。多渠道并发。"""

    NORMAL = "normal"
    LOW = "low"
    """只进站内。"""


@dataclass(frozen=True)
class Notification:
    """通知。**必须自带上下文**——「有个高意向客户」这种一句话
    通知会被忽略，被忽略的通知等于没发。

    字段：
        tenant_id, recipient
        priority
        title:        一句话（推送预览）
        context:      结构化上下文（客户/国家/需求/负责人/为什么重要）
        next_step:    下一步建议
        due_at:       相关截止时间
        link:         任务/机会的深链
        source_event: 触发的领域事件类型（审计与去重）
        dedup_key:    同一事件重复投递不重复通知
    """

    tenant_id: TenantId
    recipient: EmployeeId
    priority: NotificationPriority
    title: str
    context: NotificationContext
    source_event: str
    dedup_key: str
    next_step: str | None = None
    due_at: datetime | None = None
    link: str | None = None
    source_job_id: NotificationJobId | None = None

    def __post_init__(self) -> None:
        """拒绝未类型化上下文、非枚举优先级与非 UTC 截止时间。"""
        from notification_gateway.jobs import NotificationContext

        if (
            not isinstance(self.priority, NotificationPriority)
            or not isinstance(self.context, NotificationContext)
            or (self.due_at is not None and not _is_utc(self.due_at))
        ):
            raise ValidationError("通知无效")


@runtime_checkable
class NotificationChannel(Protocol):
    """渠道适配器 Protocol —— 插件点 4。

    实现约定：投递失败抛 TransientError 由网关重试；
    重试不阻塞任何业务流程。凭证在 connector 层。
    """

    name: str

    async def deliver(self, notification: Notification) -> None: ...


def _is_utc(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None and value.utcoffset().total_seconds() == 0
