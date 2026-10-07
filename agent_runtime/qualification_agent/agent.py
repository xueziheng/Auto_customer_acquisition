"""QualificationAgent —— 回复分类与候选提取的最小边界（评估基线第一步）。

分工（agent_runtime/AGENTS.md）：
- **模型只产出受约束输出**：14 类枚举值、候选提取（field/value/quote）与
  退订专用 typed ``suppress_scope``。任何动作、置信度（硬边界 3）、
  副作用键一律护栏拦截；明确组织级退订由确定性安全规则只升级不降级。
- **确定性动作由域 ``REPLY_ACTIONS`` 决定**（本任务不派生、不执行；由
  workflows/reply_qualification 的 apply_actions 后续消费）。
- **本能力不落库、不执行动作**：只产出 typed ChangeSet（record_classification /
  update_need_fields），应用与动作执行是 workflow 的事。
- 凭证不进模型：port 只收到系统提示与消息正文。

结构化模型调用抽象为可注入 ``ReplyModelPort``；评估运行器依赖更窄的
``ReplyClassifier`` 端口。生产实现（真实 LLM provider）在后续任务接入。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from agent_runtime.base import AgentTask, CapabilityAgent, ChangeSet
from domains.conversations.schemas import (
    MAX_REPLY_FIELD_QUOTE_CODEPOINTS,
    ReplyCategory,
    ReplySuppressScope,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import ChangeSetId, new_id

#: 需求字段词表：与 domains/demand ValidatedNeed 的 FactualField 业务字段一致。
NEED_FIELD_NAMES = frozenset(
    {
        "product_category",
        "application",
        "material",
        "size_spec",
        "quantity",
        "packaging",
        "destination",
        "required_by",
        "target_price",
        "current_supply_issue",
        "certification_required",
    }
)

_ALLOWED_OUTPUT_KEYS = frozenset({"category", "candidate_fields", "suppress_scope"})
_CANDIDATE_KEYS = frozenset({"field", "value", "quote"})

#: 模型输出大小上限（64 KiB）：在 json.loads 之前快速失败，防超大 payload 解析。
_MAX_MODEL_OUTPUT_BYTES = 65_536

_ACCOUNT_UNSUBSCRIBE_PATTERNS = tuple(
    re.compile(rf"(?:(?:please|kindly)\s+)?(?:{pattern})[.!]?", re.IGNORECASE)
    for pattern in (
        r"(?:remove|unsubscribe)\s+(?:our|my)\s+(?:(?:entire|whole)\s+)?(?:company|organization|organisation|team)\s+from\s+(?:your|the)\s+(?:(?:mailing|contact|distribution)\s+)?(?:list|database|emails?)",
        r"(?:remove|take)\s+us\s+(?:from|off)\s+(?:your|the)\s+(?:(?:mailing|contact|distribution)\s+)?(?:list|database|emails?)",
        r"remove\s+me\s+and\s+(?:my|our)\s+(?:assistant|colleagues?|team)\s+from\s+(?:your|the)\s+(?:(?:mailing|contact|distribution)\s+)?list",
        r"(?:remove|unsubscribe)\s+both\s+(?:addresses|email\s+addresses|of\s+us)\s+from\s+(?:your|the)\s+(?:(?:mailing|contact|distribution)\s+)?list",
    )
)
_EXPLICIT_UNSUBSCRIBE_PATTERNS = tuple(
    re.compile(rf"(?:(?:please|kindly)\s+)?(?:{pattern})[.!]?", re.IGNORECASE)
    for pattern in (
        r"unsubscribe\s+(?:me|us)(?:\s+from\s+(?:your|the)\s+(?:emails?|(?:mailing|contact|distribution)\s+list))?",
        r"(?:remove|take)\s+(?:me|us)\s+(?:from|off)\s+(?:your|the)\s+(?:(?:mailing|contact|distribution)\s+)?(?:list|database|emails?)",
        r"stop\s+(?:contacting\s+(?:me|us)|sending\s+(?:(?:me|us)\s+)?(?:emails?|messages?))",
        r"do\s+not\s+contact\s+(?:me|us)(?:\s+again)?",
        r"no\s+further\s+communication",
    )
)

_SYSTEM_PROMPT = r"""你是 TradeOS 的回复分类与需求提取模型。输入是客户对英文开发信的回复（subject 与 body）。
只输出一个 JSON 对象，禁止输出任何其他文字、Markdown 或代码块。禁止输出任何数值置信度或概率。
category 必须是下列 14 个枚举值之一（值必须完全一致）：
clear_interest 明确兴趣；willing_to_continue 愿意继续聊；requests_materials 要求发资料；
requests_quote 要求报价；requests_sample 要求样品；provides_specification 提供规格；
no_current_need 无当前需求；future_need_possible 以后可能有需求；refers_other_contact 介绍其他联系人；
rejection 拒绝；unsubscribe 退订；bounce 退信；auto_reply 自动回复；complaint 投诉。
candidate_fields 为可选数组，每项 {field, value, quote}：
- field 必须是：product_category, application, material, size_spec, quantity, packaging,
  destination, required_by, target_price, current_supply_issue, certification_required
