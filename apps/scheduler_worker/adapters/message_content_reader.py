"""按真实Message绑定有界读取不可变邮件；完整候选先护栏，超限拒绝不裁剪。"""

from __future__ import annotations

import re
from collections.abc import Callable

from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from artifact_store.errors import (
    ArtifactBoundedReadUnavailable,
    ArtifactIntegrityError,
    ArtifactNotFoundError,
    ArtifactReadLimitExceeded,
)
from artifact_store.store import BoundedRawArtifactStore, RawArtifactKind
from connectors.gmail.inbound_mime import parse_inbound_content
from domains.conversations.service import ConversationsUnitOfWork
from shared.errors import ValidationError
from shared.schemas.email_inbound import InboundDisposition
from shared.schemas.identifiers import ArtifactId, MessageId, TenantId
from workflows.reply_qualification.ports import (
    ReplyMessageContent,
)

_RAW_TOO_LARGE = "回复消息原文超限"
_ARTIFACT_MISSING = "回复消息原文不可读"
_ARTIFACT_INTEGRITY = "回复消息原文不可读"
_NOT_EMAIL = "回复消息原文非邮件"
_NOT_INBOUND = "回复消息非入站"
_BODY_INVALID = "回复消息正文无效"
_PRIVATE_REFERENCE = re.compile(
    r"(?:[^\s@<>]+@[^\s@<>]+|\bwww\.[^\s<>]+|[a-z][a-z0-9+.-]*://[^\s<>]+|\b(?:art|obj)_[A-Za-z0-9_-]+|(?:/Users/|/Volumes/|/home/)[^\s<>]+)",
    re.IGNORECASE,
)
_PROJECTION_MARKER = "[private reference omitted]"


class ArtifactMessageContentReader:
    """按 message_id 从 artifact store 原文抽取 subject/body 的生产实现。"""

    def __init__(
        self,
        conversations_uow_factory: Callable[[TenantId], ConversationsUnitOfWork],
        raw_store: BoundedRawArtifactStore,
        *,
        max_raw_bytes: int,
        max_subject_chars: int,
        max_body_chars: int,
    ) -> None:
        if not callable(conversations_uow_factory) or not callable(
            getattr(raw_store, "get_bounded", None)
        ):
            raise ValidationError("消息内容读取器依赖无效")
        if (
            type(max_raw_bytes) is not int
            or max_raw_bytes <= 0
            or type(max_subject_chars) is not int
            or max_subject_chars <= 0
            or type(max_body_chars) is not int
            or max_body_chars <= 0
        ):
            raise ValidationError("消息内容读取器上限无效")
        self._uow_factory = conversations_uow_factory
        self._store = raw_store
        self._max_raw_bytes = max_raw_bytes
        self._max_subject_chars = max_subject_chars
        self._max_body_chars = max_body_chars

    async def load(
        self, tenant_id: TenantId, message_id: MessageId
    ) -> ReplyMessageContent | None:
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValidationError("消息内容租户无效")
        if not isinstance(message_id, str) or not message_id:
            raise ValidationError("消息内容 message_id 无效")
        async with self._uow_factory(tenant_id) as uow:
            message = await uow.messages.get(tenant_id, message_id)
        if message is None:
            return None
        if message.direction.value != "inbound":
            raise ValidationError(_NOT_INBOUND)
        raw_ref = message.raw_artifact_ref
        if not isinstance(raw_ref, str) or not raw_ref:
            raise ValidationError(_ARTIFACT_MISSING)
        # raw_artifact_ref 是 str（Message 模型契约），显式适配为 ArtifactId；
        # 不做格式猜测/normalize，store 侧自有校验兜底
        artifact_id = ArtifactId(raw_ref)
        try:
            meta, raw = await self._store.get_bounded(
                tenant_id, artifact_id, maximum_bytes=self._max_raw_bytes
            )
        except ArtifactReadLimitExceeded:
            raise ValidationError(_RAW_TOO_LARGE) from None
        except ArtifactBoundedReadUnavailable:
            raise ValidationError(_ARTIFACT_MISSING) from None
        except ArtifactNotFoundError:
            raise ValidationError(_ARTIFACT_MISSING) from None
        except ArtifactIntegrityError:
            raise ValidationError(_ARTIFACT_INTEGRITY) from None
        if len(raw) > self._max_raw_bytes:
            raise ValidationError(_RAW_TOO_LARGE)
        if (
            meta.tenant_id != tenant_id
            or meta.artifact_id != artifact_id
            or meta.kind is not RawArtifactKind.EMAIL_RAW
            or meta.mime_type != "message/rfc822"
        ):
            raise ValidationError(_NOT_EMAIL)
        view = parse_inbound_content(raw)
        if view.disposition is not InboundDisposition.CANDIDATE:
            raise ValidationError(_BODY_INVALID)
        guard = CredentialMarkerGuard()
        guard.check(subject=view.subject, body=view.guard_body)
        guard.check(subject=view.subject, body=view.body)
        if (
            len(view.subject or "") > self._max_subject_chars
            or len(view.body) > self._max_body_chars
        ):
            raise ValidationError("回复内容超过模型读取预算，需人工核对")
        if not view.evidence_available or not view.evidence_segments:
            raise ValidationError("回复当前表达无法可靠区分，需人工核对")
        return ReplyMessageContent(
            subject="(current reply)",
            body="\n[current expression boundary]\n".join(
                _PRIVATE_REFERENCE.sub(_PROJECTION_MARKER, segment)
                for segment in view.evidence_segments
            ),
            projected=True,
            original_subject=view.subject or None,
            original_body=view.body,
            evidence_segments=view.evidence_segments,
        )
