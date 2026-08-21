"""Smart Inbox —— 统一处理客户回复。

GET  /inbox/conversations        会话列表（按分类/负责人过滤）
GET  /inbox/conversations/{id}   线程：消息 + 分类 + 提取的需求字段
POST /inbox/messages/{id}/correct-classification   人工纠正（原判保留）
POST /inbox/conversations/{id}/draft-reply         请求追问草稿
                                 （qualification_agent，最多两个主题）
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from domains.conversations.schemas import (
    ConversationInboxDetail,
    ConversationInboxItem,
    ReplyCategory,
)
from domains.conversations.service import ConversationService
from shared.errors import PermissionDenied, TransientError, ValidationError
from shared.schemas.identifiers import ConversationId, MessageId

from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_request_identity,
)
from ..identity import RequestIdentity
from ..middleware import ApiErrorResponse

router = APIRouter()

_CONVERSATION_ID_RE = re.compile(r"con_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_MESSAGE_ID_RE = re.compile(r"msg_[0-7][0-9A-HJKMNP-TV-Z]{25}")
# 会话尚未携带 ownership 投影；在接入负责人范围前只开放租户级 boss，
# 否则 manager/sales 会读到整租户回复，违反最小权限。
_INBOX_ROLES = frozenset({"boss"})


class ClassificationCorrectionBody(BaseModel):
    """人工纠正只接受 14 类枚举；原模型判定由域服务保留。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    category: ReplyCategory = Field(strict=False)


class ClassificationCorrectionAccepted(BaseModel):
    """纠正写入确认；详情刷新后可见完整 append-only 证据链。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    message_id: str
    category: ReplyCategory
    corrected_by: str


def _require_inbox_role(identity: RequestIdentity) -> None:
    if identity.employee.role not in _INBOX_ROLES:
        raise PermissionDenied("当前角色无权访问 Smart Inbox")


def _conversation_service(
    dependencies: ConfiguredApiDependencies,
) -> ConversationService:
    if dependencies.conversations is None:
        raise TransientError("会话服务未配置")
    return dependencies.conversations


def _conversation_id(value: str) -> ConversationId:
    if _CONVERSATION_ID_RE.fullmatch(value) is None:
        raise ValidationError("conversation id 无效")
    return ConversationId(value)


def _message_id(value: str) -> MessageId:
    if _MESSAGE_ID_RE.fullmatch(value) is None:
        raise ValidationError("message id 无效")
    return MessageId(value)


@router.get(
    "/inbox/conversations",
    response_model=list[ConversationInboxItem],
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def list_inbox_conversations(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    category: Annotated[ReplyCategory | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ConversationInboxItem]:
    _require_inbox_role(identity)
    return await _conversation_service(dependencies).list_inbox(
        identity.tenant_id, category=category, limit=limit
    )


@router.get(
    "/inbox/conversations/{conversation_id}",
    response_model=ConversationInboxDetail,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def get_inbox_conversation(
    conversation_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> ConversationInboxDetail:
    _require_inbox_role(identity)
    return await _conversation_service(dependencies).get_inbox_detail(
        identity.tenant_id, _conversation_id(conversation_id)
    )


@router.post(
    "/inbox/messages/{message_id}/correct-classification",
    response_model=ClassificationCorrectionAccepted,
    responses={400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}},
)
async def correct_inbox_classification(
    message_id: str,
    body: ClassificationCorrectionBody,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> ClassificationCorrectionAccepted:
    _require_inbox_role(identity)
    typed_id = _message_id(message_id)
    await _conversation_service(dependencies).correct_classification(
        identity.tenant_id,
        typed_id,
        body.category,
        str(identity.employee.employee_id),
    )
    return ClassificationCorrectionAccepted(
        message_id=str(typed_id),
        category=body.category,
        corrected_by=str(identity.employee.employee_id),
    )
