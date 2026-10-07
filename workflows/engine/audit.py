"""Run Center 的安全只读契约与服务层授权。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import (
    ApprovalId,
    ArtifactId,
    RunId,
    SourcingCaseId,
    StepId,
    TenantId,
    ToolCallId,
)

from .observability import RunObservation, WebCoreObservation

DiscoveryLane = Literal["importer", "distributor", "ecommerce"]
ResearchSourceChannel = Literal[
    "public_web",
    "industry_directory",
    "association_members",
    "trade_show_exhibitors",
    "public_procurement",
    "company_news",
    "public_linkedin_company",
    "public_trade_records",
]
ResearchStopReason = Literal[
    "plan_completed",
    "budget_exhausted",
    "no_results",
    "page_disallowed",
    "no_readable_pages",
    "pending_verification",
    "no_supported_signals",
    "quota_exhausted",
    "usage_unknown",
    "paid_enabled",
    "request_uncertain",
    "unsupported",
    "model_permission",
    "model_configuration",
    "model_quota",
    "model_authentication",
    "model_insufficient_balance",
    "model_invalid_request",
    "model_rate_limit",
    "model_provider_error",
    "model_invalid_response",
    "model_unknown",
]


class RunResearchView(BaseModel):
    """研究白名单摘要；尝试数与实际免费 credit 消耗严格分列。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    execution_mode: Literal["research_only"] = "research_only"
    planned_discovery_lanes: tuple[DiscoveryLane, ...] = ()
    discovery_lanes: tuple[DiscoveryLane, ...] = ()
    planned_source_channels: tuple[ResearchSourceChannel, ...] = ()
    searched_source_channels: tuple[ResearchSourceChannel, ...] = ()
    source_channels: tuple[ResearchSourceChannel, ...] = ()
    completion_reason: ResearchStopReason | None = None
    stop_reason: ResearchStopReason | None = None
    searches_used: int = Field(default=0, ge=0)
    pages_used: int = Field(default=0, ge=0)
    signal_count: int = Field(default=0, ge=0)
    hypothesis_count: int = Field(default=0, ge=0)
    pending_verification_count: int = Field(default=0, ge=0)
    validated_need_count: int = Field(default=0, ge=0)
    qualified_opportunity_count: int = Field(default=0, ge=0)
    queued_count: int = Field(default=0, ge=0)
    consumed_credits: int = Field(default=0, ge=0)
    reserved_credits: int = Field(default=0, ge=0)
    uncertain_credits: int = Field(default=0, ge=0)


class RunSourcingLadderView(BaseModel):
    """寻源匹配梯级的安全结果；不包含结论正文或证据定位。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    rung: int = Field(ge=1, le=5)
    outcome: Literal["no_qualified_supply", "qualified_supply_found"]


class RunSourcingStopView(BaseModel):
    """公开寻源停止原因的结构化白名单，不携带 provider 原文。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    code: str = Field(min_length=1, max_length=40)
    stage: str | None = Field(default=None, min_length=1, max_length=40)
    query_index: int | None = Field(default=None, ge=0)
    provider_http_status: int | None = Field(default=None, ge=100, le=599)
    observed_count: int | None = Field(default=None, ge=0)
    configured_limit: int | None = Field(default=None, ge=0)


class RunSourcingView(BaseModel):
    """Sourcing Case V2 的租户绑定安全摘要。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    case_id: SourcingCaseId
    plan_status: str | None = Field(default=None, min_length=1, max_length=32)
    ladder: tuple[RunSourcingLadderView, ...] = ()
    search_attempt_count: int = Field(default=0, ge=0)
    page_attempt_count: int = Field(default=0, ge=0)
    candidate_count: int = Field(default=0, ge=0)
    primary_count: int = Field(default=0, ge=0, le=1)
    alternate_count: int = Field(default=0, ge=0)
    consumed_credits: int = Field(default=0, ge=0)
    reserved_credits: int = Field(default=0, ge=0)
    uncertain_credits: int = Field(default=0, ge=0)
    stop_reason: RunSourcingStopView | None = None


class RunSummaryView(BaseModel):
    """Run 列表的安全摘要；不含 workflow context、步骤 data 或业务正文。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    run_id: RunId
    workflow_type: str
    workflow_version: int
    subject_ref: str
    current_step: str
    status: str
    created_at: datetime
    last_activity_at: datetime
    next_poll_at: datetime | None
    retry_count: int
    last_error: str | None
    research: RunResearchView | None = None
    sourcing: RunSourcingView | None = None


class RunStepView(BaseModel):
    """步骤状态投影；明确排除可能包含业务正文的 ``data``。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    step_id: StepId
    step_name: str
    status: str
    attempt: int
    due_at: datetime
    created_at: datetime
    updated_at: datetime
    error: str | None


class RunToolCallView(BaseModel):
    """工具调用安全元数据；不含输入、结果正文或 provider 凭证。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    tool_call_id: ToolCallId
    tool_id: str
    tool_version: str
    risk_level: str
    cost_class: str
    status: str
    attempt_count: int
    error_category: str | None
    created_at: datetime
    completed_at: datetime | None


