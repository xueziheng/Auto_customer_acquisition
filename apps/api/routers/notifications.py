"""Internal Smart Inbox API（仅本人收件箱）。

GET  /notifications                   当前员工的通知列表
POST /notifications/{notification_id}/read   标记已读（只影响本人）
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from notification_gateway.inbox import InAppNotificationView
from shared.errors import ValidationError
from shared.schemas.identifiers import NotificationId

from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_request_identity,
    require_inbox_access,
)
from ..identity import RequestIdentity
from ..middleware import ApiErrorResponse

router = APIRouter()

_NOTIFICATION_ID_RE = re.compile(r"ntf_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_KNOWN_EMPLOYEE_ROLES = frozenset(
    {"boss", "manager", "sales", "sourcing", "product", "finance", "viewer"}
)


@router.get(
    "/notifications",
    response_model=list[InAppNotificationView],
    responses={
        400: {"model": ApiErrorResponse},
        403: {"model": ApiErrorResponse},
    },
)
async def list_notifications(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[
        ConfiguredApiDependencies, Depends(get_api_dependencies)
    ],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[InAppNotificationView]:
    """只返回当前员工本人的通知；任何 recipient 入参一律忽略。"""
    actor = require_inbox_access(identity, allowed_roles=_KNOWN_EMPLOYEE_ROLES)
    return list(
        await dependencies.in_app_notifications.list_notifications(
            identity.tenant_id, actor=actor, limit=limit, before=None
        )
    )


@router.post(
    "/notifications/{notification_id}/read",
    response_model=InAppNotificationView,
    responses={
        400: {"model": ApiErrorResponse},
        403: {"model": ApiErrorResponse},
    },
)
async def mark_notification_read(
    notification_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[
        ConfiguredApiDependencies, Depends(get_api_dependencies)
    ],
) -> InAppNotificationView:
    """把本人收件箱中指定通知标记为已读；跨收件人一律不存在。"""
    if _NOTIFICATION_ID_RE.fullmatch(notification_id) is None:
        raise ValidationError("notification id 无效")
    actor = require_inbox_access(identity, allowed_roles=_KNOWN_EMPLOYEE_ROLES)
    return await dependencies.in_app_notifications.mark_read(
        identity.tenant_id, NotificationId(notification_id), actor=actor
    )
