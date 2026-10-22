"""Smart Inbox —— 统一处理客户回复。

GET  /inbox/conversations        会话列表（按分类/负责人过滤）
GET  /inbox/conversations/{id}   线程：消息 + 分类 + 提取的需求字段
POST /inbox/messages/{id}/correct-classification   人工纠正（原判保留）
GET /inbox/conversations/{id}/messages/{id}/next-questions 受权下一问建议
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from domains.conversations.schemas import (
    ConversationInboxDetail,
    ConversationInboxItem,
    MessageContentView,
    ReplyCategory,
    ReplyNextQuestionsView,
    ReplyWorkActionView,
    ReplyWorkStatus,
)
from domains.conversations.service import ConversationService
from domains.prospecting.private_reply_test import (
    PrivateReplyTestOptions,
    PrivateReplyTestPrepare,
    PrivateReplyTestPrepared,
)
from shared.errors import PermissionDenied, TransientError, ValidationError
from shared.schemas.identifiers import ConversationId, MessageId, ProspectAccountId

from ..composition.message_content import parse_message_content
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
# 当前范围由Conversations同事务重验；路由只做角色第一道门。
_INBOX_ROLES = frozenset({"boss", "manager", "sales"})


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
        identity.tenant_id,
        actor=identity.conversation_inbox_actor,
        category=category,
        limit=limit,
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
        identity.tenant_id,
        _conversation_id(conversation_id),
        actor=identity.conversation_inbox_actor,
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
        actor=identity.conversation_inbox_actor,
    )
    return ClassificationCorrectionAccepted(
        message_id=str(typed_id),
        category=body.category,
        corrected_by=str(identity.employee.employee_id),
    )


@router.get(
    "/inbox/conversations/{conversation_id}/messages/{message_id}/next-questions",
    response_model=ReplyNextQuestionsView,
)
async def get_next_questions(
    request: Request,
    conversation_id: str,
    message_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> ReplyNextQuestionsView:
    _require_inbox_role(identity)
    if request.query_params or await request.body():
        raise ValidationError("回复建议不接受客户端需求字段")
    if dependencies.reply_suggestions is None:
        raise TransientError("回复建议服务未配置")
    return await dependencies.reply_suggestions.read(
        identity.tenant_id,
        identity.employee.employee_id,
        _conversation_id(conversation_id),
        _message_id(message_id),
        actor=identity.conversation_inbox_actor,
    )


@router.get(
    "/inbox/messages/{message_id}/evidence",
    response_class=Response,
    responses={
        200: {
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            }
        },
        403: {"model": ApiErrorResponse},
    },
)
async def get_message_evidence(
    request: Request,
    message_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> Response:
    """仅安全下载原MIME，当前不提供独立附件或HTML执行预览。"""
    _require_inbox_role(identity)
    if request.query_params or await request.body():
        raise ValidationError("消息原件不接受额外参数")
    if dependencies.inbox_evidence is None:
        raise TransientError("消息原件服务未配置")
    typed_id = _message_id(message_id)
    content = await dependencies.inbox_evidence.read(
        identity.tenant_id, typed_id, actor=identity.conversation_inbox_actor
    )
    return Response(
        content,
        media_type="application/octet-stream",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="{typed_id}.eml"',
        },
    )


class ReplyWorkUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    expected_status: ReplyWorkStatus
    status: ReplyWorkStatus


@router.get("/inbox/reply-work", response_model=list[ReplyWorkActionView])
async def list_reply_work(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    status: ReplyWorkStatus = ReplyWorkStatus.PENDING,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
    account_id: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
) -> list[ReplyWorkActionView]:
    _require_inbox_role(identity)
    return list(await _conversation_service(dependencies).list_owned_reply_work(
        identity.tenant_id, actor=identity.conversation_inbox_actor,
        status=status, limit=limit,
        account_id=ProspectAccountId(account_id) if account_id else None,
    ))


@router.post("/inbox/reply-work/{action_id}", response_model=ReplyWorkActionView)
async def handle_reply_work(
    action_id: str,
    body: ReplyWorkUpdate,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> ReplyWorkActionView:
    _require_inbox_role(identity)
    if re.fullmatch(r"rwa_[0-7][0-9A-HJKMNP-TV-Z]{25}", action_id) is None:
        raise ValidationError("回复工作项编号无效")
    return await _conversation_service(dependencies).handle_reply_work(
        identity.tenant_id, action_id, actor=identity.conversation_inbox_actor,
        expected_status=body.expected_status, status=body.status,
    )


@router.get("/inbox/private-reply-test/options", response_model=PrivateReplyTestOptions)
async def private_reply_test_options(
    request: Request,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
) -> PrivateReplyTestOptions:
    _require_inbox_role(identity)
    application = getattr(request.app.state, "private_reply_test", None)
    if application is None:
        return PrivateReplyTestOptions()
    return await application.options(identity)


@router.post("/inbox/private-reply-test/prepare", response_model=PrivateReplyTestPrepared)
async def prepare_private_reply_test(
    request: Request, body: PrivateReplyTestPrepare,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
) -> PrivateReplyTestPrepared:
    _require_inbox_role(identity)
    application = getattr(request.app.state, "private_reply_test", None)
    if application is None:
        raise PermissionDenied("当前部署未开启私有回信测试")
    return await application.prepare(identity, body)


@router.get("/inbox/messages/{message_id}/content", response_model=MessageContentView)
async def get_message_content(
    request: Request, response: Response, message_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> MessageContentView:
    """沿原件 Gateway 当前授权返回纯文本；禁止 HTML 执行与响应缓存。"""
    _require_inbox_role(identity)
    if request.query_params or await request.body():
        raise ValidationError("消息内容不接受额外参数")
    if dependencies.inbox_evidence is None:
        raise TransientError("消息原件服务未配置")
    content = await dependencies.inbox_evidence.read(
        identity.tenant_id, _message_id(message_id), actor=identity.conversation_inbox_actor)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return parse_message_content(content)
