"""采集投影的有限诊断契约；仅固定枚举与计数，不承载模型或网页原文。"""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.schemas.model_invocation import ModelGenerationError

Count = Annotated[int, Field(strict=True, ge=0, le=100)]
Shape = Literal[
    "not_object",
    "invalid_top_keys",
    "candidates_not_list",
    "candidates_over_limit",
    "invalid_json",
    "valid",
]


class SafeCounts(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class ToolUsageSummary(SafeCounts):
    """固定工具槽的快照；未完成保留队列/在途，旧记录不推断为零。"""

    tool_limit: Annotated[int, Field(strict=True, ge=1, le=100)]
    reserved: Count = 0
    source_search: Count = 0
    read_public_page: Count = 0
    browse_public_page: Count = 0
    queued: Count = 0
    inflight: Annotated[int, Field(strict=True, ge=0, le=1)] = 0
    terminal: Count = 0
    cancelled: Count = 0
    succeeded: Count = 0
    search_limit_reached: Count = 0
    source_read_required: Count = 0
    page_id_unknown: Count = 0
    page_unavailable: Count = 0
    budget_exhausted: Count = 0
    failed_closed: Count = 0
    unreserved_budget: Count = 0
    unreserved_deadline: Count = 0
    unreserved_overflow: bool = False

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if not (
            self.reserved <= self.tool_limit
            and self.reserved
            == self.source_search + self.read_public_page + self.browse_public_page
            and self.reserved
            == self.queued + self.inflight + self.terminal + self.cancelled
            and self.terminal
            == self.succeeded
            + self.search_limit_reached
            + self.source_read_required
            + self.page_id_unknown
            + self.page_unavailable
            + self.budget_exhausted
            + self.failed_closed
        ):
            raise ValueError("工具槽计数不一致")
        if (
            self.unreserved_overflow
            and max(self.unreserved_budget, self.unreserved_deadline) != 100
        ):
            raise ValueError("工具拒绝超限计数无效")
        return self


class RejectionCounts(SafeCounts):
    item_not_object: Count = 0
    item_fields_invalid: Count = 0
    page_unknown: Count = 0
    page_not_read: Count = 0
    discovery_only: Count = 0
    instruction_content: Count = 0
    company_name_unproven: Count = 0
    country_unsupported: Count = 0
    country_address_unproven: Count = 0
    product_unproven: Count = 0
    email_unproven: Count = 0
    source_url_invalid: Count = 0
    company_domain_unproven: Count = 0
    email_domain_mismatch: Count = 0
    candidate_contract_invalid: Count = 0


class ProjectionSummary(SafeCounts):
    input_shape: Shape
    tool_usage: ToolUsageSummary | None = None
    raw_count: Count | None = None
    overflow: bool = False
    projection_complete: bool = False
    internal_failure: bool = False
    projected_count: Count = 0
    rejected: RejectionCounts = Field(default_factory=RejectionCounts)
    search_result_count: Annotated[int, Field(strict=True, ge=0, le=40)] = 0
    read_success_count: Annotated[int, Field(strict=True, ge=0, le=40)] = 0
    official_read_count: Annotated[int, Field(strict=True, ge=0, le=40)] = 0

    @model_validator(mode="after")
    def consistent(self) -> Self:
        total = self.projected_count + sum(self.rejected.model_dump().values())
        if self.input_shape == "valid":
            if self.raw_count is None or total > self.raw_count or self.overflow:
                raise ValueError("投影计数不一致")
            if self.projection_complete and (
                total != self.raw_count or self.internal_failure
            ):
                raise ValueError("投影完成计数不一致")
        elif total or self.projection_complete or self.internal_failure:
            raise ValueError("非法输入不得含投影结果")
        elif self.input_shape == "candidates_over_limit":
            if self.raw_count != 100 or not self.overflow:
                raise ValueError("超限计数无效")
        elif self.raw_count is not None or self.overflow:
            raise ValueError("非列表不能有条目计数")
        if (
            not self.official_read_count
            <= self.read_success_count
            <= self.search_result_count
        ):
            raise ValueError("页面计数不一致")
        return self


class SaveSummary(SafeCounts):
    total_count: Count
    inserted_fresh: Count = 0
    inserted_not_fresh: Count = 0
    existing_refreshed: Count = 0
    existing_unchanged: Count = 0
    query_match: Count = 0
    query_mismatch: Count = 0
    fresh: Count = 0
    future_observed: Count = 0
    expired: Count = 0

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (
            self.inserted_fresh
            + self.inserted_not_fresh
            + self.existing_refreshed
            + self.existing_unchanged
            != self.total_count
            or self.query_match + self.query_mismatch != self.total_count
            or self.fresh + self.future_observed + self.expired != self.total_count
        ):
            raise ValueError("保存计数不一致")
        return self


class SourceDiagnostic(SafeCounts):
    version: Literal[1] = 1
    projection: ProjectionSummary
    save: SaveSummary | None = None

    @field_validator("version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("诊断版本类型无效")
        return value

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if self.save is not None and (
            not self.projection.projection_complete
            or self.save.total_count != self.projection.projected_count
        ):
            raise ValueError("来源分层计数不一致")
        return self


class ProjectionDiagnosticError(ModelGenerationError):
    """仅附有安全摘要；沿来源失败端口传递，不包含原始输入。"""

    def __init__(self, summary: ProjectionSummary) -> None:
        super().__init__("invalid_response")
        self.summary = summary


def inspect_candidate_shape(value: object) -> ProjectionSummary:
    """复用真实模型既有顶层接受谓词，不将缺失列表当作空。"""
    if not isinstance(value, dict):
        return ProjectionSummary(input_shape="not_object")
    if set(value) != {"candidates"}:
        return ProjectionSummary(input_shape="invalid_top_keys")
    items = value["candidates"]
    if not isinstance(items, list):
        return ProjectionSummary(input_shape="candidates_not_list")
    if len(items) > 100:
        return ProjectionSummary(
            input_shape="candidates_over_limit", raw_count=100, overflow=True
        )
    return ProjectionSummary(input_shape="valid", raw_count=len(items))
