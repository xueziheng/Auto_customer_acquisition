"""生产 MessageContentReader：message_id → raw_artifact_ref → artifact store → 解析。

- 组合侧适配器（apps/scheduler_worker/adapters）：实现
  ``workflows.reply_qualification.ports.MessageContentReader`` 窄端口；
  workflows 不 import 本模块（依赖方向：apps → workflows 端口 + artifact_store + infra）。
- **不持有绑定 AsyncSession 的仓储**：注入 tenant-bound conversations UoW factory
  （``Callable[[TenantId], ConversationsUnitOfWork]``，与服务层模式一致），每次
  ``load`` 新开 ``async with uow`` 读取 message 后即退出。
- 大小策略（构造时显式注入并校验正数，无隐藏默认/魔法值）：
  - ``max_raw_bytes``：解析前对 artifact bytes 上限（防御超大原文）
  - ``max_subject_chars`` / ``max_body_chars``：解析后分别确定性截断
  - 截断发生在内存内：guard/model 只见截断后内容；截断外内容物理上
    未传递给模型。成本记录：超长回复的尾部丢失，分类基于前半部分；
    原文永在 artifact store，人工接管可回看。
- 异常映射：artifact store 的 not-found/integrity 已知异常与解析失败一律转
  固定安全 ``ValidationError``（不回显正文/地址）；**只捕获明确异常，
  不 catch BaseException/CancelledError/系统错误**。
- message 行不存在 → 按端口契约返回 ``None``。
"""

from __future__ import annotations

from collections.abc import Callable

from artifact_store.errors import (
    ArtifactIntegrityError,
    ArtifactNotFoundError,
)
from artifact_store.store import RawArtifactKind, RawArtifactStore
from domains.conversations.service import ConversationsUnitOfWork
from shared.errors import ValidationError
from shared.schemas.identifiers import ArtifactId, MessageId, TenantId
from workflows.reply_qualification.ports import (
    ReplyMessageContent,
)

from .rfc822 import parse_email_rfc822

_RAW_TOO_LARGE = "回复消息原文超限"
_ARTIFACT_MISSING = "回复消息原文不可读"
_ARTIFACT_INTEGRITY = "回复消息原文不可读"
_NOT_EMAIL = "回复消息原文非邮件"
_NOT_INBOUND = "回复消息非入站"
_BODY_INVALID = "回复消息正文无效"


class ArtifactMessageContentReader:
    """按 message_id 从 artifact store 原文抽取 subject/body 的生产实现。"""

    def __init__(
        self,
        conversations_uow_factory: Callable[[TenantId], ConversationsUnitOfWork],
        raw_store: RawArtifactStore,
        *,
        max_raw_bytes: int,
        max_subject_chars: int,
        max_body_chars: int,
    ) -> None:
        if not callable(conversations_uow_factory) or not isinstance(
            raw_store, RawArtifactStore
        ):
            raise ValidationError("消息内容读取器依赖无效")
        if (
            not isinstance(max_raw_bytes, int)
            or max_raw_bytes <= 0
            or not isinstance(max_subject_chars, int)
            or max_subject_chars <= 0
            or not isinstance(max_body_chars, int)
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
            meta, raw = await self._store.get(tenant_id, artifact_id)
        except ArtifactNotFoundError:
            raise ValidationError(_ARTIFACT_MISSING) from None
        except ArtifactIntegrityError:
            raise ValidationError(_ARTIFACT_INTEGRITY) from None
        if len(raw) > self._max_raw_bytes:
            raise ValidationError(_RAW_TOO_LARGE)
        if (
            meta.kind is not RawArtifactKind.EMAIL_RAW
            or meta.mime_type != "message/rfc822"
        ):
            raise ValidationError(_NOT_EMAIL)
        view = parse_email_rfc822(raw)
        subject = (
            view.subject[: self._max_subject_chars]
            if view.subject is not None
            else None
        )
        body = view.body[: self._max_body_chars]
        if not body.strip():
            raise ValidationError(_BODY_INVALID)
        return ReplyMessageContent(subject=subject, body=body)
