"""有界需求探索：确认提案 → Web 工具 → 信号/假设 → 账户发现队列。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from agent_runtime.base import AgentTask
from domains.demand.schemas import SignalCaptureRequest
from domains.demand.service import DemandService
from domains.prospecting.schemas import AccountResolveRequest
from domains.prospecting.service import ProspectingService
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.evidence import ConfidenceTier, meets_threshold
from shared.schemas.identifiers import (
    NeedHypothesisId,
    ProspectAccountId,
    UserId,
)
from shared.schemas.provenance import Provenance, SourceType
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from workflows.demand_discovery.ports import (
    AccountDiscoveryQueue,
    DemandDiscoveryPlan,
    DemandDiscoveryTaskReader,
    DemandIntelligenceCapability,
    DiscoverySearchQuery,
    WebDiscoveryPageReader,
    WebDiscoverySearcher,
)
from workflows.engine.runner import WorkflowRun


def _text(value: object, message: str, *, maximum: int = 200) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError(message)
    return value


def _base_context(run: WorkflowRun) -> tuple[str, UserId]:
    proposal_id = _text(
        run.context.get("proposal_id"),
        "需求探索缺少 proposal",
        maximum=64,
    )
    if run.subject_ref != proposal_id:
        raise ValidationError("需求探索 workflow subject 无效")
    acting_user = UserId(
        _text(
            run.context.get("acting_user_id"),
            "需求探索缺少发起人",
            maximum=64,
        )
    )
    return proposal_id, acting_user


async def _confirmed_plan(
    run: WorkflowRun,
    reader: DemandDiscoveryTaskReader,
) -> tuple[DemandDiscoveryPlan, UserId]:
    proposal_id, acting_user = _base_context(run)
    plan = await reader.load_confirmed(run.tenant_id, proposal_id, acting_user)
    _validate_plan(plan)
    return plan, acting_user


def _validate_plan(plan: DemandDiscoveryPlan) -> None:
    if not isinstance(plan, DemandDiscoveryPlan):
        raise ValidationError("需求探索确认提案无效")
    target_countries = _string_tuple(plan.target_countries, maximum=2, limit=100)
    target_categories = _string_tuple(plan.target_categories, maximum=100, limit=100)
    excluded_countries = _string_tuple(
        plan.excluded_countries, maximum=2, limit=100, allow_empty=True
    )
    excluded_categories = _string_tuple(
        plan.excluded_categories, maximum=100, limit=100, allow_empty=True
    )
    if set(target_countries) & set(excluded_countries) or set(target_categories) & set(
        excluded_categories
    ):
        raise ValidationError("需求探索目标命中排除项")
    if (
        type(plan.queries) is not tuple
        or not plan.queries
        or len(plan.queries) > 100
        or type(plan.max_search_queries) is not int
        or not 1 <= plan.max_search_queries <= 100
        or type(plan.max_pages_read) is not int
        or not 1 <= plan.max_pages_read <= 50
        or type(plan.max_signals) is not int
        or not 1 <= plan.max_signals <= 100
        or type(plan.max_hypotheses) is not int
        or not 1 <= plan.max_hypotheses <= 100
    ):
        raise ValidationError("需求探索上限无效")
    for query in plan.queries:
        if not isinstance(query, DiscoverySearchQuery):
            raise ValidationError("需求探索查询无效")
        _text(query.query, "需求探索查询无效", maximum=400)
        if len(query.query.split()) > 50:
            raise ValidationError("需求探索查询无效")
        if (
            query.country not in target_countries
            or query.category not in target_categories
        ):
            raise ValidationError("需求探索查询超出已确认范围")
        if type(query.limit) is not int or not 1 <= query.limit <= 20:
            raise ValidationError("需求探索查询上限无效")
    try:
        ConfidenceTier(plan.minimum_confidence_tier)
    except ValueError:
        raise ValidationError("需求探索置信档位门槛无效") from None
    _text(plan.objective, "需求探索目标无效", maximum=1_000)
    _text(plan.strategy_group, "需求探索策略组无效", maximum=64)
    _text(plan.campaign_id, "需求探索 Campaign 无效", maximum=40)
    _text(plan.assessment_ref, "需求探索评估引用无效", maximum=200)
    _string_tuple(plan.role_hints, maximum=200, limit=10, allow_empty=True)


class PlanSearchStep:
    def __init__(self, task_reader: DemandDiscoveryTaskReader) -> None:
        self._task_reader = task_reader

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        plan, _acting_user = await _confirmed_plan(run, self._task_reader)
        return (
            "advance",
            "execute_search",
            {
                "query_budget": plan.max_search_queries,
                "page_budget": plan.max_pages_read,
                "signal_budget": plan.max_signals,
                "hypothesis_budget": plan.max_hypotheses,
                "strategy_group": plan.strategy_group,
            },
        )


class ExecuteSearchStep:
    """同一受信调用栈内完成 Web 读取、模型分析与域写入。"""

    def __init__(
        self,
        task_reader: DemandDiscoveryTaskReader,
        searcher: WebDiscoverySearcher,
        page_reader: WebDiscoveryPageReader,
        capability: DemandIntelligenceCapability,
        demand: DemandService,
        prospecting: ProspectingService,
    ) -> None:
        self._task_reader = task_reader
        self._searcher = searcher
        self._page_reader = page_reader
        self._capability = capability
        self._demand = demand
        self._prospecting = prospecting

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        plan, acting_user = await _confirmed_plan(run, self._task_reader)
        pages: list[dict[str, object]] = []
        searches_used = 0
        pages_used = 0
        try:
            for query in plan.queries:
                if searches_used >= plan.max_search_queries:
                    break
                # 硬预算在外部 IO 前递增；Tool Gateway 仍需 durable quota 二次兜底。
                searches_used += 1
                batch = await self._searcher.search(
                    run.tenant_id,
                    run.run_id,
                    query.query,
                    query.country,
                    query.category,
                    query.limit,
                )
                try:
                    for index in range(len(batch.results)):
                        if pages_used >= plan.max_pages_read:
                            break
                        pages_used += 1
                        try:
                            snapshot = await self._page_reader.read_page(
                                run.tenant_id,
                                run.run_id,
                                batch,
                                index,
                            )
                        except ToolGatewayError as error:
                            if error.category in {
                                ToolErrorCategory.PROVIDER_PERMANENT,
                                ToolErrorCategory.VALIDATION,
                            }:
                                continue
                            raise
                        pages.append(
                            {
                                "text": snapshot.text,
                                "url": snapshot.url,
                                "observed_at": snapshot.observed_at,
                                "content_hash": snapshot.content_hash,
                                "snapshot_artifact_ref": str(
                                    snapshot.snapshot_artifact_ref
                                ),
                            }
                        )
                finally:
                    self._searcher.release(batch)
                if pages_used >= plan.max_pages_read:
                    break
        except BaseException:
            self._searcher.discard_all()
            raise
        if not pages:
            return (
                "complete",
                None,
                {
                    "searches_used": searches_used,
                    "pages_used": pages_used,
                    "signal_count": 0,
                    "hypothesis_count": 0,
                    "completion_reason": "no_readable_pages",
                },
            )
        change_set = await self._capability.run(
            AgentTask(
                tenant_id=run.tenant_id,
                run_id=run.run_id,
                acting_user=acting_user,
                objective=plan.objective,
                inputs={
                    "pages": tuple(pages),
                    "target_countries": plan.target_countries,
                    "target_categories": plan.target_categories,
                    "excluded_countries": plan.excluded_countries,
                    "excluded_categories": plan.excluded_categories,
                    "max_signals": plan.max_signals,
                    "max_hypotheses": plan.max_hypotheses,
                    "strategy_group": plan.strategy_group,
                },
            ),
            context=None,
        )
        if change_set.tenant_id != run.tenant_id or change_set.run_id != run.run_id:
            raise TenantIsolationViolation("需求探索 ChangeSet 租户或 run 不一致")
        signal_changes, hypothesis_changes = _split_changes(change_set.changes)
        if (
            len(signal_changes) > plan.max_signals
            or len(hypothesis_changes) > plan.max_hypotheses
        ):
            raise ValidationError("需求探索 ChangeSet 超过确认上限")
        signal_ids: list[str] = []
        for change in signal_changes:
            payload = _payload(change, "capture_signal")
            observed_at = _utc_datetime(payload.get("observed_at"))
            signal_ids.append(
                await self._demand.capture_signal(
                    run.tenant_id,
                    SignalCaptureRequest(
                        signal_type=_text(
                            payload.get("signal_type"), "需求信号类型无效", maximum=100
                        ),
                        entity_name=_text(
                            payload.get("entity_name"), "需求信号企业无效"
                        ),
                        raw_observation=_text(
                            payload.get("raw_observation"),
                            "需求信号观察无效",
                            maximum=2_000,
                        ),
                        possible_need=(
                            None
                            if payload.get("possible_need") is None
                            else _text(
                                payload.get("possible_need"),
                                "需求信号推断无效",
                                maximum=500,
                            )
                        ),
                        observed_at=observed_at,
                        source_type=_text(
                            payload.get("source_type"),
                            "需求信号来源类型无效",
                            maximum=64,
                        ),
                        source_id=_text(
                            payload.get("source_id"), "需求信号来源无效", maximum=200
                        ),
                        extracted_by=_text(
                            payload.get("extracted_by"),
                            "需求信号提取者无效",
                            maximum=128,
                        ),
                        source_url=_text(
                            payload.get("source_url"),
                            "需求信号 URL 无效",
                            maximum=2_000,
                        ),
                        page_hash=_text(
                            payload.get("page_hash"), "需求信号页面哈希无效", maximum=64
                        ),
                        snapshot_artifact_ref=_text(
                            payload.get("snapshot_artifact_ref"),
                            "需求信号网页快照引用无效",
                            maximum=40,
                        ),
                    ),
                )
            )
        hypothesis_ids: list[str] = []
        for change in hypothesis_changes:
            payload = _payload(change, "create_hypothesis")
            raw_indexes = payload.get("signal_indexes")
            if not isinstance(raw_indexes, (list, tuple)) or not raw_indexes:
                raise ValidationError("需求假设信号索引无效")
            indexes = tuple(dict.fromkeys(raw_indexes))
            if any(
                type(index) is not int or not 0 <= index < len(signal_ids)
                for index in indexes
            ):
                raise ValidationError("需求假设信号索引越界")
            refs = tuple(signal_ids[index] for index in indexes)
            account_name_index = payload.get("account_name_signal_index")
            country_index = payload.get("country_signal_index")
            if (
                type(account_name_index) is not int
                or type(country_index) is not int
                or account_name_index not in indexes
                or country_index not in indexes
            ):
                raise ValidationError("需求假设企业字段证据引用无效")
            entity_name = _text(payload.get("entity_name"), "需求假设企业无效")
            country = _text(payload.get("country"), "需求假设国家无效", maximum=64)
            name_signal = _payload(signal_changes[account_name_index], "capture_signal")
            country_signal = _payload(signal_changes[country_index], "capture_signal")
            account_id = await self._prospecting.resolve_account(
                run.tenant_id,
                AccountResolveRequest(
                    entity_name=entity_name,
                    country=country,
                    website_domain=_text(
                        payload.get("website_domain"),
                        "需求假设官网无效",
                        maximum=253,
                    ),
                    source_signal_refs=refs,
                    field_provenance={
                        "name": _account_field_provenance(
                            name_signal,
                            extracted_by="system:url-host-v1",
                            quote_key="source_url",
                        ),
                        "country": _account_field_provenance(
                            country_signal,
                            extracted_by=_text(
                                country_signal.get("extracted_by"),
                                "需求信号提取者无效",
                                maximum=128,
                            ),
                            quote_key="raw_observation",
                        ),
                    },
                ),
            )
            hypothesis_id = await self._demand.create_hypothesis(
                run.tenant_id,
                ProspectAccountId(account_id),
                _text(payload.get("category"), "需求假设品类无效"),
                list(refs),
                _text(
                    payload.get("reasoning"),
                    "需求假设理由无效",
                    maximum=2_000,
                ),
                _text(
                    payload.get("inferred_by"),
                    "需求假设推断者无效",
                    maximum=128,
                ),
            )
            hypothesis_ids.append(str(hypothesis_id))
        return (
            "advance",
            "generate_hypotheses",
            {
                "searches_used": searches_used,
                "pages_used": pages_used,
                "signal_ids": list(dict.fromkeys(signal_ids)),
                "hypothesis_ids": list(dict.fromkeys(hypothesis_ids)),
                "signal_count": len(signal_ids),
                "hypothesis_count": len(hypothesis_ids),
                "completion_reason": (
                    "budget_exhausted"
                    if searches_used >= plan.max_search_queries
                    or pages_used >= plan.max_pages_read
                    else "plan_completed"
                ),
            },
        )


def _account_field_provenance(
    signal: dict[str, object],
    *,
    extracted_by: str,
    quote_key: str,
) -> Provenance:
    """从字段显式引用的单条 signal 构造原样来源，不借用其他网页证据。"""
    page_hash = _text(signal.get("page_hash"), "需求信号页面哈希无效", maximum=64)
    return Provenance(
        source_type=SourceType.WEB_PAGE,
        source_id=page_hash,
        extracted_by=extracted_by,
        extracted_at=_utc_datetime(signal.get("observed_at")),
        source_url=_text(signal.get("source_url"), "需求信号 URL 无效", maximum=2_000),
        page_hash=page_hash,
        source_quote=_text(
            signal.get(quote_key), "需求信号字段证据无效", maximum=2_000
        ),
    )


class GenerateHypothesesStep:
    """持久化后证据完整性检查；正文和模型输出不跨步骤。"""

    def __init__(self, demand: DemandService) -> None:
        self._demand = demand

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        _base_context(run)
        raw_ids = run.context.get("hypothesis_ids")
        if not isinstance(raw_ids, list):
            raise ValidationError("需求探索假设集合无效")
        tiers: dict[str, str] = {}
        for raw_id in dict.fromkeys(raw_ids):
            hypothesis_id = NeedHypothesisId(
                _text(raw_id, "需求探索假设标识无效", maximum=40)
            )
            confidence = await self._demand.get_confidence(run.tenant_id, hypothesis_id)
            tiers[str(hypothesis_id)] = confidence.tier.value
        return (
            "advance",
            "score_and_queue",
            {"confidence_tiers": tiers},
        )


class ScoreAndQueueStep:
    def __init__(
        self,
        task_reader: DemandDiscoveryTaskReader,
        demand: DemandService,
        queue: AccountDiscoveryQueue,
    ) -> None:
        self._task_reader = task_reader
        self._demand = demand
        self._queue = queue

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        plan, acting_user = await _confirmed_plan(run, self._task_reader)
        minimum = ConfidenceTier(plan.minimum_confidence_tier)
        raw_ids = run.context.get("hypothesis_ids")
        if not isinstance(raw_ids, list):
            raise ValidationError("需求探索假设集合无效")
        queued_hypotheses: list[str] = []
        account_runs: list[str] = []
        for raw_id in dict.fromkeys(raw_ids):
            hypothesis_id = NeedHypothesisId(
                _text(raw_id, "需求探索假设标识无效", maximum=40)
            )
            confidence = await self._demand.get_confidence(run.tenant_id, hypothesis_id)
            if not meets_threshold(confidence, minimum):
                continue
            child = await self._queue.start(
                run.tenant_id,
                hypothesis_id,
                campaign_id=plan.campaign_id,
                acting_user=acting_user,
                role_hints=plan.role_hints,
                assessment_ref=plan.assessment_ref,
            )
            queued_hypotheses.append(str(hypothesis_id))
            account_runs.append(str(child))
        return (
            "complete",
            None,
            {
                "queued_hypothesis_ids": queued_hypotheses,
                "account_discovery_run_ids": account_runs,
                "queued_count": len(account_runs),
            },
        )


def _split_changes(
    changes: object,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    if not isinstance(changes, list):
        raise ValidationError("需求探索 ChangeSet 无效")
    signals: list[dict[str, object]] = []
    hypotheses: list[dict[str, object]] = []
    seen_hypothesis = False
    for item in changes:
        if not isinstance(item, dict):
            raise ValidationError("需求探索 ChangeSet 无效")
        if (
            item.get("domain") != "demand"
            or item.get("risk_level") != "low"
            or item.get("operation") not in {"capture_signal", "create_hypothesis"}
        ):
            raise ValidationError("需求探索 ChangeSet 越界")
        if item["operation"] == "capture_signal":
            if seen_hypothesis:
                raise ValidationError("需求探索 ChangeSet 顺序无效")
            signals.append(item)
        else:
            seen_hypothesis = True
            hypotheses.append(item)
    return signals, hypotheses


def _payload(change: dict[str, object], operation: str) -> dict[str, object]:
    if change.get("operation") != operation or not isinstance(
        change.get("payload"), dict
    ):
        raise ValidationError("需求探索 ChangeSet payload 无效")
    return cast(dict[str, object], change["payload"])


def _utc_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValidationError("需求信号观察时间无效")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValidationError("需求信号观察时间无效") from None
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise ValidationError("需求信号观察时间无效")
    return parsed.astimezone(UTC)


def _string_tuple(
    values: object,
    *,
    maximum: int,
    limit: int,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if type(values) is not tuple or len(values) > limit:
        raise ValidationError("需求探索集合无效")
    result = tuple(
        dict.fromkeys(
            _text(value, "需求探索集合无效", maximum=maximum) for value in values
        )
    )
    if not allow_empty and not result:
        raise ValidationError("需求探索集合无效")
    return result


__all__ = (
    "ExecuteSearchStep",
    "GenerateHypothesesStep",
    "PlanSearchStep",
    "ScoreAndQueueStep",
)
