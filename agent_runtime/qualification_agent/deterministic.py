"""本机无模型回复分类；只接受可由固定短语证明的明确类别。"""

from __future__ import annotations

import re

from agent_runtime.qualification_agent.agent import ReplyClassificationResult
from domains.conversations.schemas import ReplyCategory, ReplySuppressScope
from shared.errors import ValidationError


def _patterns(*values: str) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(value, re.IGNORECASE) for value in values)


_UNSUBSCRIBE = _patterns(
    r"\b(?:unsubscribe|unsubsribe)\b",
    r"\bopt[ -]?out\b",
    r"\bremove\s+(?:me|us|our\s+(?:entire\s+|whole\s+)?(?:company|organization|organisation|team))\b",
    r"\bstop\s+(?:emailing|contacting|sending)\b",
    r"\bdo\s+not\s+(?:email|contact)\b",
    r"\bno\s+further\s+communication\b",
    r"\btake\s+(?:me|us)\s+off\b[^.!?\n]{0,80}\b(?:list|database)\b",
    r"\bcease\s+(?:all\s+)?communication\b",
    r"\bdelete\s+(?:my|our)\s+(?:email\s+address(?:es)?|contact\s+details|details|data)\b",
    r"\bdo[ -]not[ -]contact\s+list\b",
    r"\b(?:do\s+not|don't)\s+(?:email|contact)\b",
    r"\b(?:do\s+not|don't)\s+want\s+to\s+(?:hear\s+from\s+you|receive\s+(?:your\s+)?(?:emails?|messages?))\b",
    r"退订|取消订阅",
    r"(?:不要|别|请勿)再.{0,12}(?:发|发送).{0,6}(?:邮件|信息)",
    r"(?:停止|不要再|请勿再)联系",
    r"从.{0,8}(?:邮件|联系).{0,8}(?:删除|移除)",
)
_ACCOUNT_SCOPE = _patterns(
    r"\b(?:our|the)\s+(?:entire\s+|whole\s+)?(?:company|organization|organisation|team)\b",
    r"\bremove\s+us\b",
    r"\bdo\s+not\s+(?:email|contact)\s+us\b",
    r"\btake\s+us\s+off\b",
    r"\b(?:remove|delete)\s+me\s+and\s+(?:my|our)\b",
    r"\bboth\s+(?:addresses|email\s+addresses|of\s+us)\b",
    r"(?:我们|本|整个)(?:公司|企业|团队|组织)",
    r"我和我(?:的)?(?:同事|助理)",
)
_AUTO_REPLY = _patterns(
    r"\bautomatic\s+reply\b",
    r"\bauto[ -]?reply\b",
    r"\bout\s+of\s+(?:the\s+)?office\b",
    r"\bi\s+(?:am|will\s+be)\s+away\b",
    r"\bmailbox\s+is\s+not\s+monitored\b",
)
_COMPLAINT = _patterns(
    r"\breport(?:ed|ing)?\s+(?:this\s+)?(?:as\s+)?spam\b",
    r"\bspam\s+complaint\b",
)
_REJECTION = _patterns(
    r"\bnot\s+interested\b",
    r"\bwe\s+(?:do\s+not|don't)\s+(?:need|want)\b",
    r"\bno\s+current\s+need\b",
)
_QUOTE = _patterns(
    r"\bplease\s+send\s+(?:a\s+)?(?:quotation|quote)\b",
    r"\bcould\s+you\s+(?:please\s+)?quote\b",
    r"\bwhat(?:'s|\s+is)\s+the\s+price\b",
    r"\bprice\s+for\s+(?:\d+|one|a)\b",
    r"\brequest(?:ing)?\s+(?:a\s+)?(?:quotation|quote)\b",
)
_SAMPLE = _patterns(
    r"\bplease\s+send\s+(?:a\s+)?sample\b",
    r"\bcan\s+(?:we|you)\s+(?:get|send)\s+(?:a\s+)?sample\b",
    r"\bsample\s+(?:request|required)\b",
)
_MATERIALS = _patterns(
    r"\bplease\s+(?:send|share)\s+(?:(?:me|us)\s+)?(?:your\s+)?(?:product\s+)?(?:catalogue|catalog|brochure|datasheet)\b",
    r"\bcan\s+you\s+(?:send|share)\s+(?:(?:me|us)\s+)?(?:your\s+)?(?:product\s+)?(?:catalogue|catalog|brochure|datasheet)\b",
)


class DeterministicReplyClassifier:
    """不确定即拒绝；绝不把礼貌回复或含糊兴趣升级为购买意向。"""

    @property
    def model(self) -> str:
        return "deterministic-reply-v2"

    async def classify(self, *, message: dict[str, str]) -> ReplyClassificationResult:
        if not isinstance(message, dict) or any(
            not isinstance(message.get(name), str) or not message[name].strip()
            for name in ("message_id", "subject", "body")
        ):
            raise ValidationError("回复消息输入无效")
        text = f"{message['subject']}\n{message['body']}"
        if self._matches(_UNSUBSCRIBE, text):
            scope = (
                ReplySuppressScope.ACCOUNT
                if self._matches(_ACCOUNT_SCOPE, text)
                else ReplySuppressScope.CONTACT
            )
            return ReplyClassificationResult(
                ReplyCategory.UNSUBSCRIBE, suppress_scope=scope
            )
        for patterns, category in (
            (_COMPLAINT, ReplyCategory.COMPLAINT),
            (_AUTO_REPLY, ReplyCategory.AUTO_REPLY),
            (_REJECTION, ReplyCategory.REJECTION),
            (_QUOTE, ReplyCategory.REQUESTS_QUOTE),
            (_SAMPLE, ReplyCategory.REQUESTS_SAMPLE),
            (_MATERIALS, ReplyCategory.REQUESTS_MATERIALS),
        ):
            if self._matches(patterns, text):
                return ReplyClassificationResult(category)
        raise ValidationError("回复意向不明确，需人工核对")

    @staticmethod
    def _matches(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
        return any(pattern.search(text) is not None for pattern in patterns)