class RunArtifactView(BaseModel):
    """系统产物引用；只返回索引元数据，不返回对象键或内容。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    artifact_id: ArtifactId
    kind: str
    mime_type: str
    subject_ref: str
    generated_by: str
    generated_at: datetime


class RunApprovalView(BaseModel):
    """由 Run 发起的审批状态；不返回提案正文、理由或决定备注。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    approval_id: ApprovalId
    approval_type: str
    state: str
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None


class RunDetailView(BaseModel):
    """Run 全景安全投影；所有集合都来自同租户的持久化审计元数据。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    summary: RunSummaryView
    steps: tuple[RunStepView, ...]
    tool_calls: tuple[RunToolCallView, ...]
    artifacts: tuple[RunArtifactView, ...]
    approvals: tuple[RunApprovalView, ...]
    observation: RunObservation | None = None


@dataclass(frozen=True)
class RunAuditActor:
    """Run 审计调用者；角色必须来自员工身份解析，不信任请求头。"""

    actor_id: str
    role: str


@runtime_checkable
class RunAuditRepository(Protocol):
    """按租户读取已经脱敏的 Run 审计投影。"""

    async def list_runs(
        self,
        tenant_id: TenantId,
        *,
        workflow_type: str | None,
        status: str | None,
        limit: int,
    ) -> list[RunSummaryView]: ...

    async def get_run(
        self, tenant_id: TenantId, run_id: RunId
    ) -> RunDetailView | None: ...

    async def get_observability(
        self, tenant_id: TenantId, *, start: datetime, end: datetime, observed_at: datetime,
    ) -> WebCoreObservation: ...


class Phase1RunAuditAuthorizer:
    """Phase 1 仅允许老板读取租户级 Run 审计。"""

    def __init__(self, tenant_id: TenantId) -> None:
        self._tenant_id = tenant_id

    def require_list(self, tenant_id: TenantId, actor: RunAuditActor) -> None:
        if (
            tenant_id != self._tenant_id
            or actor.role != "boss"
            or not actor.actor_id
            or actor.actor_id != actor.actor_id.strip()
        ):
            raise PermissionDenied("Phase 1 Run 审计授权拒绝")

    def require_read(self, tenant_id: TenantId, actor: RunAuditActor) -> None:
        self.require_list(tenant_id, actor)


class RunAuditService:
    """执行服务层二次判权后调用 tenant-bound 安全读仓储。"""

    def __init__(
        self,
        repository: RunAuditRepository,
        authorizer: Phase1RunAuditAuthorizer,
        *, now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._authorizer = authorizer
        self._now = now

    async def get_observability(
        self, tenant_id: TenantId, *, actor: RunAuditActor,
        start: datetime | None = None, end: datetime | None = None,
    ) -> WebCoreObservation:
        """先授权再约束窗口，避免无权限调用进入租户统计读取。"""
        self._authorizer.require_read(tenant_id, actor)
        observed_at = self._now()
        if (start is None) != (end is None):
            raise ValidationError("观测窗口必须成对提供")
        if start is None and end is None:
            end, start = observed_at, observed_at - timedelta(days=7)
        assert start is not None and end is not None
        if (start.utcoffset() is None or end.utcoffset() is None
            or not start < end <= observed_at or end - start > timedelta(days=31)):
            raise ValidationError("观测窗口必须有时区、有效且不超过31天或当前时间")
        return await self._repository.get_observability(
            tenant_id, start=start, end=end, observed_at=observed_at,
        )

    async def list_runs(
        self,
        tenant_id: TenantId,
        *,
        actor: RunAuditActor,
        workflow_type: str | None,
        status: str | None,
        limit: int,
    ) -> list[RunSummaryView]:
        """列出 Run 摘要；仓储不得返回 context、步骤 data 或业务正文。"""
        self._authorizer.require_list(tenant_id, actor)
        return await self._repository.list_runs(
            tenant_id,
            workflow_type=workflow_type,
            status=status,
            limit=limit,
        )

    async def get_run(
        self,
        tenant_id: TenantId,
        run_id: RunId,
        *,
        actor: RunAuditActor,
    ) -> RunDetailView | None:
        """读取同租户 Run 全景；跨租户与不存在都由仓储统一返回不可见。"""
        self._authorizer.require_read(tenant_id, actor)
        return await self._repository.get_run(tenant_id, run_id)


__all__ = (
    "Phase1RunAuditAuthorizer",
    "RunApprovalView",
    "RunArtifactView",
    "RunAuditActor",
    "RunAuditRepository",
    "RunAuditService",
    "RunDetailView",
    "RunSourcingLadderView",
    "RunSourcingStopView",
    "RunSourcingView",
    "RunStepView",
    "RunSummaryView",
    "RunToolCallView",
)