- quote 必须是输入消息中逐字出现、且不超过 500 个 Unicode code point 的摘录
  （证明字段来自客户原话；禁止返回整段正文）
- value 始终是字符串；规范化 value 的格式时，quote 必须保留原文，不改写单位、币种或措辞。
- product_category 只填产品品类名称；客户明确提到的用途单独放 application，
  不把 "for ..." 用途拼进 product_category；不得创造客户未提及的产品或用途。
- quantity 的 value 只填无单位的十进制整数字符串，不含千分位分隔符、小数或指数。
  单位仍保留在 quote 中，不新增 unit 字段。数量为范围、约数或不能唯一确定时不提取该字段，
  不取范围端点、不四舍五入，也不把包装数量换算成产品数量。
- target_price 的 value 必须是一个编码后的 JSON 对象字符串，且仅有 amount、currency 两键：
  amount 是不含货币符号的十进制字符串；currency 是客户明确给出的三位大写币种代码。
  不得把整段自然语言价格当 value，也不得把该对象直接作为 value；在外层 JSON 中转义内部引号。
  只抄录客户明确给出的目标价，不计算、换汇或由总价推算单价；币种不明时不猜测（如单独的 $）。
  这仍是客户目标价，不是供应商 quoted 价格或可发送的正式报价。
