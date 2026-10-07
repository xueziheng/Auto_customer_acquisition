"""冻结语料到生产入站内容的窄适配器；不接触 expected 或选择模型。"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import cast

from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from agent_runtime.qualification_agent.agent import (
    ReplyClassificationResult,
    ReplyClassifier,
)
from shared.errors import ValidationError
from workflows.reply_qualification.evidence import require_reply_evidence
from workflows.reply_qualification.ports import ReplyMessageContent

_MESSAGE_KEYS = frozenset({"message_id", "subject", "body"})


@dataclass(frozen=True)
class GatewayReplyEvalInput:
    """可信 reader 返回的完整视图只用于内存验证，原件不进入模型 payload。"""

    message_id: str
    content: ReplyMessageContent = field(repr=False)


class GatewayReplyEvalClassifier:
    """仅按语料 ID 路由已验证入站映射；底层仍是原 Gateway 分类器。

    ``messages`` 必须来自生产 MIME 读取/投影后的 canonical 入站消息。
    映射中的真实 ID 仅供绑定校验，模型 payload 的收敛由原分类端口负责。
    拷贝映射以防校验后替换内容；本适配器不提供规则或离线回退。
    """

    def __init__(
        self, classifier: ReplyClassifier, messages: Mapping[str, GatewayReplyEvalInput]
    ) -> None:
        if not isinstance(messages, Mapping) or not messages:
            raise ValidationError("评估入站映射无效")
        validated: dict[str, GatewayReplyEvalInput] = {}
        canonical_ids: set[str] = set()
        guard = CredentialMarkerGuard()
        for case_id, message in messages.items():
            if (
                not isinstance(case_id, str)
                or not case_id.strip()
                or not isinstance(message, GatewayReplyEvalInput)
                or not isinstance(message.message_id, str)
                or not message.message_id.strip()
                or not isinstance(message.content, ReplyMessageContent)
                or not message.content.projected
                or message.content.subject != "(current reply)"
                or not isinstance(message.content.evidence_segments, tuple)
                or message.message_id in canonical_ids
            ):
                raise ValidationError("评估入站映射无效")
            guard.check(subject=message.content.subject, body=message.content.body)
            canonical_ids.add(message.message_id)
            validated[case_id] = message
        self._classifier = classifier
        self._messages = MappingProxyType(validated)

    @property
    def model(self) -> str:
        """保留实际分类器标识，不把受控测试冒充真实模型。"""
        return self._classifier.model

    async def classify(self, *, message: dict[str, str]) -> ReplyClassificationResult:
        """忽略 runner 自带主题/正文；未知或多义映射不允许模型调用。"""
        if (
            not isinstance(message, dict)
            or set(message) != _MESSAGE_KEYS
            or not isinstance(message.get("message_id"), str)
        ):
            raise ValidationError("评估消息映射缺失")
        current = self._messages.get(message["message_id"])
        if current is None:
            raise ValidationError("评估消息映射缺失")
        result = await self._classifier.classify(
            message={
                "message_id": current.message_id,
                "subject": cast(str, current.content.subject),
                "body": current.content.body,
            }
        )
        require_reply_evidence(current.content, result)
        return result
