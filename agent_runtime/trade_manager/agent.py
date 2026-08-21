"""Trade Manager 的老板需求探索指令解析边界。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)
from pydantic import ValidationError as PydanticValidationError

from agent_runtime.base import AgentTask, CapabilityAgent, ChangeSet
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from domains.directives.schemas import (
    DemandDiscoveryPlanInput,
    DiscoverySearchQueryInput,
)
from shared.errors import ValidationError
from shared.schemas.evidence import ConfidenceTier
from shared.schemas.identifiers import ChangeSetId, new_id

_MAX_OUTPUT_BYTES = 65_536
_SYSTEM_PROMPT = """你是 TradeOS 的老板指令解析器。当前只解析 Phase 1 的需求探索指令。
只输出 JSON 对象，禁止 Markdown 和额外文字。顶层键必须精确为
interpretation_summary、expected_behavior_changes、plan、no_auto_send。

plan 必须精确包含：objective、queries、target_countries、target_categories、
excluded_countries、excluded_categories、max_search_queries、max_pages_read、
max_signals、max_hypotheses、minimum_confidence_tier、strategy_group、campaign_id、
role_hints、assessment_ref。queries 每项只含 query、country、category、limit。

所有预算、国家、品类、Campaign、置信档位门槛都必须来自用户原话；不得猜测或
填默认值。信息不完整时不要编造字段。国家使用大写 ISO 两位代码；
minimum_confidence_tier 只能是 low、low_mid、mid、mid_high、high、very_high、
extreme。no_auto_send 必须是 true。不得输出凭证、联系人、价格、概率、发送动作
或未授权字段。"""


class _QueryPayload(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    query: str = Field(min_length=1, max_length=400)
    country: str = Field(pattern=r"^[A-Z]{2}$")
    category: str = Field(min_length=1, max_length=100)
    limit: StrictInt = Field(ge=1, le=20)

    @field_validator("query", "category")
    @classmethod
    def exact_text(cls, value: str) -> str:
        if value != value.strip() or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("invalid text")
        if len(value.split()) > 50:
            raise ValueError("too many words")
        return value


class _PlanPayload(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    objective: str = Field(min_length=1, max_length=1_000)
    queries: tuple[_QueryPayload, ...] = Field(min_length=1, max_length=100)
    target_countries: tuple[str, ...] = Field(min_length=1, max_length=100)
    target_categories: tuple[str, ...] = Field(min_length=1, max_length=100)
    excluded_countries: tuple[str, ...] = Field(max_length=100)
    excluded_categories: tuple[str, ...] = Field(max_length=100)
    max_search_queries: StrictInt = Field(ge=1, le=100)
    max_pages_read: StrictInt = Field(ge=1, le=50)
    max_signals: StrictInt = Field(ge=1, le=100)
    max_hypotheses: StrictInt = Field(ge=1, le=100)
    minimum_confidence_tier: ConfidenceTier
    strategy_group: str = Field(min_length=1, max_length=64)
    campaign_id: str = Field(pattern=r"^cmp_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    role_hints: tuple[str, ...] = Field(max_length=10)
    assessment_ref: str = Field(min_length=1, max_length=200)

    @field_validator(
        "objective",
        "strategy_group",
        "campaign_id",
        "assessment_ref",
    )
    @classmethod
    def exact_text(cls, value: str) -> str:
        if value != value.strip() or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValueError("invalid text")
        return value

    @field_validator("target_countries", "excluded_countries")
    @classmethod
    def countries(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            len(value) != 2
            or not value.isascii()
            or not value.isupper()
            or not value.isalpha()
            for value in values
        ):
            raise ValueError("invalid countries")
        return values

    @field_validator(
        "target_categories",
        "excluded_categories",
        "role_hints",
    )
    @classmethod
    def text_lists(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            not value
            or value != value.strip()
            or len(value) > 200
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in value
            )
            for value in values
        ):
            raise ValueError("invalid text list")
        return values

    @model_validator(mode="after")
    def coherent(self) -> _PlanPayload:
        if (
            len(self.queries) > self.max_search_queries
            or set(self.target_countries) & set(self.excluded_countries)
            or set(self.target_categories) & set(self.excluded_categories)
            or any(
                query.country not in self.target_countries
                or query.category not in self.target_categories
                for query in self.queries
            )
        ):
            raise ValueError("incoherent discovery plan")
        identities = {
            (query.query, query.country, query.category) for query in self.queries
        }
        if len(identities) != len(self.queries):
            raise ValueError("duplicate query")
        return self


class _ProposalPayload(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    interpretation_summary: str = Field(min_length=1, max_length=4_000)
    expected_behavior_changes: tuple[str, ...] = Field(min_length=1, max_length=50)
    plan: _PlanPayload
    no_auto_send: StrictBool

    @field_validator("interpretation_summary")
    @classmethod
    def summary_text(cls, value: str) -> str:
        if value != value.strip():
            raise ValueError("invalid summary")
        return value

    @field_validator("expected_behavior_changes")
    @classmethod
    def changes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)) or any(
            not value or value != value.strip() or len(value) > 1_000
            for value in values
        ):
            raise ValueError("invalid changes")
        return values


@runtime_checkable
class TradeManagerModelPort(Protocol):
    async def parse_discovery_directive(
        self,
        *,
        system_prompt: str,
        message: str,
    ) -> str: ...


@dataclass(frozen=True)
class DiscoveryProposalDraft:
    plan: DemandDiscoveryPlanInput
    interpretation_summary: str
    expected_behavior_changes: tuple[str, ...]


class TradeManagerAgent(CapabilityAgent):
    name = "trade_manager"

    def __init__(
        self,
        model: str,
        model_client: Any,
        gateway: Any,
        guardrails: Any,
    ) -> None:
        if (
            not isinstance(model, str)
            or not model
            or model != model.strip()
            or not isinstance(model_client, TradeManagerModelPort)
            or not isinstance(guardrails, CredentialMarkerGuard)
        ):
            raise ValidationError("Trade Manager 配置无效")
        self._model = model
        self._model_port = model_client
        self._gateway = gateway
        self._input_guard = guardrails

    @property
    def model(self) -> str:
        return self._model

    async def propose_discovery(self, message: str) -> DiscoveryProposalDraft:
        if (
            not isinstance(message, str)
            or not message
            or message != message.strip()
            or len(message) > 10_000
        ):
            raise ValidationError("老板指令原话无效")
        self._input_guard.check(subject="老板需求探索指令", body=message)
        raw = await self._model_port.parse_discovery_directive(
            system_prompt=_SYSTEM_PROMPT,
            message=message,
        )
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_OUTPUT_BYTES:
            raise ValidationError("Trade Manager 输出无效")
        try:
            payload = _ProposalPayload.model_validate_json(raw, strict=True)
        except PydanticValidationError:
            raise ValidationError("Trade Manager 输出不满足需求探索契约") from None
        if payload.no_auto_send is not True:
            raise ValidationError("Trade Manager 试图产生自动外发动作")
        plan = payload.plan
        return DiscoveryProposalDraft(
            plan=DemandDiscoveryPlanInput(
                objective=plan.objective,
                queries=tuple(
                    DiscoverySearchQueryInput(
                        query=item.query,
                        country=item.country,
                        category=item.category,
                        limit=item.limit,
                    )
                    for item in plan.queries
                ),
                target_countries=plan.target_countries,
                target_categories=plan.target_categories,
                excluded_countries=plan.excluded_countries,
                excluded_categories=plan.excluded_categories,
                max_search_queries=plan.max_search_queries,
                max_pages_read=plan.max_pages_read,
                max_signals=plan.max_signals,
                max_hypotheses=plan.max_hypotheses,
                minimum_confidence_tier=plan.minimum_confidence_tier.value,
                strategy_group=plan.strategy_group,
                campaign_id=plan.campaign_id,
                role_hints=plan.role_hints,
                assessment_ref=plan.assessment_ref,
            ),
            interpretation_summary=payload.interpretation_summary,
            expected_behavior_changes=payload.expected_behavior_changes,
        )

    async def run(self, task: AgentTask, context: object) -> ChangeSet:
        del context
        if not isinstance(task, AgentTask) or set(task.inputs) != {"message"}:
            raise ValidationError("Trade Manager 任务无效")
        message = task.inputs["message"]
        if not isinstance(message, str):
            raise ValidationError("Trade Manager 任务无效")
        draft = await self.propose_discovery(message)
        plan = draft.plan
        plan_payload = {
            "objective": plan.objective,
            "queries": [vars(item) for item in plan.queries],
            "target_countries": list(plan.target_countries),
            "target_categories": list(plan.target_categories),
            "excluded_countries": list(plan.excluded_countries),
            "excluded_categories": list(plan.excluded_categories),
            "max_search_queries": plan.max_search_queries,
            "max_pages_read": plan.max_pages_read,
            "max_signals": plan.max_signals,
            "max_hypotheses": plan.max_hypotheses,
            "minimum_confidence_tier": plan.minimum_confidence_tier,
            "strategy_group": plan.strategy_group,
            "campaign_id": plan.campaign_id,
            "role_hints": list(plan.role_hints),
            "assessment_ref": plan.assessment_ref,
        }
        return ChangeSet(
            change_set_id=ChangeSetId(new_id("cs")),
            tenant_id=task.tenant_id,
            run_id=task.run_id,
            changes=[
                {
                    "domain": "directives",
                    "operation": "submit_proposal",
                    "risk_level": "medium",
                    "payload": {
                        "raw_text": message,
                        "interpretation_summary": draft.interpretation_summary,
                        "expected_behavior_changes": list(
                            draft.expected_behavior_changes
                        ),
                        "demand_discovery": plan_payload,
                    },
                }
            ],
            summary="需求探索指令已解析为待老板确认的提案",
        )


__all__ = (
    "DiscoveryProposalDraft",
    "TradeManagerAgent",
    "TradeManagerModelPort",
)
