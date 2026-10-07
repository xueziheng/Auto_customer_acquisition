"""v2仅研究编排：确认范围→免费搜索→有归属证据→假设，不持有触达/报价端口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_runtime.assistant.discovery_queries import query_source_channel
from agent_runtime.base import AgentTask
from domains.demand.schemas import ResearchEvidence, SignalCaptureRequest
from domains.demand.service import DemandService
from domains.prospecting.schemas import AccountResolveRequest
from domains.prospecting.service import ProspectingService
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import NeedHypothesisId, ProspectAccountId
from shared.schemas.model_invocation import ModelGenerationError
from shared.schemas.provenance import Provenance, SourceType
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.free_search_contracts import FreeSearchError
from tool_gateway.handlers.web_slots import SearchResultBatch
from workflows.demand_discovery.ports import (
    DemandDiscoveryTaskReader,
    DemandIntelligenceCapability,
    DiscoverySearchQuery,
    WebDiscoveryPageReader,
    WebDiscoverySearcher,
)
from workflows.demand_discovery.steps import (
    _base_context,
    _confirmed_plan,
    _payload,
    _split_changes,
    _text,
    _utc_datetime,
)
from workflows.engine.runner import StepHandler, WorkflowRun


def _evidence_text(value: object) -> str:
    """逐字证据允许LF/TAB，其他控制符拒绝；不压平或重新拼接原文。"""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 2000
        or any((ord(c) < 32 and c not in {"\n", "\t"}) or ord(c) == 127 for c in value)
    ):
        raise ValidationError("研究摘录无效")
    return value


def _spread_queries(
    queries: tuple[DiscoverySearchQuery, ...],
) -> list[DiscoverySearchQuery]:
    """来源方向轮转；仅重排已确认查询，页面预算小时优先扩大方向覆盖。"""
    groups: dict[str, list[DiscoverySearchQuery]] = {}
    for query in queries:
        groups.setdefault(query_source_channel(query.query), []).append(query)
    scheduled: list[DiscoverySearchQuery] = []
    scope_uses: dict[tuple[str, str], int] = {}
    while any(groups.values()):
        for group in groups.values():
            if not group:
                continue
            # 来源轮转中优先分给尚未覆盖的市场/品类，避免小预算全落在首个市场。
            index = min(range(len(group)), key=lambda i: scope_uses.get(
                (group[i].country, group[i].category), 0
            ))
            query = group.pop(index)
            scope = (query.country, query.category)
            scope_uses[scope] = scope_uses.get(scope, 0) + 1
            scheduled.append(query)
    return scheduled


@dataclass
class _PreparedSignal:
    request: SignalCaptureRequest
    model_indexes: list[int]


@dataclass
class _PendingQuery:
    query: DiscoverySearchQuery
    batch: SearchResultBatch
    next_index: int = 0


def _source_signals(
    signals: list[dict[str, object]],
    pages: list[dict[str, Any]],
) -> tuple[list[_PreparedSignal], list[ResearchEvidence]]:
    """同原页摘录展开至每份可信查询归属，来源优先；模型索引仅用于重连假设。"""
    candidates: dict[tuple[str, str, str], _PreparedSignal] = {}
    trusted: list[ResearchEvidence] = []
    by_source: dict[tuple[str, str], list[_PreparedSignal]] = {
        (page["research_evidence"].discovery_key, page["content_hash"]): []
        for page in pages
    }
    for model_index, change in enumerate(signals):
        payload = _payload(change, "capture_signal")
        references = [
            page
            for page in pages
            if page["url"] == payload.get("source_url")
            and page["content_hash"] == payload.get("page_hash")
            and page["snapshot_artifact_ref"] == payload.get("snapshot_artifact_ref")
            and page["research_evidence"].model_dump(mode="json")
            == payload.get("research_evidence")
        ]
        if not references:
            raise ValidationError("研究信号缺少受信页面与线路归属")
        trusted.append(references[0]["research_evidence"])
        observation = _evidence_text(payload.get("raw_observation"))
        signal_type = _text(payload.get("signal_type"), "研究信号类型无效")
        for page in pages:
            if (
                page["url"] != references[0]["url"]
                or page["content_hash"] != references[0]["content_hash"]
            ):
                continue
            if observation not in page["text"]:
                raise ValidationError("研究摘录不属于原页面")
            evidence = page["research_evidence"]
            key = (evidence.discovery_key, page["content_hash"], signal_type)
            if key in candidates:
                candidate = candidates[key]
                if candidate.request.raw_observation != observation:
                    raise ValidationError("同一研究来源与信号类型存在冲突摘录")
                if model_index not in candidate.model_indexes:
                    candidate.model_indexes.append(model_index)
                continue
            candidate = _PreparedSignal(
                SignalCaptureRequest(
                    signal_type=signal_type,
                    entity_name=evidence.company_name or "待核验公开来源",
                    raw_observation=observation,
                    possible_need=(
                        _text(payload.get("possible_need"), "研究推断无效", maximum=500)
                        if payload.get("possible_need") is not None
                        else None
                    ),
                    observed_at=page["observed_at"],
                    source_type="web_page",
                    source_id=page["content_hash"],
                    page_hash=page["content_hash"],
                    source_url=page["url"],
                    snapshot_artifact_ref=page["snapshot_artifact_ref"],
                    extracted_by=_text(
                        payload.get("extracted_by"), "研究提取者无效", maximum=64
                    ),
                    research_evidence=evidence,
                ),
                [model_index],
            )
            candidates[key] = candidate
            by_source[(evidence.discovery_key, page["content_hash"])].append(candidate)
    groups = [group for group in by_source.values() if group]
    return [group[0] for group in groups] + [
        item for group in groups for item in group[1:]
    ], trusted


class ModeDispatchStep:
    """以已确认提案决定语义；新引擎默认v2不能改变历史提案。"""

    def __init__(
        self,
        reader: DemandDiscoveryTaskReader,
        legacy: StepHandler,
        research: StepHandler,
    ) -> None:
        self._reader, self._legacy, self._research = reader, legacy, research

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        plan, _ = await _confirmed_plan(run, self._reader)
        target = (
            self._research if plan.execution_mode == "research_only" else self._legacy
        )
        return await target.execute(run)


class ResearchScoreStep:
    """只读取确定性置信档位；故意不接受联系人、触达或报价依赖。"""

    def __init__(self, demand: DemandService) -> None:
        self._demand = demand

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        _base_context(run)
        tiers = {}
        for hypothesis_id in run.context.get("hypothesis_ids", []):
            confidence = await self._demand.get_confidence(
                run.tenant_id, NeedHypothesisId(hypothesis_id)
            )
            tiers[hypothesis_id] = confidence.tier.value
        return (
            "complete",
            None,
            {
                "confidence_tiers": tiers,
                "execution_mode": "research_only",
                "queued_count": 0,
                "queued_hypothesis_ids": [],
                "account_discovery_run_ids": [],
                "validated_need_count": 0,
                "qualified_opportunity_count": 0,
            },
        )


class ResearchExecuteSearchStep:
    """原页面与查询归属只在本调用栈内进入能力，正文不进入Run context。

    searches_used/pages_used为本轮预算尝试计数（含拒绝），不是Provider计费量；
    实际免费额度以持久SearchUsageReader为准。
    """

    def __init__(
        self,
        reader: DemandDiscoveryTaskReader,
        searcher: WebDiscoverySearcher,
        pages: WebDiscoveryPageReader,
        capability: DemandIntelligenceCapability,
        demand: DemandService,
        prospecting: ProspectingService,
        *,
        free_search_enabled: bool,
    ) -> None:
        self._reader, self._searcher, self._pages = reader, searcher, pages
        self._capability, self._demand, self._prospecting = (
            capability,
            demand,
            prospecting,
        )
        self._free_search_enabled = free_search_enabled is True

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        plan, acting_user = await _confirmed_plan(run, self._reader)
        result: dict[str, Any] = {
            "execution_mode": "research_only",
            "searches_used": 0,
            "pages_used": 0,
            "signal_count": 0,
            "hypothesis_count": 0,
            "pending_verification_count": 0,
            "validated_need_count": 0,
            "qualified_opportunity_count": 0,
            "queued_count": 0,
            "planned_discovery_lanes": list(dict.fromkeys(
                query.discovery_lane for query in plan.queries
            )),
            "discovery_lanes": [],
            "planned_source_channels": list(dict.fromkeys(
                query_source_channel(query.query) for query in plan.queries
            )),
            "searched_source_channels": [],
            "source_channels": [],
            "signal_ids": [],
            "hypothesis_ids": [],
        }
        if not self._free_search_enabled:
            return "complete", None, {**result, "completion_reason": "unsupported"}
        pages: list[dict[str, Any]] = []
        stop_reason = "plan_completed"
        results_seen = 0
        disallowed = 0
        pending: list[_PendingQuery] = []
        page_sources: set[tuple[str, str]] = set()
        search_failed = False

        def capacity_available() -> bool:
            return (
                result["pages_used"] < plan.max_pages_read
                and len(pages) < plan.max_signals
            )

        async def read_next(item: _PendingQuery) -> bool:
            nonlocal disallowed
            index = item.next_index
            item.next_index += 1
            result["pages_used"] += 1
            try:
                snapshot = await self._pages.read_page(
                    run.tenant_id, run.run_id, item.batch, index
                )
            except ToolGatewayError as error:
                if error.category in {
                    ToolErrorCategory.PROVIDER_PERMANENT,
                    ToolErrorCategory.VALIDATION,
                    ToolErrorCategory.PAGE_ACCESS_FORBIDDEN,
                    ToolErrorCategory.LOGIN_OR_CAPTCHA,
                    ToolErrorCategory.UNSAFE_REDIRECT,
                }:
                    disallowed += 1
                    return False
                raise
            query = item.query
            assert query.discovery_lane is not None
            evidence = ResearchEvidence.from_page(
                proposal_id=run.subject_ref,
                query=query.query,
                discovery_lane=query.discovery_lane,
                query_country=query.country,
                query_category=query.category,
                text=snapshot.text,
                url=snapshot.url,
            )
            identity = (evidence.discovery_key, snapshot.content_hash)
            if identity in page_sources:
                return True
            page_sources.add(identity)
            pages.append({
                "text": snapshot.text,
                "url": snapshot.url,
                "observed_at": snapshot.observed_at,
                "content_hash": snapshot.content_hash,
                "snapshot_artifact_ref": str(snapshot.snapshot_artifact_ref),
                "research_evidence": evidence,
            })
            return True

        try:
            scheduled_queries = _spread_queries(plan.queries)
            for query_index, query in enumerate(scheduled_queries):
                if (
                    result["searches_used"] >= plan.max_search_queries
                    or not capacity_available()
                ):
                    stop_reason = "budget_exhausted"
                    break
                result["searches_used"] += 1
                try:
                    batch = await self._searcher.search(
                        run.tenant_id,
                        run.run_id,
                        query.query,
                        query.country,
                        query.category,
                        query.limit,
                    )
                except FreeSearchError as error:
                    stop_reason = error.reason.value
                    search_failed = True
                    break
                except BaseException:
                    search_failed = True
                    raise
                item = _PendingQuery(query, batch)
                pending.append(item)
                channel = query_source_channel(query.query)
                if channel not in result["searched_source_channels"]:
                    result["searched_source_channels"].append(channel)
                results_seen += len(batch.results)
                # 拒绝页可在本批份额内继续；为尚未执行的查询保留页面机会。
                # 成功后立即换查询，避免后续搜索失败清槽前还没读到本批可读页。
                queries_left = min(len(scheduled_queries), plan.max_search_queries) - query_index
                pages_left = plan.max_pages_read - result["pages_used"]
                allowance = (pages_left + queries_left - 1) // queries_left
                for _ in range(allowance):
                    if not capacity_available() or item.next_index >= len(batch.results):
                        break
                    if await read_next(item):
                        break
            while not search_failed and any(
                item.next_index < len(item.batch.results) for item in pending
            ):
                if not capacity_available():
                    if stop_reason == "plan_completed":
                        stop_reason = "budget_exhausted"
                    break
                for item in pending:
                    if not capacity_available():
                        break
                    if item.next_index < len(item.batch.results):
                        await read_next(item)
        finally:
            # search adapter 在失败时已作废所有句柄；统一清槽，不能逐个释放
            # 过期句柄而覆盖 quota/permission 等原始原因，也不再读取旧批次。
            self._searcher.discard_all()
        if not pages:
            if stop_reason in {"plan_completed", "budget_exhausted"}:
                stop_reason = (
                    "page_disallowed"
                    if disallowed
                    else "no_results"
                    if not results_seen
                    else "no_readable_pages"
                )
            return "complete", None, {**result, "completion_reason": stop_reason}
        try:
            changes = await self._capability.run(
                AgentTask(
                    tenant_id=run.tenant_id,
                    run_id=run.run_id,
                    acting_user=acting_user,
                    objective=plan.objective,
                    inputs={
                        "pages": tuple(pages),
                        "execution_mode": "research_only",
                        "target_countries": plan.target_countries,
                        "target_categories": plan.target_categories,
                        "excluded_countries": plan.excluded_countries,
                        "excluded_categories": plan.excluded_categories,
                        "max_signals": plan.max_signals,
                        "max_hypotheses": plan.max_hypotheses,
                        "strategy_group": plan.strategy_group,
                    },
                ),
                None,
            )
        except ModelGenerationError as error:
            return "fail", "research_model_blocked", {**result, "completion_reason": "model_" + error.code}
        if changes.tenant_id != run.tenant_id or changes.run_id != run.run_id:
            raise TenantIsolationViolation("研究变更集租户或Run不一致")
        signals, hypotheses = _split_changes(changes.changes)
        if len(signals) > plan.max_signals or len(hypotheses) > plan.max_hypotheses:
            raise ValidationError("研究变更集超过确认上限")
        prepared, trusted = _source_signals(signals, pages)
        if len(prepared) > plan.max_signals:
            stop_reason = "budget_exhausted"
        model_signal_ids: list[list[str]] = [[] for _ in signals]
        for candidate in prepared[: plan.max_signals]:
            signal_id = await self._demand.capture_signal(
                run.tenant_id, candidate.request
            )
            assert candidate.request.research_evidence is not None
            evidence = candidate.request.research_evidence
            for index in candidate.model_indexes:
                model_signal_ids[index].append(signal_id)
            result["signal_ids"].append(signal_id)
            if evidence.discovery_lane not in result["discovery_lanes"]:
                result["discovery_lanes"].append(evidence.discovery_lane)
            channel = query_source_channel(evidence.query)
            if channel not in result["source_channels"]:
                result["source_channels"].append(channel)
            if evidence.identity_status == "pending_verification":
                result["pending_verification_count"] += 1
        for change in hypotheses:
            payload = _payload(change, "create_hypothesis")
            indexes = payload.get("signal_indexes")
            if (
                not isinstance(indexes, (tuple, list))
                or not indexes
                or any(type(i) is not int or not 0 <= i < len(trusted) for i in indexes)
            ):
                raise ValidationError("研究假设信号引用无效")
            if any(not model_signal_ids[i] for i in indexes):
                # 模型的额外观察未分得容量时，其假设不能引用没有落库的信号。
                stop_reason = "budget_exhausted"
                continue
            evidence = trusted[indexes[0]]
            if evidence.identity_status != "self_described" or any(
                trusted[i].identity_status != "self_described"
                or trusted[i].website_domain != evidence.website_domain
                or trusted[i].country != evidence.country
                for i in indexes
            ):
                raise ValidationError("待核验研究信号不能创建企业或假设")
            if (
                payload.get("country") != evidence.country
                or evidence.country not in plan.target_countries
            ):
                raise ValidationError("研究假设所在地不受原页面支持")
            if payload.get("category") not in plan.target_categories:
                raise ValidationError("研究假设品类超出确认范围")
            refs = tuple(
                dict.fromkeys(
                    signal_id for i in indexes for signal_id in model_signal_ids[i]
                )
            )
            source = _payload(signals[indexes[0]], "capture_signal")

            def provenance(
                quote: str | None,
                source: dict[str, object] = source,
                evidence: ResearchEvidence = evidence,
            ) -> Provenance:
                return Provenance(
                    source_type=SourceType.WEB_PAGE,
                    source_id=str(source["page_hash"]),
                    extracted_by="system:research-self-description-v1",
                    extracted_at=_utc_datetime(source["observed_at"]),
                    source_url=evidence.source_url,
                    page_hash=str(source["page_hash"]),
                    source_quote=quote,
                )

            assert (
                evidence.company_name and evidence.country and evidence.website_domain
            )
            account_id = await self._prospecting.resolve_account(
                run.tenant_id,
                AccountResolveRequest(
                    entity_name=evidence.company_name,
                    country=evidence.country,
                    website_domain=evidence.website_domain,
                    source_signal_refs=refs,
                    field_provenance={
                        "name": provenance(evidence.identity_quote),
                        "country": provenance(evidence.country_quote),
                    },
                ),
            )
            hypothesis_id = await self._demand.create_hypothesis(
                run.tenant_id,
                ProspectAccountId(account_id),
                str(payload["category"]),
                list(refs),
                _text(payload.get("reasoning"), "研究假设推断无效", maximum=2000),
                _text(payload.get("inferred_by"), "研究假设推断者无效", maximum=64),
            )
            result["hypothesis_ids"].append(str(hypothesis_id))
        result.update(
            signal_count=len(set(result["signal_ids"])),
            hypothesis_count=len(set(result["hypothesis_ids"])),
            completion_reason=stop_reason,
        )
        if result["pending_verification_count"] and stop_reason == "plan_completed":
            result["completion_reason"] = "pending_verification"
        if not signals and stop_reason == "plan_completed":
            result["completion_reason"] = "no_supported_signals"
        return "advance", "generate_hypotheses", result
