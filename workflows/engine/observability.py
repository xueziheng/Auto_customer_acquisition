"""观测契约：记录计数、缺项与业务资格分开，不创建计费事实。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from shared.schemas.money import Money


class ObservationModel(BaseModel):
    """只读且拒绝隐式类型转换的安全投影。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


ObservationStage = Literal[
    "demand_signal",
    "need_hypothesis",
    "validated_need",
    "opportunity_record",
    "supply_match",
    "quote_record",
    "human_execution",
    "deal_outcome",
]


class StageObservation(ObservationModel):
    stage: ObservationStage
    count: int | None = Field(ge=0)
    source: str
    time_field: str | None
    missing_inputs: tuple[str, ...] = ()


class CostInputObservation(ObservationModel):
    """无可信usage和费率时未知；费用只允许原Money/Decimal契约。"""

    model_input_tokens: int | None = Field(default=None, ge=0)
    model_output_tokens: int | None = Field(default=None, ge=0)
    human_work_seconds: int | None = Field(default=None, ge=0)
    total_cost: Money | None = None
    cost_per_qualified_opportunity: Money | None = None
    missing_inputs: tuple[str, ...] = (
        "provider_token_usage_missing",
        "human_time_records_missing",
        "rate_card_missing",
        "qualification_evidence_missing",
    )


class SourceCallObservation(ObservationModel):
    tool_id: str
    call_count: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    duplicate_receipt_count: int = Field(ge=0)


class HandoffOwnerObservation(ObservationModel):
    employee_id: str | None
    queue_depth: int = Field(ge=0)


class HandoffObservation(ObservationModel):
    source: Literal["handoffs.requested"] = "handoffs.requested"
    scope: Literal["tenant_current"] = "tenant_current"
    queue_depth: int = Field(ge=0)
    oldest_wait_seconds: int | None = Field(ge=0)
    invalid_time_count: int = Field(ge=0)
    by_employee: tuple[HandoffOwnerObservation, ...]


class WebCoreObservation(ObservationModel):
    scope: Literal["tenant_window"] = "tenant_window"
    completeness: Literal["partial"] = "partial"
    window_start: datetime
    window_end: datetime
    observed_at: datetime
    stages: tuple[StageObservation, ...]
    qualified_opportunity_count: int | None = Field(default=None, ge=0)
    missing_inputs: tuple[str, ...] = (
        "qualification_evidence_missing",
        "run_entity_attribution_missing",
        "supply_match_source_missing",
    )
    inputs: CostInputObservation = Field(default_factory=CostInputObservation)
    source_calls: tuple[SourceCallObservation, ...]
    consumed_credits: int = Field(ge=0)
    reserved_credits: int = Field(ge=0)
    uncertain_credits: int = Field(ge=0)
    handoffs: HandoffObservation


class RunObservation(ObservationModel):
    """单Run的安全关联与记录时间；无可信绑定不得用文本猜业务对象。"""

    scope: Literal["run"] = "run"
    completeness: Literal["partial"] = "partial"
    source: Literal["workflow_steps_and_tool_calls"] = "workflow_steps_and_tool_calls"
    call_count: int = Field(ge=0)
    attempt_count: int = Field(ge=0)
    duplicate_receipt_count: int = Field(ge=0)
    recorded_span_seconds: int | None = Field(ge=0)
    invalid_time_count: int = Field(ge=0)
    handoff_id: str | None = None
    opportunity_id: str | None = None
    need_id: str | None = None
    responsible_employee_id: str | None = None
    inputs: CostInputObservation = Field(default_factory=CostInputObservation)
