"""公开研究的应用层只读投影；不携带配置引用、凭证或供应商响应正文。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from domains.demand.schemas import DemandSignalView
from domains.directives.schemas import ProposalView
from domains.prospecting.schemas import ProspectAccountView, ProspectContactDetailView

ResearchAccessState = Literal[
    "not_configured",
    "configured_unverified",
    "free_last_verified",
    "usage_unknown",
    "paid_enabled",
    "quota_exhausted",
    "snapshot_unavailable",
]


class ResearchAccessView(BaseModel):
    """只说明当前持久记录；实际执行仍须 Gateway 重新核验及预留。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    provider: Literal["tavily"] = "tavily"
    state: ResearchAccessState
    can_confirm_research: bool
    confirmation_requires_recheck: bool = False
    remaining_lower_bound: int | None = None
    checked_at: datetime | None = None
    runtime_activation: Literal["not_verified"] = "not_verified"


@dataclass(frozen=True)
class DiscoveryProposalView(ProposalView):
    """保留历史提案字段，增加后端确认依据与计划线路（非已执行线路）。"""

    execution_mode: Literal["research_only", "outreach_preparation"] = (
        "outreach_preparation"
    )
    planned_discovery_lanes: tuple[str, ...] = ()
    can_confirm: bool = False
    confirmation_blocked_reason: str | None = None
    research_access: ResearchAccessView | None = None


class DiscoveryExecutionView(BaseModel):
    """提案决定与持久 Run 分离；unknown 时不允许猜测或自动重试。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    state: Literal["not_started", "started", "unknown"]
    run_id: str | None = None
    can_resume: bool = False


@dataclass(frozen=True)
class ResearchProspectAccountView(ProspectAccountView):
    """应用层聚合信号来源，避免 prospecting 域直接依赖 demand。"""

    research_signals: tuple[DemandSignalView, ...] = ()


@dataclass(frozen=True)
class ResearchProspectAccountDetailView:
    """沿用联系人详情，仅补企业的公开研究证据。"""

    account: ResearchProspectAccountView
    contacts: tuple[ProspectContactDetailView, ...]
