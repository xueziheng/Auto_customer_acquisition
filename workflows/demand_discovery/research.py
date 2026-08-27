"""v2仅研究编排：确认范围→免费搜索→有归属证据→假设，不持有触达/报价端口。"""

from __future__ import annotations

from typing import Any

from agent_runtime.base import AgentTask
from domains.demand.schemas import ResearchEvidence, SignalCaptureRequest
from domains.demand.service import DemandService
from domains.prospecting.schemas import AccountResolveRequest
from domains.prospecting.service import ProspectingService
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import NeedHypothesisId, ProspectAccountId
from shared.schemas.provenance import Provenance, SourceType
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.free_search_contracts import FreeSearchError
from workflows.demand_discovery.ports import (
    DemandDiscoveryTaskReader,
    DemandIntelligenceCapability,
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
            "discovery_lanes": ["importer", "distributor", "ecommerce"],
            "signal_ids": [],
            "hypothesis_ids": [],
        }
        if not self._free_search_enabled:
            return "complete", None, {**result, "completion_reason": "unsupported"}
        pages: list[dict[str, Any]] = []
        stop_reason = "plan_completed"
        results_seen = 0
        disallowed = 0
        try:
            for query in plan.queries:
                if (
                    result["searches_used"] >= plan.max_search_queries
                    or result["pages_used"] >= plan.max_pages_read
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
                    break
                try:
                    results_seen += len(batch.results)
                    for index in range(len(batch.results)):
                        if result["pages_used"] >= plan.max_pages_read:
                            stop_reason = "budget_exhausted"
                            break
                        result["pages_used"] += 1
                        try:
                            snapshot = await self._pages.read_page(
                                run.tenant_id, run.run_id, batch, index
                            )
                        except ToolGatewayError as error:
                            if error.category in {
                                ToolErrorCategory.PROVIDER_PERMANENT,
                                ToolErrorCategory.VALIDATION,
                            }:
                                disallowed += 1
                                continue
                            raise
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
                        pages.append(
                            {
                                "text": snapshot.text,
                                "url": snapshot.url,
                                "observed_at": snapshot.observed_at,
                                "content_hash": snapshot.content_hash,
                                "snapshot_artifact_ref": str(
                                    snapshot.snapshot_artifact_ref
                                ),
                                "research_evidence": evidence,
                            }
                        )
                finally:
                    self._searcher.release(batch)
        finally:
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
        if changes.tenant_id != run.tenant_id or changes.run_id != run.run_id:
            raise TenantIsolationViolation("研究变更集租户或Run不一致")
        signals, hypotheses = _split_changes(changes.changes)
        if len(signals) > plan.max_signals or len(hypotheses) > plan.max_hypotheses:
            raise ValidationError("研究变更集超过确认上限")
        trusted: list[ResearchEvidence] = []
        for change in signals:
            payload = _payload(change, "capture_signal")
            candidates = [
                page
                for page in pages
                if page["url"] == payload.get("source_url")
                and page["content_hash"] == payload.get("page_hash")
                and page["snapshot_artifact_ref"]
                == payload.get("snapshot_artifact_ref")
                and page["research_evidence"].model_dump(mode="json")
                == payload.get("research_evidence")
            ]
            if not candidates:
                raise ValidationError("研究信号缺少受信页面与线路归属")
            page = candidates[0]
            evidence = page["research_evidence"]
            observation = _text(
                payload.get("raw_observation"), "研究摘录无效", maximum=2000
            )
            if observation not in page["text"]:
                raise ValidationError("研究摘录不属于原页面")
            signal_id = await self._demand.capture_signal(
                run.tenant_id,
                SignalCaptureRequest(
                    signal_type=_text(payload.get("signal_type"), "研究信号类型无效"),
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
            )
            trusted.append(evidence)
            result["signal_ids"].append(signal_id)
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
            refs = tuple(result["signal_ids"][i] for i in indexes)
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
