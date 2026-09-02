"""老板指令的两阶段确认、只增版本与回滚实现。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from domains.directives.errors import DirectiveProposalNotFoundError
from domains.directives.models import (
    DemandDiscoveryConfig,
    Directive,
    DirectiveContent,
    DirectiveObjective,
    DirectiveProposal,
    DiscoveryConfig,
    DiscoverySearchQueryConfig,
    HandoffRules,
    MarketAssignment,
    OutreachBounds,
    ProposalState,
    SourcingAdmissionConfig,
)
from domains.directives.repository import DirectiveUnitOfWorkFactory
from domains.directives.schemas import (
    DemandDiscoveryPlanInput,
    DirectiveView,
    DiscoverySearchQueryInput,
    ProposalView,
    SourcingAdmissionConfigInput,
)
from domains.directives.service import DirectiveEmployeeReader
from shared.errors import InvalidStateTransition, PermissionDenied, ValidationError
from shared.events.catalog import DirectiveActivated
from shared.schemas.evidence import ConfidenceTier
from shared.schemas.identifiers import DirectiveId, EmployeeId, TenantId, new_id

_PROPOSAL_TTL = timedelta(days=7)


def _text(value: object, message: str, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError(message)
    return value


def _strings(
    values: object,
    message: str,
    *,
    maximum_items: int,
    maximum_length: int,
    allow_empty: bool = True,
) -> list[str]:
    if not isinstance(values, list) or len(values) > maximum_items:
        raise ValidationError(message)
    normalized = [
        _text(value, message, maximum=maximum_length) for value in values
    ]
    if not allow_empty and not normalized:
        raise ValidationError(message)
    if len(set(normalized)) != len(normalized):
        raise ValidationError(message)
    return normalized


def _validate_content(content: DirectiveContent) -> DirectiveContent:
    if not isinstance(content, DirectiveContent) or not isinstance(
        content.objective, DirectiveObjective
    ):
        raise ValidationError("指令结构无效")
    if not isinstance(content.market_assignments, list) or len(
        content.market_assignments
    ) > 100:
        raise ValidationError("市场分配无效")
    countries: set[str] = set()
    for assignment in content.market_assignments:
        if not isinstance(assignment, MarketAssignment):
            raise ValidationError("市场分配无效")
        country = _text(assignment.country, "市场分配国家无效", maximum=100)
        _text(str(assignment.owner), "市场分配负责人无效", maximum=64)
        if country in countries:
            raise ValidationError("同一市场不能重复分配")
        countries.add(country)
    discovery = content.discovery
    if discovery is not None:
        if not isinstance(discovery, DiscoveryConfig):
            raise ValidationError("探索配置无效")
        if (
            type(discovery.need_first_ratio) is not int
            or type(discovery.catalog_assisted_ratio) is not int
            or not 0 <= discovery.need_first_ratio <= 100
            or not 0 <= discovery.catalog_assisted_ratio <= 100
            or discovery.need_first_ratio + discovery.catalog_assisted_ratio != 100
        ):
            raise ValidationError("探索配比必须为非负整数且合计 100")
        _strings(
            discovery.focus_categories,
            "重点品类无效",
            maximum_items=100,
            maximum_length=200,
        )
        _strings(
            discovery.excluded_buyer_types,
            "排除客户类型无效",
            maximum_items=100,
            maximum_length=100,
        )
    demand_discovery = content.demand_discovery
    if demand_discovery is not None:
        _validate_demand_discovery(demand_discovery)
    if (
        content.objective is DirectiveObjective.DISCOVER_AND_VALIDATE_DEMAND
        and demand_discovery is None
    ):
        raise ValidationError("需求探索指令必须包含完整探索计划")
    outreach = content.outreach
    if outreach is not None and (
        not isinstance(outreach, OutreachBounds)
        or outreach.primary_channel != "email"
        or type(outreach.max_sequence_messages) is not int
        or not 1 <= outreach.max_sequence_messages <= 5
        or outreach.stop_on_reply is not True
    ):
        raise ValidationError("Phase 1 触达边界无效")
    handoff = content.handoff
    if handoff is not None:
        if not isinstance(handoff, HandoffRules):
            raise ValidationError("接管规则无效")
        _text(str(handoff.manager), "接管经理无效", maximum=64)
        _strings(
            handoff.triggers,
            "接管触发条件无效",
            maximum_items=20,
            maximum_length=100,
            allow_empty=False,
        )
    paused = _strings(
        content.paused_markets,
        "暂停市场无效",
        maximum_items=100,
        maximum_length=100,
    )
    if countries.intersection(paused):
        raise ValidationError("同一指令不能同时分配并暂停市场")
    if content.monthly_budget_credits is not None and (
        type(content.monthly_budget_credits) is not int
        or content.monthly_budget_credits <= 0
    ):
        raise ValidationError("月度预算积分无效")
    if content.notes is not None:
        _text(content.notes, "指令备注无效", maximum=2_000)
    sourcing_admission = content.sourcing_admission
    if sourcing_admission is not None and (
        not isinstance(sourcing_admission, SourcingAdmissionConfig)
        or sourcing_admission.mode != "cluster_ranked"
        or type(sourcing_admission.automatic_admission_enabled) is not bool
        or type(sourcing_admission.batch_limit) is not int
        or not 1 <= sourcing_admission.batch_limit <= 50
    ):
        raise ValidationError("寻源准入配置无效")
    return content


def _utc(value: datetime) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() != timedelta(0)
    ):
        raise ValidationError("指令时间必须是 UTC")
    return value


def _parsed_fields(content: DirectiveContent) -> dict[str, str]:
    fields: dict[str, str] = {"objective": content.objective.value}
    if content.market_assignments:
        fields["market_assignments"] = json.dumps(
            {
                assignment.country: str(assignment.owner)
                for assignment in content.market_assignments
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    if content.discovery is not None:
        fields["need_first_ratio"] = str(content.discovery.need_first_ratio)
        fields["catalog_assisted_ratio"] = str(
            content.discovery.catalog_assisted_ratio
        )
        fields["focus_categories"] = ", ".join(
            content.discovery.focus_categories
        )
    if content.demand_discovery is not None:
        plan = content.demand_discovery
        fields.update(
            {
                "discovery_objective": plan.objective,
                "queries": json.dumps(
                    [
                        {
                            "query": item.query,
                            "country": item.country,
                            "category": item.category,
                            "limit": item.limit,
                            "discovery_lane": item.discovery_lane,
                        }
                        for item in plan.queries
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "target_countries": ", ".join(plan.target_countries),
                "target_categories": ", ".join(plan.target_categories),
                "excluded_countries": ", ".join(plan.excluded_countries),
                "excluded_categories": ", ".join(plan.excluded_categories),
                "max_search_queries": str(plan.max_search_queries),
                "max_pages_read": str(plan.max_pages_read),
                "max_signals": str(plan.max_signals),
                "max_hypotheses": str(plan.max_hypotheses),
                "minimum_confidence_tier": plan.minimum_confidence_tier,
                "strategy_group": plan.strategy_group,
                "execution_mode": plan.execution_mode,
                "campaign_id": plan.campaign_id,
                "role_hints": ", ".join(plan.role_hints),
                "assessment_ref": plan.assessment_ref,
            }
        )
    if content.outreach is not None:
        fields["max_sequence_messages"] = str(
            content.outreach.max_sequence_messages
        )
        fields["stop_on_reply"] = "true"
    if content.handoff is not None:
        fields["handoff_manager"] = str(content.handoff.manager)
        fields["handoff_triggers"] = ", ".join(content.handoff.triggers)
    if content.paused_markets:
        fields["paused_markets"] = ", ".join(content.paused_markets)
    if content.monthly_budget_credits is not None:
        fields["monthly_budget_credits"] = str(content.monthly_budget_credits)
    if content.sourcing_admission is not None:
        fields["sourcing_admission_mode"] = content.sourcing_admission.mode
        fields["automatic_sourcing_admission_enabled"] = (
            "true"
            if content.sourcing_admission.automatic_admission_enabled
            else "false"
        )
        fields["sourcing_admission_batch_limit"] = str(
            content.sourcing_admission.batch_limit
        )
    return fields


def _validate_demand_discovery(config: DemandDiscoveryConfig) -> None:
    if not isinstance(config, DemandDiscoveryConfig):
        raise ValidationError("需求探索计划无效")
    _text(config.objective, "需求探索目标无效", maximum=1_000)
    targets_countries = _strings(
        config.target_countries,
        "需求探索目标国家无效",
        maximum_items=100,
        maximum_length=2,
        allow_empty=False,
    )
    targets_categories = _strings(
        config.target_categories,
        "需求探索目标品类无效",
        maximum_items=100,
        maximum_length=100,
        allow_empty=False,
    )
    excluded_countries = _strings(
        config.excluded_countries,
        "需求探索排除国家无效",
        maximum_items=100,
        maximum_length=2,
    )
    excluded_categories = _strings(
        config.excluded_categories,
        "需求探索排除品类无效",
        maximum_items=100,
        maximum_length=100,
    )
    if any(
        len(country) != 2 or not country.isascii() or not country.isupper()
        for country in (*targets_countries, *excluded_countries)
    ):
        raise ValidationError("需求探索国家必须为大写两位代码")
    if set(targets_countries) & set(excluded_countries) or set(
        targets_categories
    ) & set(excluded_categories):
        raise ValidationError("需求探索目标命中排除项")
    caps = (
        (config.max_search_queries, 100),
        (config.max_pages_read, 50),
        (config.max_signals, 100),
        (config.max_hypotheses, 100),
    )
    if any(type(value) is not int or not 1 <= value <= maximum for value, maximum in caps):
        raise ValidationError("需求探索硬上限无效")
    if (
        not isinstance(config.queries, list)
        or not config.queries
        or len(config.queries) > 100
        or len(config.queries) > config.max_search_queries
    ):
        raise ValidationError("需求探索查询无效")
    seen_queries: set[tuple[str, str, str]] = set()
    for item in config.queries:
        if not isinstance(item, DiscoverySearchQueryConfig):
            raise ValidationError("需求探索查询无效")
        query = _text(item.query, "需求探索查询无效", maximum=400)
        if len(query.split()) > 50:
            raise ValidationError("需求探索查询无效")
        if (
            item.country not in targets_countries
            or item.category not in targets_categories
            or type(item.limit) is not int
            or not 1 <= item.limit <= 20
        ):
            raise ValidationError("需求探索查询超出确认范围")
        identity = (query, item.country, item.category)
        if identity in seen_queries:
            raise ValidationError("需求探索查询不能重复")
        seen_queries.add(identity)
    try:
        ConfidenceTier(config.minimum_confidence_tier)
    except ValueError:
        raise ValidationError("需求探索置信档位门槛无效") from None
    _text(config.strategy_group, "需求探索策略组无效", maximum=64)
    if config.execution_mode not in {"research_only", "outreach_preparation"}:
        raise ValidationError("需求探索执行模式无效")
    if config.execution_mode == "research_only":
        if (
            len(config.queries) > config.max_search_queries
            or {q.discovery_lane for q in config.queries}
            != {"importer", "distributor", "ecommerce"}
        ):
            raise ValidationError("研究计划必须在确认预算内覆盖三线路")
        return
    _text(config.campaign_id, "需求探索 Campaign 无效", maximum=40)
    _strings(
        config.role_hints,
        "需求探索角色提示无效",
        maximum_items=10,
        maximum_length=200,
    )
    _text(config.assessment_ref, "需求探索评估引用无效", maximum=200)


class DirectiveServiceImpl:
    def __init__(
        self,
        uow_factory: DirectiveUnitOfWorkFactory,
        employees: DirectiveEmployeeReader,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not isinstance(uow_factory, DirectiveUnitOfWorkFactory):
            raise ValidationError("指令事务依赖无效")
        if not isinstance(employees, DirectiveEmployeeReader):
            raise ValidationError("指令员工依赖无效")
        self._uow_factory = uow_factory
        self._employees = employees
        self._now = now or (lambda: datetime.now(UTC))

    async def submit_proposal(
        self,
        tenant_id: TenantId,
        raw_text: str,
        parsed: DirectiveContent,
        interpretation_summary: str,
        expected_behavior_changes: list[str],
        parsed_by: str,
    ) -> str:
        self._validate_tenant(tenant_id)
        _text(raw_text, "老板指令原话无效", maximum=10_000)
        _validate_content(parsed)
        if parsed.sourcing_admission is not None:
            raise ValidationError("寻源准入配置必须通过寻源准入提案接口提交")
        _text(interpretation_summary, "指令理解摘要无效", maximum=4_000)
        changes = _strings(
            expected_behavior_changes,
            "指令预计行为变化不能为空",
            maximum_items=50,
            maximum_length=1_000,
            allow_empty=False,
        )
        _text(parsed_by, "指令解析器版本无效", maximum=128)
        now = _utc(self._now())
        async with self._uow_factory(tenant_id) as uow:
            active = await uow.directives.get_active(tenant_id)
            complete = _validate_content(
                replace(
                    parsed,
                    sourcing_admission=(
                        None
                        if active is None
                        else active.content.sourcing_admission
                    ),
                )
            )
            proposal = DirectiveProposal(
                proposal_id=new_id("dpr"),
                tenant_id=tenant_id,
                raw_text=raw_text,
                parsed=complete,
                interpretation_summary=interpretation_summary,
                expected_behavior_changes=changes,
                parsed_by=parsed_by,
                created_at=now,
                base_directive_version=(
                    0 if active is None else active.version
                ),
            )
            await uow.proposals.add(proposal)
        return proposal.proposal_id

    async def submit_discovery_proposal(
        self,
        tenant_id: TenantId,
        raw_text: str,
        plan: DemandDiscoveryPlanInput,
        interpretation_summary: str,
        expected_behavior_changes: list[str],
        parsed_by: str,
    ) -> str:
        if not isinstance(plan, DemandDiscoveryPlanInput):
            raise ValidationError("需求探索提案输入无效")
        return await self.submit_proposal(
            tenant_id,
            raw_text,
            DirectiveContent(
                objective=DirectiveObjective.DISCOVER_AND_VALIDATE_DEMAND,
                demand_discovery=DemandDiscoveryConfig(
                    objective=plan.objective,
                    queries=[
                        DiscoverySearchQueryConfig(
                            query=item.query,
                            country=item.country,
                            category=item.category,
                            limit=item.limit,
                            discovery_lane=item.discovery_lane,
                        )
                        for item in plan.queries
                    ],
                    target_countries=list(plan.target_countries),
                    target_categories=list(plan.target_categories),
                    excluded_countries=list(plan.excluded_countries),
                    excluded_categories=list(plan.excluded_categories),
                    max_search_queries=plan.max_search_queries,
                    max_pages_read=plan.max_pages_read,
                    max_signals=plan.max_signals,
                    max_hypotheses=plan.max_hypotheses,
                    minimum_confidence_tier=plan.minimum_confidence_tier,
                    strategy_group=plan.strategy_group,
                    campaign_id=plan.campaign_id,
                    role_hints=list(plan.role_hints),
                    assessment_ref=plan.assessment_ref,
                    execution_mode=plan.execution_mode,
                ),
            ),
            interpretation_summary,
            expected_behavior_changes,
            parsed_by,
        )

    async def submit_sourcing_admission_proposal(
        self,
        tenant_id: TenantId,
        raw_text: str,
        config: SourcingAdmissionConfigInput,
        interpretation_summary: str,
        expected_behavior_changes: list[str],
        parsed_by: str,
        *,
        submitted_by: EmployeeId,
    ) -> str:
        await self._require_boss(tenant_id, submitted_by)
        _text(raw_text, "老板指令原话无效", maximum=10_000)
        if not isinstance(config, SourcingAdmissionConfigInput):
            raise ValidationError("寻源准入提案输入无效")
        admission = SourcingAdmissionConfig(
            mode=config.mode,
            automatic_admission_enabled=config.automatic_admission_enabled,
            batch_limit=config.batch_limit,
        )
        if (
            admission.mode != "cluster_ranked"
            or type(admission.automatic_admission_enabled) is not bool
            or type(admission.batch_limit) is not int
            or not 1 <= admission.batch_limit <= 50
        ):
            raise ValidationError("寻源准入配置无效")
        _text(interpretation_summary, "指令理解摘要无效", maximum=4_000)
        changes = _strings(
            expected_behavior_changes,
            "指令预计行为变化不能为空",
            maximum_items=50,
            maximum_length=1_000,
            allow_empty=False,
        )
        _text(parsed_by, "指令解析器版本无效", maximum=128)
        now = _utc(self._now())
        async with self._uow_factory(tenant_id) as uow:
            active = await uow.directives.get_active(tenant_id)
            base_content = (
                DirectiveContent(objective=DirectiveObjective.FOCUS_EXISTING_NEEDS)
                if active is None
                else active.content
            )
            parsed = _validate_content(
                replace(base_content, sourcing_admission=admission)
            )
            proposal = DirectiveProposal(
                proposal_id=new_id("dpr"),
                tenant_id=tenant_id,
                raw_text=raw_text,
                parsed=parsed,
                interpretation_summary=interpretation_summary,
                expected_behavior_changes=changes,
                parsed_by=parsed_by,
                created_at=now,
                base_directive_version=0 if active is None else active.version,
            )
            await uow.proposals.add(proposal)
        return proposal.proposal_id

    async def confirm_proposal(
        self,
        tenant_id: TenantId,
        proposal_id: str,
        confirmed_by: EmployeeId,
    ) -> DirectiveId:
        await self._require_boss(tenant_id, confirmed_by)
        _text(proposal_id, "指令提案标识无效", maximum=40)
        now = _utc(self._now())
        expired = False
        activated: Directive | None = None
        async with self._uow_factory(tenant_id) as uow:
            proposal = await uow.proposals.get_for_update(tenant_id, proposal_id)
            if proposal is None:
                raise DirectiveProposalNotFoundError("指令提案不存在")
            if proposal.state is not ProposalState.PENDING_CONFIRMATION:
                raise InvalidStateTransition("指令提案已决策，不能再次确认")
            version = await uow.directives.next_version(tenant_id)
            active = await uow.directives.get_active_for_update(tenant_id)
            if proposal.base_directive_version is not None:
                current_version = 0 if active is None else active.version
                if proposal.base_directive_version != current_version:
                    raise InvalidStateTransition(
                        "指令提案基线已陈旧，必须基于当前指令重新提交"
                    )
            if now >= proposal.created_at + _PROPOSAL_TTL:
                await uow.proposals.update(
                    replace(
                        proposal,
                        state=ProposalState.EXPIRED,
                        decided_at=now,
                    )
                )
                expired = True
            else:
                if active is not None:
                    await uow.directives.mark_superseded(
                        tenant_id, active.directive_id
                    )
                activated = Directive(
                    directive_id=DirectiveId(new_id("dir")),
                    tenant_id=tenant_id,
                    version=version,
                    content=proposal.parsed,
                    source_proposal_id=proposal.proposal_id,
                    activated_at=now,
                    activated_by=confirmed_by,
                )
                await uow.directives.add(activated)
                await uow.directives.set_active(activated)
                await uow.proposals.update(
                    replace(
                        proposal,
                        state=ProposalState.CONFIRMED,
                        decided_at=now,
                        decided_by=confirmed_by,
                    )
                )
                await uow.bus.publish(
                    DirectiveActivated(
                        tenant_id=tenant_id,
                        occurred_at=now,
                        run_id=None,
                        directive_id=str(activated.directive_id),
                        version=version,
                    )
                )
        if expired:
            raise InvalidStateTransition("指令提案已过期，必须重新解析")
        if activated is None:
            raise InvalidStateTransition("指令提案未能生效")
        return activated.directive_id

    async def reject_proposal(
        self,
        tenant_id: TenantId,
        proposal_id: str,
        rejected_by: EmployeeId,
    ) -> None:
        await self._require_boss(tenant_id, rejected_by)
        _text(proposal_id, "指令提案标识无效", maximum=40)
        now = _utc(self._now())
        async with self._uow_factory(tenant_id) as uow:
            proposal = await uow.proposals.get_for_update(tenant_id, proposal_id)
            if proposal is None:
                raise ValidationError("指令提案不存在")
            if proposal.state is not ProposalState.PENDING_CONFIRMATION:
                raise InvalidStateTransition("指令提案已决策，不能再次否决")
            state = (
                ProposalState.EXPIRED
                if now >= proposal.created_at + _PROPOSAL_TTL
                else ProposalState.REJECTED
            )
            await uow.proposals.update(
                replace(
                    proposal,
                    state=state,
                    decided_at=now,
                    decided_by=(
                        None if state is ProposalState.EXPIRED else rejected_by
                    ),
                )
            )
        if state is ProposalState.EXPIRED:
            raise InvalidStateTransition("指令提案已过期，不能否决")

    async def rollback_to_version(
        self,
        tenant_id: TenantId,
        version: int,
        requested_by: EmployeeId,
    ) -> DirectiveId:
        await self._require_boss(tenant_id, requested_by)
        if type(version) is not int or version < 1:
            raise ValidationError("指令版本无效")
        now = _utc(self._now())
        async with self._uow_factory(tenant_id) as uow:
            next_version = await uow.directives.next_version(tenant_id)
            target = await uow.directives.get_version(tenant_id, version)
            if target is None:
                raise ValidationError("指令历史版本不存在")
            active = await uow.directives.get_active_for_update(tenant_id)
            if active is not None:
                await uow.directives.mark_superseded(
                    tenant_id, active.directive_id
                )
            directive = Directive(
                directive_id=DirectiveId(new_id("dir")),
                tenant_id=tenant_id,
                version=next_version,
                content=target.content,
                source_proposal_id=target.source_proposal_id,
                activated_at=now,
                activated_by=requested_by,
                rollback_of=target.version,
            )
            await uow.directives.add(directive)
            await uow.directives.set_active(directive)
            await uow.bus.publish(
                DirectiveActivated(
                    tenant_id=tenant_id,
                    occurred_at=now,
                    run_id=None,
                    directive_id=str(directive.directive_id),
                    version=directive.version,
                )
            )
        return directive.directive_id

    async def get_active(self, tenant_id: TenantId) -> DirectiveView | None:
        self._validate_tenant(tenant_id)
        async with self._uow_factory(tenant_id) as uow:
            directive = await uow.directives.get_active(tenant_id)
        if directive is None:
            return None
        names = await self._employee_names(tenant_id, directive)
        return self._directive_view(directive, names)

    async def get_proposal(
        self, tenant_id: TenantId, proposal_id: str
    ) -> ProposalView:
        self._validate_tenant(tenant_id)
        _text(proposal_id, "指令提案标识无效", maximum=40)
        async with self._uow_factory(tenant_id) as uow:
            proposal = await uow.proposals.get(tenant_id, proposal_id)
        if proposal is None:
            raise DirectiveProposalNotFoundError("指令提案不存在")
        names: dict[EmployeeId, str] = {}
        if proposal.decided_by is not None:
            names = await self._employees.names_for(
                tenant_id, (proposal.decided_by,)
            )
            if set(names) != {proposal.decided_by}:
                raise ValidationError("指令员工展示名结果无效")
        return ProposalView(
            proposal_id=proposal.proposal_id,
            raw_text=proposal.raw_text,
            interpretation_summary=proposal.interpretation_summary,
            expected_behavior_changes=list(proposal.expected_behavior_changes),
            parsed_fields=_parsed_fields(proposal.parsed),
            state=proposal.state.value,
            created_at=proposal.created_at,
            decided_at=proposal.decided_at,
            decided_by_id=(
                None
                if proposal.decided_by is None
                else str(proposal.decided_by)
            ),
            decided_by_name=(
                None
                if proposal.decided_by is None
                else names[proposal.decided_by]
            ),
            sourcing_admission_mode=(
                None
                if proposal.parsed.sourcing_admission is None
                else proposal.parsed.sourcing_admission.mode
            ),
            automatic_sourcing_admission_enabled=(
                None
                if proposal.parsed.sourcing_admission is None
                else proposal.parsed.sourcing_admission.automatic_admission_enabled
            ),
            sourcing_admission_batch_limit=(
                None
                if proposal.parsed.sourcing_admission is None
                else proposal.parsed.sourcing_admission.batch_limit
            ),
        )

    async def get_confirmed_discovery_plan(
        self,
        tenant_id: TenantId,
        proposal_id: str,
        confirmed_by: EmployeeId,
    ) -> DemandDiscoveryPlanInput:
        self._validate_tenant(tenant_id)
        _text(proposal_id, "指令提案标识无效", maximum=40)
        _text(str(confirmed_by), "指令确认员工无效", maximum=64)
        async with self._uow_factory(tenant_id) as uow:
            proposal = await uow.proposals.get(tenant_id, proposal_id)
        if (
            proposal is None
            or proposal.state is not ProposalState.CONFIRMED
            or proposal.decided_by != confirmed_by
            or proposal.parsed.objective
            is not DirectiveObjective.DISCOVER_AND_VALIDATE_DEMAND
            or proposal.parsed.demand_discovery is None
        ):
            raise InvalidStateTransition("需求探索提案未由当前发起人确认")
        plan = proposal.parsed.demand_discovery
        _validate_demand_discovery(plan)
        return DemandDiscoveryPlanInput(
            objective=plan.objective,
            queries=tuple(
                DiscoverySearchQueryInput(
                    query=item.query,
                    country=item.country,
                    category=item.category,
                    limit=item.limit,
                    discovery_lane=item.discovery_lane,
                )
                for item in plan.queries
            ),
            target_countries=tuple(plan.target_countries),
            target_categories=tuple(plan.target_categories),
            excluded_countries=tuple(plan.excluded_countries),
            excluded_categories=tuple(plan.excluded_categories),
            max_search_queries=plan.max_search_queries,
            max_pages_read=plan.max_pages_read,
            max_signals=plan.max_signals,
            max_hypotheses=plan.max_hypotheses,
            minimum_confidence_tier=plan.minimum_confidence_tier,
            strategy_group=plan.strategy_group,
            campaign_id=plan.campaign_id,
            role_hints=tuple(plan.role_hints),
            assessment_ref=plan.assessment_ref,
            execution_mode=plan.execution_mode,
        )

    async def list_versions(
        self, tenant_id: TenantId, limit: int = 20
    ) -> list[DirectiveView]:
        self._validate_tenant(tenant_id)
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValidationError("指令版本读取上限无效")
        async with self._uow_factory(tenant_id) as uow:
            directives = await uow.directives.list_versions(tenant_id, limit)
        employee_ids = tuple(
            dict.fromkeys(
                employee_id
                for directive in directives
                for employee_id in self._directive_employee_ids(directive)
            )
        )
        names = await self._employees.names_for(tenant_id, employee_ids)
        if set(names) != set(employee_ids):
            raise ValidationError("指令员工展示名结果无效")
        return [self._directive_view(directive, names) for directive in directives]

    @staticmethod
    def _validate_tenant(tenant_id: TenantId) -> None:
        _text(str(tenant_id), "指令租户无效", maximum=64)

    async def _require_boss(
        self, tenant_id: TenantId, employee_id: EmployeeId
    ) -> None:
        self._validate_tenant(tenant_id)
        _text(str(employee_id), "指令决策员工无效", maximum=64)
        if not await self._employees.is_active_boss(tenant_id, employee_id):
            raise PermissionDenied("只有在职老板可以确认指令")

    @staticmethod
    def _directive_employee_ids(
        directive: Directive,
    ) -> tuple[EmployeeId, ...]:
        content = directive.content
        values = [
            directive.activated_by,
            *(assignment.owner for assignment in content.market_assignments),
        ]
        if content.handoff is not None:
            values.append(content.handoff.manager)
        return tuple(dict.fromkeys(values))

    async def _employee_names(
        self, tenant_id: TenantId, directive: Directive
    ) -> dict[EmployeeId, str]:
        employee_ids = self._directive_employee_ids(directive)
        names = await self._employees.names_for(tenant_id, employee_ids)
        if set(names) != set(employee_ids):
            raise ValidationError("指令员工展示名结果无效")
        return names

    @staticmethod
    def _directive_view(
        directive: Directive,
        names: dict[EmployeeId, str],
    ) -> DirectiveView:
        content = directive.content
        discovery = content.discovery
        outreach = content.outreach
        handoff = content.handoff
        sourcing_admission = content.sourcing_admission
        return DirectiveView(
            directive_id=str(directive.directive_id),
            source_proposal_id=directive.source_proposal_id,
            version=directive.version,
            objective=content.objective.value,
            activated_at=directive.activated_at,
            activated_by_name=names[directive.activated_by],
            market_assignments={
                assignment.country: names[assignment.owner]
                for assignment in content.market_assignments
            },
            need_first_ratio=(
                None if discovery is None else discovery.need_first_ratio
            ),
            catalog_assisted_ratio=(
                None if discovery is None else discovery.catalog_assisted_ratio
            ),
            focus_categories=(
                [] if discovery is None else list(discovery.focus_categories)
            ),
            paused_markets=list(content.paused_markets),
            max_sequence_messages=(
                None if outreach is None else outreach.max_sequence_messages
            ),
            handoff_manager_name=(
                None if handoff is None else names[handoff.manager]
            ),
            handoff_triggers=(
                [] if handoff is None else list(handoff.triggers)
            ),
            monthly_budget_credits=content.monthly_budget_credits,
            is_rollback=directive.rollback_of is not None,
            rollback_of_version=directive.rollback_of,
            superseded_at=directive.superseded_at,
            sourcing_admission_mode=(
                None if sourcing_admission is None else sourcing_admission.mode
            ),
            automatic_sourcing_admission_enabled=(
                None
                if sourcing_admission is None
                else sourcing_admission.automatic_admission_enabled
            ),
            sourcing_admission_batch_limit=(
                None
                if sourcing_admission is None
                else sourcing_admission.batch_limit
            ),
        )


__all__ = ("DirectiveServiceImpl",)
