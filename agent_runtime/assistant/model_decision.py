"""模型解释只选择当前授权来源；原文与引用由确定性代码生成。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, TypeAdapter, model_validator
from pydantic import ValidationError as SchemaError

from agent_runtime.assistant.context import AssistantContext
from agent_runtime.assistant.proposal import LABELS, collect_fields, missing
from domains.assistant.schemas import (
    AssistantDecision,
    AssistantDTO,
    Clarification,
    Explanation,
    ReadRequest,
    ResearchDraft,
)
from shared.errors import ValidationError


class AssistantOutputError(ValidationError):
    """只携带固定失败分类，禁止把模型正文或校验输入放进审计。"""

    def __init__(self, reason: Literal[
        "output_schema", "output_reference", "output_excerpt", "output_guardrail"
    ]) -> None:
        super().__init__("模型结果未通过验证")
        self.reason = reason


class SourceSelection(AssistantDTO):
    """仅点选本次已授权输入，不允许生成正文、链接或来源身份。"""

    kind: Literal["explain"] = "explain"
    source_indexes: tuple[Annotated[int, Field(strict=True, ge=0)], ...] = Field(
        min_length=1,
        max_length=20,
        description="选择 untrusted_sources 中相关原文的数组下标，从 0 开始，不可重复。",
    )

    @model_validator(mode="after")
    def unique_sources(self) -> SourceSelection:
        if len(set(self.source_indexes)) != len(self.source_indexes):
            raise ValueError("来源选择不得重复")
        return self


class ResearchSelection(AssistantDTO):
    """只表达准备提案的意图；范围、预算和来源不由模型重新抄写。"""

    kind: Literal["research"] = "research"


ModelDecision = Annotated[
    Clarification | ReadRequest | SourceSelection | ResearchSelection,
    Field(discriminator="kind"),
]
_ADAPTER: TypeAdapter[ModelDecision] = TypeAdapter(ModelDecision)


def model_decision_schema() -> dict[str, Any]:
    """只用于模型输入；不修改已持久化的领域结果或 Web DTO。"""
    return _ADAPTER.json_schema()


def model_context(context: AssistantContext) -> dict[str, object]:
    """向模型提供代码从员工原话收集的当前字段；不猜值、不把存在等同于合法。"""
    payload = context.payload()
    fields = collect_fields(context)
    payload["research_input"] = {
        "fields": [field.model_dump(mode="json") for field in fields.values()],
        "missing_fields": sorted(set(LABELS) - fields.keys()),
        "labels": {name: list(labels) for name, labels in LABELS.items()},
    }
    return payload


def parse_model_decision(text: str, context: AssistantContext) -> AssistantDecision:
    """拒绝越界选择；交付前仍须通过原护栏与当前来源重核。"""
    if len(text.encode()) > 65536:
        raise AssistantOutputError("output_schema")
    try:
        decision = _ADAPTER.validate_json(text)
    except SchemaError:
        raise AssistantOutputError("output_schema") from None
    if isinstance(decision, ResearchSelection):
        fields = collect_fields(context)
        return ResearchDraft(fields=tuple(fields.values())) if fields else missing(tuple(LABELS))
    if not isinstance(decision, SourceSelection):
        return decision
    if any(index >= len(context.fragments) for index in decision.source_indexes):
        raise AssistantOutputError("output_reference")
    return Explanation(fragments=tuple(
        context.fragments[index].model_copy(update={
            "source_turn_ids": tuple(turn.turn_id for turn in context.turns),
        })
        for index in decision.source_indexes
    ))
