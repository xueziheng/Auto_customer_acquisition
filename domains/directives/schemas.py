"""老板指令域对外 DTO。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class DiscoverySearchQueryInput:
    """公开的需求探索查询输入；域内仍会独立校验。"""

    query: str
    country: str
    category: str
    limit: int


@dataclass(frozen=True)
class DemandDiscoveryPlanInput:
    """Trade Manager 提交给指令域的公开需求探索计划。"""

    objective: str
    queries: tuple[DiscoverySearchQueryInput, ...]
    target_countries: tuple[str, ...]
    target_categories: tuple[str, ...]
    excluded_countries: tuple[str, ...]
    excluded_categories: tuple[str, ...]
    max_search_queries: int
    max_pages_read: int
    max_signals: int
    max_hypotheses: int
    minimum_confidence_tier: str
    strategy_group: str
    campaign_id: str
    role_hints: tuple[str, ...]
    assessment_ref: str


@dataclass(frozen=True)
class ProposalView:
    """提案视图 —— 老板确认界面的数据源。

    界面必须并排展示三样东西：原话、系统的理解、预计行为变化。
    只展示解析字段的确认界面发现不了误解析。
    """

    proposal_id: str
    raw_text: str
    interpretation_summary: str
    expected_behavior_changes: list[str]
    parsed_fields: dict[str, str]
    state: str
    created_at: datetime
    decided_at: datetime | None = None
    decided_by_name: str | None = None


@dataclass(frozen=True)
class DirectiveView:
    """生效指令视图。"""

    directive_id: str
    source_proposal_id: str
    version: int
    objective: str
    activated_at: datetime
    activated_by_name: str
    market_assignments: dict[str, str] = field(default_factory=dict)
    need_first_ratio: int | None = None
    catalog_assisted_ratio: int | None = None
    focus_categories: list[str] = field(default_factory=list)
    paused_markets: list[str] = field(default_factory=list)
    max_sequence_messages: int | None = None
    handoff_manager_name: str | None = None
    handoff_triggers: list[str] = field(default_factory=list)
    monthly_budget_credits: int | None = None
    is_rollback: bool = False
    rollback_of_version: int | None = None
    superseded_at: datetime | None = None