- required_by 仅在客户明确给出完整日期时使用 YYYY-MM-DD；不猜缺失年份或相对日期。
缺少事实或格式不能无歧义表达时省略该候选，不输出空值或编造内容；quote 不能用规范化后的 value 替代。
完整格式示例输入（仅示范格式，不是当前客户事实）：
{"subject":"Purchase request","body":"We need 120 valves for irrigation at EUR 4.25 per piece."}
对应输出：
{"category":"provides_specification","candidate_fields":[{"field":"product_category","value":"valves","quote":"valves"},{"field":"application","value":"irrigation","quote":"for irrigation"},{"field":"quantity","value":"120","quote":"120 valves"},{"field":"target_price","value":"{\"amount\":\"4.25\",\"currency\":\"EUR\"}","quote":"EUR 4.25 per piece"}]}
仅 category=unsubscribe 时可输出 suppress_scope，值只能是 contact 或 account；
客户明确要求停止联系整个公司、组织、团队或多名收件人时必须是 account，否则是 contact。
输出示例：{"category": "clear_interest", "candidate_fields": []}
"""


@runtime_checkable
class ReplyModelPort(Protocol):
    """结构化模型调用端口（计量/超时/凭证在实现侧；生产 provider 后续接入）。"""

    async def classify_reply(
        self, *, system_prompt: str, message: dict[str, str]
    ) -> str:
        """返回模型输出的原始文本；失败抛 TransientError 由调用方重试。"""
        ...


@dataclass(frozen=True)
class ReplyFieldCandidate:
    """受约束的候选提取：quote 必须逐字来自原消息（provenance）。"""

    field: str
    value: str
    quote: str


@dataclass(frozen=True)
class ReplyClassificationResult:
    """分类器的 typed 输出边界：枚举类别 + 已验证候选。无任何置信度字段。"""

    category: ReplyCategory
    candidate_fields: tuple[ReplyFieldCandidate, ...] = ()
    suppress_scope: ReplySuppressScope | None = None
    rejected_candidates: bool = False


@runtime_checkable
class ReplyClassifier(Protocol):
    """评估运行器依赖的最窄分类器端口（agent 是其实现之一）。"""

    @property
    def model(self) -> str:
        """模型标识（classified_by 留痕用；评估报告按 provider 区分）。"""
        ...

    async def classify(
        self, *, message: dict[str, str]
    ) -> ReplyClassificationResult: ...


class QualificationAgent(CapabilityAgent):
    """回复分类能力：验证受限输出 → 产出 typed ChangeSet。"""

    name = "qualification_agent"

    @property
    def model(self) -> str:
        """模型标识（评估报告按 provider 区分 smoke/真实模型）。"""
        return self._model

    def __init__(
        self,
        model: str,
        model_client: Any,
        gateway: Any,
        guardrails: Any,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValidationError("qualification agent 模型标识无效")
        if not isinstance(model_client, ReplyModelPort):
            raise ValidationError("qualification agent 模型端口无效")
        self._model = model
        self._model_port = model_client
        self._gateway = gateway
        self._guardrails = guardrails

    # ---- 分类边界 ----------------------------------------------------------

    async def classify(self, *, message: dict[str, str]) -> ReplyClassificationResult:
        """验证受限模型输出并返回 typed 结果；无效输出抛 ValidationError（fail closed）。

        port 只收到 subject/body——message_id 等 identifier 绝不进模型调用。
        """
        self._validate_message(message)
        port_message = {
            "subject": message["subject"],
            "body": message["body"],
        }
        raw = await self._model_port.classify_reply(
            system_prompt=_SYSTEM_PROMPT, message=port_message
        )
        return self._validate_model_output(raw, port_message)

    @staticmethod
    def _validate_message(message: object) -> dict[str, str]:
        if not isinstance(message, dict):
            raise ValidationError("回复消息输入无效")
        subject = message.get("subject")
        body = message.get("body")
        message_id = message.get("message_id")
        if (
            not isinstance(subject, str)
            or not subject.strip()
            or not isinstance(body, str)
            or not body.strip()
            or not isinstance(message_id, str)
            or not message_id.strip()
        ):
            raise ValidationError("回复消息输入无效")
        return message

    @classmethod
    def _validate_model_output(
        cls, raw: str, message: dict[str, str]
    ) -> ReplyClassificationResult:
        """受限输出校验：键白名单（无动作/置信度）、枚举类别、候选词表与 quote。"""
        if not isinstance(raw, str):
            # port 返回非 str（不守契约实现）：在任何 encode/len/json.loads 之前
            # 类型校验失败，run 捕获后返回可审计空 ChangeSet，不泄漏异常
            raise ValidationError("模型输出类型无效")
        if len(raw.encode("utf-8")) > _MAX_MODEL_OUTPUT_BYTES:
            raise ValidationError("模型输出超过大小上限")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise ValidationError("模型输出不是合法 JSON") from None
        if not isinstance(payload, dict):
            raise ValidationError("模型输出必须是 JSON 对象")
        unknown = set(payload) - _ALLOWED_OUTPUT_KEYS
        if unknown:
            raise ValidationError(f"模型输出含未授权键：{sorted(unknown)}")
        raw_category = payload.get("category")
        try:
            category = ReplyCategory(raw_category)
        except (TypeError, ValueError):
            raise ValidationError("模型输出了未知类别") from None
        # 完整的当前请求句才可覆盖；提及、否定和条件说明不是退订请求。
        current_sentences = tuple(
            sentence.strip()
            for sentence in re.split(r"(?<=[.!?])\s+|[\r\n]+", message.get("body", ""))
            if sentence.strip()
        )
        direct_request = any(
            pattern.fullmatch(sentence)
            for pattern in (
                *_EXPLICIT_UNSUBSCRIBE_PATTERNS,
                *_ACCOUNT_UNSUBSCRIBE_PATTERNS,
            )
            for sentence in current_sentences
        )
        bare_request = (
            re.fullmatch(
                r"(?:(?:please|kindly)\s+)?unsubscribe[.!]?",
                message.get("body", "").strip(),
                re.IGNORECASE,
            )
            is not None
        )
        if category is not ReplyCategory.COMPLAINT and (direct_request or bare_request):
            category = ReplyCategory.UNSUBSCRIBE
        raw_scope = payload.get("suppress_scope")
        if category is ReplyCategory.UNSUBSCRIBE:
            if raw_scope is None:
                suppress_scope = ReplySuppressScope.CONTACT
            else:
                try:
                    suppress_scope = ReplySuppressScope(raw_scope)
                except (TypeError, ValueError):
                    raise ValidationError("模型输出退订抑制范围无效") from None
            if any(
                pattern.fullmatch(sentence)
                for pattern in _ACCOUNT_UNSUBSCRIBE_PATTERNS
                for sentence in current_sentences
            ):
                suppress_scope = ReplySuppressScope.ACCOUNT
        else:
            if raw_scope is not None:
                raise ValidationError("非退订分类不得携带抑制范围")
            suppress_scope = None
        raw_candidates = payload.get("candidate_fields", [])
        if not isinstance(raw_candidates, list):
            raise ValidationError("模型输出 candidate_fields 必须是数组")
        candidates: list[ReplyFieldCandidate] = []
        rejected_candidates = False
        seen_fields: dict[str, tuple[str, str]] = {}
        for item in raw_candidates:
            if not isinstance(item, dict):
                raise ValidationError("模型输出候选字段条目无效")
            unknown_keys = set(item) - _CANDIDATE_KEYS
            if unknown_keys:
                raise ValidationError(f"模型输出候选含未授权键：{sorted(unknown_keys)}")
            field = item.get("field")
            value = item.get("value")
            quote = item.get("quote")
            if isinstance(quote, str) and len(quote) > MAX_REPLY_FIELD_QUOTE_CODEPOINTS:
                raise ValidationError("模型输出候选逐字证据超长")
            if (
                not isinstance(field, str)
                or field not in NEED_FIELD_NAMES
                or not isinstance(value, str)
                or not value.strip()
                or not isinstance(quote, str)
                or not quote.strip()
            ):
                # 词表外/缺值候选被拒；workflow不得将其静默当成缺项。
                rejected_candidates = True
                continue
            haystacks = (
                str(message.get("subject", "")),
                str(message.get("body", "")),
            )
            if not any(quote in haystack for haystack in haystacks):
                # quote不在输入中；仅安全信号交给workflow，无原文/原因。
                rejected_candidates = True
                continue
            previous = seen_fields.get(field)
            if previous is not None:
                if previous == (value, quote):
                    # 完全相同候选：确定性去重
                    continue
                # 同 field 但 value/quote 不同：整份输出拒绝（无法确定取哪个）
                raise ValidationError(f"模型输出候选字段冲突：{field}")
            seen_fields[field] = (value, quote)
            candidates.append(
                ReplyFieldCandidate(field=field, value=value, quote=quote)
            )
        return ReplyClassificationResult(
            category=category,
            candidate_fields=tuple(candidates),
            suppress_scope=suppress_scope,
            rejected_candidates=rejected_candidates,
        )

    # ---- ChangeSet ---------------------------------------------------------

    async def run(self, task: AgentTask, context: Any) -> ChangeSet:
        """读上下文 → 分类（护栏校验）→ 产出 ChangeSet；无效模型输出返回空 ChangeSet。"""
        del context
        if not isinstance(task, AgentTask):
            raise ValidationError("qualification agent 任务无效")
        raw_message = (task.inputs or {}).get("message")
        try:
            message = self._validate_message(raw_message)
        except ValidationError:
            # 消息输入校验失败：固定摘要（与模型输出护栏失败区分）
            return self._input_rejected_changeset(task)
        try:
            result = await self.classify(message=message)
        except ValidationError as exc:
            # 模型输出护栏拦截：不可重试 → 带解释的空 ChangeSet（base 契约）
            return self._intercepted_changeset(task, str(exc))
        changes: list[dict[str, Any]] = [
            {
                "domain": "conversations",
                "operation": "record_classification",
                "payload": {
                    "message_id": message["message_id"],
                    "category": result.category.value,
                    "model_version": self._model,
                },
                "risk_level": "low",
            }
        ]
        if result.suppress_scope is not None:
            changes[0]["payload"]["suppress_scope"] = result.suppress_scope.value
        if result.candidate_fields:
            changes.append(
                {
                    "domain": "demand",
                    "operation": "update_need_fields",
                    "payload": {
                        "message_id": message["message_id"],
                        "fields": [
                            {
                                "field": candidate.field,
                                "value": candidate.value,
                                "quote": candidate.quote,
                            }
                            for candidate in result.candidate_fields
                        ],
                    },
                    "risk_level": "low",
                }
            )
        return ChangeSet(
            change_set_id=ChangeSetId(new_id("cs")),
            tenant_id=task.tenant_id,
            run_id=task.run_id,
            changes=changes,
            summary=f"回复分类：{result.category.value}",
        )

    @staticmethod
    def _intercepted_changeset(task: AgentTask, reason: str) -> ChangeSet:
        return ChangeSet(
            change_set_id=ChangeSetId(new_id("cs")),
            tenant_id=task.tenant_id,
            run_id=task.run_id,
            changes=[],
            summary=f"模型输出被护栏拦截：{reason}",
        )

    @staticmethod
    def _input_rejected_changeset(task: AgentTask) -> ChangeSet:
        return ChangeSet(
            change_set_id=ChangeSetId(new_id("cs")),
            tenant_id=task.tenant_id,
            run_id=task.run_id,
            changes=[],
            summary="任务输入无效",
        )
