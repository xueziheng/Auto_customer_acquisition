"""把指令域公开 DTO 适配为需求探索 workflow 的确认计划。"""

from __future__ import annotations

from domains.directives.service import DirectiveService
from shared.errors import ValidationError
from shared.schemas.identifiers import EmployeeId, TenantId, UserId
from workflows.demand_discovery.ports import (
    DemandDiscoveryPlan,
    DiscoverySearchQuery,
)


class DirectiveDemandDiscoveryTaskReader:
    def __init__(self, directives: DirectiveService) -> None:
        if not isinstance(directives, DirectiveService):
            raise ValidationError("需求探索指令读取器依赖无效")
        self._directives = directives

    async def load_confirmed(
        self,
        tenant_id: TenantId,
        proposal_id: str,
        acting_user: UserId,
    ) -> DemandDiscoveryPlan:
        plan = await self._directives.get_confirmed_discovery_plan(
            tenant_id,
            proposal_id,
            EmployeeId(str(acting_user)),
        )
        return DemandDiscoveryPlan(
            objective=plan.objective,
            queries=tuple(
                DiscoverySearchQuery(
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
            minimum_confidence_tier=plan.minimum_confidence_tier,
            strategy_group=plan.strategy_group,
            campaign_id=plan.campaign_id,
            role_hints=plan.role_hints,
            assessment_ref=plan.assessment_ref,
        )


__all__ = ("DirectiveDemandDiscoveryTaskReader",)
