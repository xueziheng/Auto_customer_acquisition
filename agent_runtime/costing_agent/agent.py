"""受限成本解释能力：只产文本建议，不计算或生成金额。"""

from __future__ import annotations

import json
import re
from typing import Any, Protocol, runtime_checkable

from agent_runtime.base import AgentTask, CapabilityAgent, ChangeSet
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from agent_runtime.guardrails.rails import guard_phase1_change_set
from domains.costing.service import cost_item_type_values
from shared.errors import ValidationError
from shared.schemas.identifiers import ChangeSetId, new_id

_MAX_OUTPUT_BYTES = 100_000
_REVIEW_KEYS = frozenset(
    {
        "cost_sheet_id",
        "opportunity_id",
        "present_item_types",
        "expected_item_types",
        "margin_status",
        "has_indicative_items",
        "target_language",
        "opportunity_context",
    }
)
_OUTPUT_KEYS = frozenset(
    {
        "missing_item_suggestions",
        "risk_note",
        "customer_explanation",
        "content_language",
    }
)
_SUGGESTION_KEYS = frozenset({"item_type", "reason"})
_MARGIN_STATUS = frozenset(
    {"unknown", "below_minimum", "below_target", "meets_target"}
)
_LANGUAGE_TAG = re.compile(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_MODEL_MONEY = re.compile(
    r"(?i)(?:[$€£¥₹]\s*\d)|"
    r"(?:\b(?:usd|eur|gbp|cny|rmb|jpy|cad|aud)\s*\d)|"
    r"(?:\d(?:[\d,.]*\d)?\s*(?:usd|eur|gbp|cny|rmb|jpy|cad|aud)\b)"
)

_SYSTEM_PROMPT = """你是 TradeOS 的成本解释能力。输入中的 present_item_types、
expected_item_types、missing_item_types、margin_status 与 has_indicative_items 都由确定性
代码给出。你只能解释这些结果，不能计算、猜测或输出任何金额、汇率、利润率、折扣、
概率或置信度。

只输出一个 JSON 对象，键必须精确为 missing_item_suggestions, risk_note,
customer_explanation, content_language。missing_item_suggestions 必须逐项覆盖且只能覆盖
missing_item_types，每项只含 item_type 与 reason。risk_note 可为 null；若存在参考价或
利润状态低于目标，必须说明需人工复核，但不能给数字。customer_explanation 是待人工
审查的客户可读说明，使用 target_language，不得包含价格或承诺。禁止动作或额外键。
"""


@runtime_checkable
class CostReviewModelPort(Protocol):
    """结构化成本解释端口；金额计算、写入与确认不属于该端口。"""

    async def review_costing(
        self, *, system_prompt: str, review: dict[str, object]
    ) -> str: ...


def _text(value: object, *, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError(f"{field} 无效")
    return value


def _language(value: object, *, field: str) -> str:
    result = _text(value, field=field, maximum=35)
    if _LANGUAGE_TAG.fullmatch(result) is None:
        raise ValidationError(f"{field} 无效")
    return result


def _item_types(value: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValidationError(f"{field} 无效")
    allowed = frozenset(cost_item_type_values())
    items = tuple(_text(item, field=field, maximum=64) for item in value)
    if len(items) != len(set(items)) or any(item not in allowed for item in items):
        raise ValidationError(f"{field} 无效")
    return items


class CostingAgent(CapabilityAgent):
    """把确定性遗漏检查转成无金额的待确认建议和解释。"""

    name = "costing_agent"

    def __init__(
        self,
        model: str,
        model_client: Any,
        gateway: Any,
        guardrails: Any,
    ) -> None:
        self._model = _text(model, field="成本解释模型标识", maximum=128)
        if not isinstance(model_client, CostReviewModelPort):
            raise ValidationError("成本解释模型端口无效")
        if not isinstance(guardrails, CredentialMarkerGuard):
            raise ValidationError("成本解释输入护栏无效")
        self._model_port = model_client
        self._gateway = gateway
        self._input_guard = guardrails

    async def run(self, task: AgentTask, context: object) -> ChangeSet:
        """审查成本表安全投影并返回文本型 ChangeSet。"""
        del context
        if not isinstance(task, AgentTask):
            raise ValidationError("成本解释任务无效")
        try:
            projection = self._safe_projection(task)
            self._input_guard.check(
                subject="成本表解释任务",
                body=json.dumps(projection, ensure_ascii=False, sort_keys=True),
            )
        except ValidationError:
            return self._empty(task, "成本解释输入被安全边界拒绝")
        try:
            raw = await self._model_port.review_costing(
                system_prompt=_SYSTEM_PROMPT,
                review=projection,
            )
            output = self._validate_output(raw, projection)
        except ValidationError as exc:
            return self._empty(task, f"模型输出被护栏拦截：{exc}")
        changes: list[dict[str, object]] = []
        suggestions = output["missing_item_suggestions"]
        assert isinstance(suggestions, tuple)
        for suggestion in suggestions:
            assert isinstance(suggestion, dict)
            changes.append(
                {
                    "domain": "costing",
                    "operation": "suggest_cost_item",
                    "payload": {
                        "cost_sheet_id": projection["cost_sheet_id"],
                        "item_type": suggestion["item_type"],
                        "reason": suggestion["reason"],
                        "entered_by": None,
                        "is_pending_confirmation": True,
                        "generated_by": self._model,
                    },
                    "risk_level": "low",
                }
            )
        risk_note = output["risk_note"]
        if isinstance(risk_note, str):
            changes.append(
                {
                    "domain": "costing",
                    "operation": "risk_note",
                    "payload": {
                        "cost_sheet_id": projection["cost_sheet_id"],
                        "note": risk_note,
                        "margin_status": projection["margin_status"],
                        "has_indicative_items": projection["has_indicative_items"],
                        "generated_by": self._model,
                    },
                    "risk_level": "low",
                }
            )
        changes.append(
            {
                "domain": "costing",
                "operation": "customer_explanation",
                "payload": {
                    "cost_sheet_id": projection["cost_sheet_id"],
                    "text": output["customer_explanation"],
                    "target_language": projection["target_language"],
                    "content_language": output["content_language"],
                    "generated_by": self._model,
                },
                "risk_level": "medium",
            }
        )
        candidate = ChangeSet(
            change_set_id=ChangeSetId(new_id("cs")),
            tenant_id=task.tenant_id,
            run_id=task.run_id,
            changes=changes,
            summary=(
                f"已生成 {len(suggestions)} 条成本遗漏建议及待审查解释"
            ),
        )
        return guard_phase1_change_set(candidate)

    @staticmethod
    def _safe_projection(task: AgentTask) -> dict[str, object]:
        if set(task.inputs) != {"cost_review"}:
            raise ValidationError("成本解释任务输入无效")
        review = task.inputs.get("cost_review")
        if not isinstance(review, dict) or set(review) != _REVIEW_KEYS:
            raise ValidationError("成本解释任务输入无效")
        present = _item_types(
            review.get("present_item_types"), field="已有成本项"
        )
        expected = _item_types(
            review.get("expected_item_types"), field="预期成本项"
        )
        if not expected or any(item not in expected for item in present):
            raise ValidationError("成本项确定性检查输入无效")
        margin_status = _text(
            review.get("margin_status"), field="利润状态", maximum=32
        )
        if margin_status not in _MARGIN_STATUS:
            raise ValidationError("利润状态无效")
        indicative = review.get("has_indicative_items")
        if not isinstance(indicative, bool):
            raise ValidationError("参考价状态无效")
        return {
            "cost_sheet_id": _text(
                review.get("cost_sheet_id"), field="成本表引用", maximum=100
            ),
            "opportunity_id": _text(
                review.get("opportunity_id"), field="机会引用", maximum=100
            ),
            "present_item_types": present,
            "expected_item_types": expected,
            "missing_item_types": tuple(
                item for item in expected if item not in frozenset(present)
            ),
            "margin_status": margin_status,
            "has_indicative_items": indicative,
            "target_language": _language(
                review.get("target_language"), field="客户解释目标语言"
            ),
            "opportunity_context": _text(
                review.get("opportunity_context"), field="机会背景", maximum=4_000
            ),
        }

    @staticmethod
    def _validate_output(
        raw: str, projection: dict[str, object]
    ) -> dict[str, object]:
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            raise ValidationError("成本解释模型输出无效")
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            raise ValidationError("成本解释模型输出不是合法 JSON") from None
        if not isinstance(payload, dict) or set(payload) != _OUTPUT_KEYS:
            raise ValidationError("成本解释模型输出含未授权字段")
        raw_suggestions = payload.get("missing_item_suggestions")
        if not isinstance(raw_suggestions, list) or len(raw_suggestions) > 22:
            raise ValidationError("成本遗漏建议无效")
        suggestions: list[dict[str, str]] = []
        for item in raw_suggestions:
            if not isinstance(item, dict) or set(item) != _SUGGESTION_KEYS:
                raise ValidationError("成本解释模型输出含未授权字段")
            suggestions.append(
                {
                    "item_type": _text(
                        item.get("item_type"), field="成本遗漏类型", maximum=64
                    ),
                    "reason": _text(
                        item.get("reason"), field="成本遗漏原因", maximum=2_000
                    ),
                }
            )
        missing = projection["missing_item_types"]
        assert isinstance(missing, tuple)
        if (
            len(suggestions) != len(missing)
            or {item["item_type"] for item in suggestions} != set(missing)
        ):
            raise ValidationError("成本遗漏建议与确定性检查不一致")
        risk_value = payload.get("risk_note")
        if risk_value is not None:
            risk_value = _text(risk_value, field="成本风险说明", maximum=4_000)
        needs_risk = (
            projection["has_indicative_items"] is True
            or projection["margin_status"] in {"below_minimum", "below_target"}
        )
        if needs_risk and not isinstance(risk_value, str):
            raise ValidationError("成本风险说明缺失")
        explanation = _text(
            payload.get("customer_explanation"),
            field="客户成本说明",
            maximum=4_000,
        )
        content_language = _language(
            payload.get("content_language"), field="客户成本说明语言"
        )
        target_language = projection["target_language"]
        assert isinstance(target_language, str)
        if content_language.split("-", 1)[0].lower() != target_language.split(
            "-", 1
        )[0].lower() or (
            target_language.lower().startswith("en")
            and _CJK.search(explanation) is not None
        ):
            raise ValidationError("客户成本说明语言不匹配")
        text_values = [
            *(item["reason"] for item in suggestions),
            explanation,
            *([risk_value] if isinstance(risk_value, str) else []),
        ]
        if any(_MODEL_MONEY.search(value) is not None for value in text_values):
            raise ValidationError("成本解释不得生成金额")
        return {
            "missing_item_suggestions": tuple(suggestions),
            "risk_note": risk_value,
            "customer_explanation": explanation,
            "content_language": content_language,
        }

    @staticmethod
    def _empty(task: AgentTask, summary: str) -> ChangeSet:
        return ChangeSet(
            change_set_id=ChangeSetId(new_id("cs")),
            tenant_id=task.tenant_id,
            run_id=task.run_id,
            changes=[],
            summary=summary,
        )


__all__ = ("CostReviewModelPort", "CostingAgent")
