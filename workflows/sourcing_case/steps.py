"""Sourcing Case V2 的内部梯子、产品准备与显式等待步骤。"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

from agent_runtime.sourcing_agent import SourcingPageCandidateDraft
from domains.products.service import (
    ProductActor,
    ProductMatchResult,
    ProductService,
    ProductSpecComparison,
    ProductSpecFact,
    ProductSpecMatchLevel,
    ProductSpecRequirement,
    QualifiedProductMatch,
)
from domains.sourcing.service import (
    LadderCheck,
    LadderOutcome,
    MatchLadderRung,
    SourcingActor,
    SourcingNeedSnapshot,
    SourcingService,
    SpecComparison,
    SpecMatchLevel,
)
from domains.suppliers.service import SupplierActor, SupplierService
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import (
    EmployeeId,
    ProductId,
    SourcingCaseId,
    SourcingPlanId,
    ValidatedNeedId,
)
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.free_search_contracts import (
    FreeSearchError,
    FreeSearchStopReason,
    SearchQuotaRepository,
)
from workflows.engine.runner import WorkflowRun
from workflows.sourcing_case.ports import (
    AuthorizedPublicSourcingPlanReader,
    PersistedSearchReceiptPort,
    PublicCandidateDraftWriter,
    PublicCandidateExtractor,
    PublicPageReader,
    PublicSourcingSearcher,
    SourcingNeedReader,
)

_PRODUCT_CONCLUSIONS = {
    1: ("no_qualified_catalog_exact", "qualified_catalog_exact"),
    2: ("no_qualified_catalog_modifiable", "qualified_catalog_modifiable"),
    3: ("no_qualified_candidate_product", "qualified_candidate_product"),
}


def _raise_dependency_error(
    *, transient: bool, failed: bool, transient_message: str, permanent_message: str
) -> None:
    """在 ``except`` 外按可重试性抛固定错误，避免保留下层异常链或 context。"""

    if transient:
        raise TransientError(transient_message)
    if failed:
        raise ValidationError(permanent_message)


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


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _base(run: WorkflowRun) -> tuple[SourcingCaseId, ValidatedNeedId, str]:
    case_id = SourcingCaseId(_text(run.context.get("case_id"), "寻源流程缺少 Case ID"))
    if run.subject_ref != str(case_id):
        raise ValidationError("寻源流程 subject 与 Case 不一致")
    need_id = ValidatedNeedId(_text(run.context.get("need_id"), "寻源流程缺少 Need ID"))
    snapshot_hash = _text(
        run.context.get("need_snapshot_hash"),
        "寻源流程缺少需求快照哈希",
        maximum=64,
    )
    if len(snapshot_hash) != 64 or any(
        ch not in "0123456789abcdef" for ch in snapshot_hash
    ):
        raise ValidationError("寻源流程需求快照哈希无效")
    return case_id, need_id, snapshot_hash


async def _trusted_need(
    run: WorkflowRun, reader: SourcingNeedReader
) -> SourcingNeedSnapshot:
    _case_id, need_id, snapshot_hash = _base(run)
    failed = False
    transient = False
    snapshot: SourcingNeedSnapshot | None = None
    try:
        snapshot = await reader.read(run.tenant_id, need_id)
    except TransientError:
        transient = True
    except Exception:  # noqa: BLE001 - 丢弃 reader 自由异常，防止写入 run.last_error
        failed = True
    _raise_dependency_error(
        transient=transient,
        failed=failed,
        transient_message="可信寻源需求快照暂不可用",
        permanent_message="可信寻源需求快照读取失败",
    )
    if (
        not isinstance(snapshot, SourcingNeedSnapshot)
        or snapshot.need_id != need_id
        or snapshot.snapshot_hash != snapshot_hash
    ):
        raise ValidationError("可信寻源需求快照与 Workflow 不一致")
    return snapshot


def _category_and_keywords(
    snapshot: SourcingNeedSnapshot,
) -> tuple[str, list[str]]:
    if not isinstance(snapshot.product_category.value, str):
        raise ValidationError("可信寻源需求品类必须是文本")
    category = _normalize(snapshot.product_category.value)
    if not category:
        raise ValidationError("可信寻源需求品类不能为空")
    keywords = sorted(
        {
            normalized
            for fact in (snapshot.application, snapshot.material, snapshot.size_spec)
            if fact is not None and isinstance(fact.value, str)
            if (normalized := _normalize(fact.value))
        }
    )
    return category, keywords


def _required_specs(
    snapshot: SourcingNeedSnapshot,
) -> tuple[ProductSpecRequirement, ...]:
    facts = (
        ("product_category", snapshot.product_category),
        ("application", snapshot.application),
        ("material", snapshot.material),
        ("size_spec", snapshot.size_spec),
    )
    requirements: list[ProductSpecRequirement] = []
    for name, fact in facts:
        if fact is None:
            continue
        if not isinstance(fact.value, str):
            raise ValidationError("可信寻源产品规格必须是文本")
        value = _normalize(fact.value)
        if not value:
            raise ValidationError("可信寻源产品规格不能为空")
        requirements.append(ProductSpecRequirement(name, value))
    quantity = snapshot.quantity.value
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise ValidationError("可信寻源需求数量必须是正整数")
    requirements.append(ProductSpecRequirement("moq", str(quantity)))
    if snapshot.unit is not None:
        if not isinstance(snapshot.unit.value, str):
            raise ValidationError("可信寻源计价单位必须是文本")
        unit = _normalize(snapshot.unit.value)
        if not unit:
            raise ValidationError("可信寻源计价单位不能为空")
        requirements.append(ProductSpecRequirement("unit", unit))
    return tuple(requirements)


def _stable_check_id(
    case_id: SourcingCaseId,
    rung: int,
    outcome: LadderOutcome,
    match_object_id: str | None,
) -> str:
    payload = json.dumps(
        [str(case_id), rung, outcome.value, match_object_id],
        separators=(",", ":"),
    ).encode()
    return f"slc_{hashlib.sha256(payload).hexdigest()[:26]}"


def _ladder_check(
    *,
    run: WorkflowRun,
    snapshot: SourcingNeedSnapshot,
    actor: SourcingActor,
    rung: int,
    outcome: LadderOutcome,
    conclusion: str,
    input_snapshot: dict[str, object],
    match_object_type: str | None = None,
    match_object_id: str | None = None,
    evidence_refs: tuple[str, ...] = (),
    spec_comparisons: tuple[SpecComparison, ...] = (),
) -> LadderCheck:
    case_id, _need_id, _hash = _base(run)
    return LadderCheck(
        check_id=_stable_check_id(case_id, rung, outcome, match_object_id),
        tenant_id=run.tenant_id,
        case_id=case_id,
        sequence_number=rung,
        rung=MatchLadderRung(rung),
        outcome=outcome,
        input_snapshot=input_snapshot,
        input_snapshot_hash=snapshot.snapshot_hash,
        conclusion=conclusion,
        match_object_type=match_object_type,
        match_object_id=match_object_id,
        spec_comparisons=spec_comparisons,
        evidence_refs=evidence_refs,
        checked_by=EmployeeId(actor.actor_id),
        checked_at=run.created_at,
    )


class InternalMatchLadderStep:
    """一次读取内部产品/供应商服务，并严格按梯级 1–5 记录事实。"""

    def __init__(
        self,
        *,
        need_reader: SourcingNeedReader,
        products: ProductService,
        suppliers: SupplierService,
        sourcing: SourcingService,
        product_actor: ProductActor,
        supplier_actor: SupplierActor,
        sourcing_actor: SourcingActor,
    ) -> None:
        self._need_reader = need_reader
        self._products = products
        self._suppliers = suppliers
        self._sourcing = sourcing
        self._product_actor = product_actor
        self._supplier_actor = supplier_actor
        self._sourcing_actor = sourcing_actor

    async def _record(self, run: WorkflowRun, check: LadderCheck) -> None:
        failed = False
        transient = False
        try:
            await self._sourcing.record_ladder_check(
                run.tenant_id,
                SourcingCaseId(run.subject_ref),
                check,
                actor=self._sourcing_actor,
            )
        except TransientError:
            transient = True
        except Exception:  # noqa: BLE001 - 域/仓储异常原文不得进入 run.last_error
            failed = True
        _raise_dependency_error(
            transient=transient,
            failed=failed,
            transient_message="寻源内部匹配记录暂不可用",
            permanent_message="寻源内部匹配记录失败",
        )

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        snapshot = await _trusted_need(run, self._need_reader)
        category, keywords = _category_and_keywords(snapshot)
        requirements = _required_specs(snapshot)
        required_by_name: dict[str, str] = {}
        for requirement in requirements:
            spec_name = _normalize(requirement.spec_name)
            required = _normalize(requirement.required)
            if not spec_name or not required or spec_name in required_by_name:
                raise ValidationError("可信寻源需求规格必须规范化后唯一且非空")
            required_by_name[spec_name] = required
        required_names = tuple(sorted(required_by_name))
        failed = False
        transient = False
        product_result: ProductMatchResult | None = None
        try:
            product_result = await self._products.search_for_matching(
                run.tenant_id,
                category,
                keywords,
                requirements,
                actor=self._product_actor,
            )
        except TransientError:
            transient = True
        except Exception:  # noqa: BLE001 - 下层异常可能携带内部价格或连接信息
            failed = True
        _raise_dependency_error(
            transient=transient,
            failed=failed,
            transient_message="寻源内部产品匹配暂不可用",
            permanent_message="寻源内部产品匹配失败",
        )
        if not isinstance(product_result, ProductMatchResult):
            raise ValidationError("寻源内部产品匹配结果无效")

        buckets: dict[int, list[QualifiedProductMatch]] = {1: [], 2: [], 3: []}
        for match in sorted(
            product_result.qualified_matches,
            key=lambda item: str(item.product.product_id),
        ):
            if not isinstance(match, QualifiedProductMatch):
                raise ValidationError("内部产品匹配缺少逐项规格比较")
            product = match.product
            if product.tenant_id != run.tenant_id:
                raise ValidationError("内部产品匹配结果租户无效")
            product_facts: dict[str, ProductSpecFact] = {}
            for raw_name, fact in sorted(product.match_specs.items()):
                fact_name = _normalize(raw_name)
                if (
                    not fact_name
                    or fact_name in product_facts
                    or not isinstance(fact, ProductSpecFact)
                    or not isinstance(fact.value, str)
                    or not _normalize(fact.value)
                    or fact.evidence_ref is None
                    or not str(fact.evidence_ref).strip()
                ):
                    raise ValidationError("内部产品匹配规格证明不完整")
                product_facts[fact_name] = fact
            comparison_names: list[str] = []
            for comparison in match.spec_comparisons:
                if (
                    not isinstance(comparison, ProductSpecComparison)
                    or not isinstance(comparison.spec_name, str)
                    or not isinstance(comparison.required, str)
                    or not isinstance(comparison.offered, str)
                    or comparison.level is not ProductSpecMatchLevel.EXACT
                    or comparison.evidence_ref is None
                    or not str(comparison.evidence_ref).strip()
                ):
                    raise ValidationError("内部产品匹配规格证明不完整")
                spec_name = _normalize(comparison.spec_name)
                matched_fact = product_facts.get(spec_name)
                if (
                    not spec_name
                    or not _normalize(comparison.offered)
                    or _normalize(comparison.required)
                    != required_by_name.get(spec_name)
                    or matched_fact is None
                    or _normalize(comparison.offered)
                    != _normalize(matched_fact.value)
                    or str(comparison.evidence_ref)
                    != str(matched_fact.evidence_ref)
                ):
                    raise ValidationError("内部产品匹配规格证明不完整")
                comparison_names.append(spec_name)
            if (
                tuple(comparison_names) != required_names
                or len(set(comparison_names)) != len(comparison_names)
            ):
                raise ValidationError("内部产品匹配规格证明不完整")
            pool = getattr(product.pool, "value", None)
            if pool == "formal":
                buckets[2 if product.customizable else 1].append(match)
            elif pool == "candidate":
                buckets[3].append(match)

        finding_rows = sorted(
            (
                {
                    "product_id": str(item.product_id),
                    "code": item.code,
                    "missing_fields": list(item.missing_fields),
                }
                for item in product_result.findings
            ),
            key=lambda item: str(item["product_id"]),
        )
        for rung in (1, 2, 3):
            matches = buckets[rung]
            product_ids = [str(item.product.product_id) for item in matches]
            input_snapshot: dict[str, object] = {
                "need_id": str(snapshot.need_id),
                "category": category,
                "qualified_product_ids": product_ids,
                "excluded_product_findings": finding_rows,
            }
            if matches:
                input_snapshot["product_spec_evidence"] = {
                    str(match.product.product_id): {
                        _normalize(comparison.spec_name): str(
                            comparison.evidence_ref
                        )
                        for comparison in match.spec_comparisons
                    }
                    for match in matches
                }
                evidence_refs = tuple(
                    sorted(
                        {
                            str(item.internal_cost_source_ref)
                            for match in matches
                            for item in (match.product,)
                            if item.internal_cost_source_ref is not None
                        }
                        | {
                            str(comparison.evidence_ref)
                            for match in matches
                            for comparison in match.spec_comparisons
                            if comparison.evidence_ref is not None
                        }
                    )
                )
                comparisons = tuple(
                    SpecComparison(
                        spec_name=item.spec_name,
                        required=item.required,
                        offered=item.offered,
                        level=SpecMatchLevel.EXACT,
                        product_id=match.product.product_id,
                        evidence_ref=item.evidence_ref,
                    )
                    for match in matches
                    for item in match.spec_comparisons
                )
                await self._record(
                    run,
                    _ladder_check(
                        run=run,
                        snapshot=snapshot,
                        actor=self._sourcing_actor,
                        rung=rung,
                        outcome=LadderOutcome.QUALIFIED_SUPPLY_FOUND,
                        conclusion=_PRODUCT_CONCLUSIONS[rung][1],
                        input_snapshot=input_snapshot,
                        match_object_type="product",
                        match_object_id=product_ids[0],
                        evidence_refs=evidence_refs,
                        spec_comparisons=comparisons,
                    ),
                )
                return (
                    "advance",
                    "prepare_candidates",
                    {
                        "internal_product_ids": product_ids,
                        "supplier_candidate_ids": [],
                    },
                )
            await self._record(
                run,
                _ladder_check(
                    run=run,
                    snapshot=snapshot,
                    actor=self._sourcing_actor,
                    rung=rung,
                    outcome=LadderOutcome.NO_QUALIFIED_SUPPLY,
                    conclusion=_PRODUCT_CONCLUSIONS[rung][0],
                    input_snapshot=input_snapshot,
                ),
            )

        tags = sorted({category, *keywords})
        supplier_failed = False
        supplier_transient = False
        suppliers: list[Any] = []
        try:
            suppliers = await self._suppliers.search_by_capability(
                run.tenant_id, tags, actor=self._supplier_actor
            )
        except TransientError:
            supplier_transient = True
        except Exception:  # noqa: BLE001 - 下层异常可能携带供应商敏感内容
            supplier_failed = True
        _raise_dependency_error(
            transient=supplier_transient,
            failed=supplier_failed,
            transient_message="寻源内部供应商能力匹配暂不可用",
            permanent_message="寻源内部供应商能力匹配失败",
        )
        ordered_suppliers = sorted(suppliers, key=lambda item: str(item.supplier_id))
        if any(item.tenant_id != run.tenant_id for item in ordered_suppliers):
            raise ValidationError("内部供应商能力匹配结果租户无效")
        supplier_ids = [str(item.supplier_id) for item in ordered_suppliers]
        for rung, conclusion in (
            (4, "supplier_similar_lead_not_qualified"),
            (5, "supplier_custom_lead_not_qualified"),
        ):
            await self._record(
                run,
                _ladder_check(
                    run=run,
                    snapshot=snapshot,
                    actor=self._sourcing_actor,
                    rung=rung,
                    outcome=LadderOutcome.NO_QUALIFIED_SUPPLY,
                    conclusion=conclusion,
                    input_snapshot={
                        "need_id": str(snapshot.need_id),
                        "category": category,
                        "supplier_lead_ids": supplier_ids,
                    },
                    match_object_type=("supplier_capability" if supplier_ids else None),
                    match_object_id=(supplier_ids[0] if supplier_ids else None),
                ),
            )
        return (
            "advance",
            "await_public_plan",
            {"internal_product_ids": [], "supplier_candidate_ids": []},
        )


class PrepareCandidatesStep:
    """把内部匹配产品登记为 canonical Option，并最终发布 Ready。"""

    def __init__(
        self, *, sourcing: SourcingService, sourcing_actor: SourcingActor
    ) -> None:
        self._sourcing = sourcing
        self._sourcing_actor = sourcing_actor

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        case_id, _need_id, _hash = _base(run)
        raw_ids = run.context.get("internal_product_ids")
        candidate_ids = run.context.get("supplier_candidate_ids")
        if (
            not isinstance(raw_ids, list)
            or not raw_ids
            or raw_ids != sorted(set(raw_ids))
            or any(not isinstance(item, str) or not item.strip() for item in raw_ids)
            or candidate_ids != []
        ):
            raise ValidationError("内部候选准备上下文无效")
        option_ids = []
        failed = False
        transient = False
        try:
            for product_id in raw_ids:
                option_ids.append(
                    await self._sourcing.register_existing_product_option(
                        run.tenant_id,
                        case_id,
                        ProductId(product_id),
                        actor=self._sourcing_actor,
                    )
                )
            await self._sourcing.mark_candidates_ready(
                run.tenant_id,
                case_id,
                tuple(option_ids),
                (),
                actor=self._sourcing_actor,
            )
        except TransientError:
            transient = True
        except Exception:  # noqa: BLE001 - 只向 workflow 暴露固定失败分类
            failed = True
        _raise_dependency_error(
            transient=transient,
            failed=failed,
            transient_message="寻源内部产品准备暂不可用",
            permanent_message="寻源内部产品准备失败",
        )
        return (
            "advance",
            "await_product_cards",
            {
                "option_ids": [str(item) for item in option_ids],
                "supplier_candidate_ids": [],
            },
        )


class AwaitProductCardsStep:
    """内部路径无 Supplier Candidate，直接进入人工审核等待。"""

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        _base(run)
        candidate_ids = run.context.get("supplier_candidate_ids")
        internal_ids = run.context.get("internal_product_ids")
        option_ids = run.context.get("option_ids")
        if (
            candidate_ids == []
            and isinstance(internal_ids, list)
            and internal_ids
            and isinstance(option_ids, list)
            and option_ids
        ):
            return ("advance", "await_review", {})
        if not isinstance(candidate_ids, list) or any(
            not isinstance(item, str) or not item.strip() for item in candidate_ids
        ):
            raise ValidationError("候选产品卡等待上下文无效")
        return ("wait", None, {})


def sourcing_search_request_key(plan_hash: str, query_index: int) -> str:
    """绑定已授权计划与查询位置的免费额度操作键。"""

    if (
        not isinstance(plan_hash, str)
        or len(plan_hash) != 64
        or any(character not in "0123456789abcdef" for character in plan_hash)
        or isinstance(query_index, bool)
        or not isinstance(query_index, int)
        or query_index < 0
    ):
        raise ValidationError("公开寻源查询操作键绑定无效")
    payload = (
        b"tradeos:sourcing-search:v1\0"
        + plan_hash.encode("ascii")
        + b"\0"
        + str(query_index).encode("ascii")
    )
    return hashlib.sha256(payload).hexdigest()


_TOOL_STOP_REASONS = {
    ToolErrorCategory.PAGE_ACCESS_FORBIDDEN: "page_access_forbidden",
    ToolErrorCategory.LOGIN_OR_CAPTCHA: "login_or_captcha",
    ToolErrorCategory.UNSAFE_REDIRECT: "unsafe_redirect",
    ToolErrorCategory.RATE_LIMITED: "provider_rate_limited",
    ToolErrorCategory.PROVIDER_TRANSIENT: "provider_timeout",
    ToolErrorCategory.RECONCILIATION_REQUIRED: "reconciliation_required",
}

_FREE_STOP_REASONS = {
    FreeSearchStopReason.QUOTA_EXHAUSTED: "quota_exhausted",
    FreeSearchStopReason.USAGE_UNKNOWN: "quota_status_unknown",
    FreeSearchStopReason.PAID_ENABLED: "paid_usage_enabled",
    FreeSearchStopReason.REQUEST_UNCERTAIN: "reconciliation_required",
    FreeSearchStopReason.UNSUPPORTED: "quota_status_unknown",
}


class PublicSearchStep:
    """已授权计划下的有界公开寻源；只生成未核验草稿。"""

    def __init__(
        self,
        *,
        need_reader: SourcingNeedReader,
        plan_reader: AuthorizedPublicSourcingPlanReader,
        quota: SearchQuotaRepository,
        searcher: PublicSourcingSearcher,
        page_reader: PublicPageReader,
        receipts: PersistedSearchReceiptPort,
        extractor: PublicCandidateExtractor,
        drafts: PublicCandidateDraftWriter,
    ) -> None:
        self._need_reader = need_reader
        self._plan_reader = plan_reader
        self._quota = quota
        self._searcher = searcher
        self._page_reader = page_reader
        self._receipts = receipts
        self._extractor = extractor
        self._drafts = drafts

    @staticmethod
    def _wait(reason: str, *, searches: int, pages: int) -> tuple[str, None, dict[str, Any]]:
        return (
            "wait",
            None,
            {
                "sourcing_stop_reason": reason,
                "sourcing_searches_used": searches,
                "sourcing_pages_used": pages,
            },
        )

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        case_id, _need_id, _snapshot_hash = _base(run)
        plan_id = SourcingPlanId(
            _text(run.context.get("sourcing_plan_id"), "寻源计划 ID 无效", maximum=40)
        )
        plan_hash = _text(
            run.context.get("sourcing_plan_hash"), "寻源计划哈希无效", maximum=64
        )
        need = await _trusted_need(run, self._need_reader)
        try:
            plan = await self._plan_reader.load_authorized(
                tenant_id=run.tenant_id,
                case_id=case_id,
                run_id=run.run_id,
                plan_id=plan_id,
                plan_hash=plan_hash,
            )
        except TransientError:
            raise TransientError("已授权公开寻源计划暂不可用") from None
        except Exception:  # noqa: BLE001 - 下层异常原文不得进入 Workflow。
            raise ValidationError("已授权公开寻源计划读取失败") from None
        if (
            plan.tenant_id != run.tenant_id
            or plan.case_id != case_id
            or plan.plan_id != plan_id
            or plan.plan_hash != plan_hash
            or plan.authorized_plan_hash != plan_hash
            or plan.status.value != "running"
            or plan.provider != "tavily"
            or plan.search_depth != "basic"
        ):
            raise ValidationError("已授权公开寻源计划绑定无效")

        searches_used = 0
        pages_used = 0
        result_count = 0
        verifiable_count = 0
        draft_ids: list[str] = []
        rejected_page_reason: str | None = None
        try:
            for query_index, query in enumerate(plan.queries):
                if searches_used >= plan.max_search_queries or pages_used >= plan.max_pages_read:
                    break
                request_key = sourcing_search_request_key(plan_hash, query_index)
                batch = await self._receipts.restore(
                    tenant_id=run.tenant_id,
                    run_id=run.run_id,
                    plan_hash=plan_hash,
                    query_index=query_index,
                )
                try:
                    if batch is None:
                        if await self._quota.get(run.run_id, request_key) is not None:
                            return self._wait(
                                "reconciliation_required",
                                searches=searches_used,
                                pages=pages_used,
                            )
                        try:
                            batch = await self._searcher.search(
                                run.tenant_id,
                                run.run_id,
                                query.query_text,
                                query.target_country,
                                plan.product_category,
                                min(20, plan.max_pages_read - pages_used),
                                quota_request_key=request_key,
                            )
                        except FreeSearchError as error:
                            if error.reason is FreeSearchStopReason.REQUEST_UNCERTAIN:
                                await self._receipts.record_uncertain(
                                    tenant_id=run.tenant_id,
                                    case_id=case_id,
                                    run_id=run.run_id,
                                    plan_id=plan_id,
                                    plan_hash=plan_hash,
                                    query_index=query_index,
                                    request_key=request_key,
                                    query_hash=hashlib.sha256(
                                        query.query_text.encode()
                                    ).hexdigest(),
                                )
                            return self._wait(
                                _FREE_STOP_REASONS[error.reason],
                                searches=searches_used,
                                pages=pages_used,
                            )
                        except ToolGatewayError as error:
                            return self._wait(
                                _TOOL_STOP_REASONS.get(
                                    error.category, "provider_timeout"
                                ),
                                searches=searches_used,
                                pages=pages_used,
                            )
                        searches_used += 1
                        try:
                            await self._receipts.commit_locator_receipt(
                                tenant_id=run.tenant_id,
                                case_id=case_id,
                                run_id=run.run_id,
                                plan_id=plan_id,
                                plan_hash=plan_hash,
                                query_index=query_index,
                                request_key=request_key,
                                query_hash=hashlib.sha256(
                                    query.query_text.encode()
                                ).hexdigest(),
                                batch=batch,
                            )
                        except Exception:  # noqa: BLE001 - 持久异常可能含 locator。
                            raise ValidationError("公开寻源安全回执保存失败") from None
                    else:
                        searches_used += 1
                    result_count += len(batch.results)
                    for result_index in range(
                        min(len(batch.results), plan.max_pages_read - pages_used)
                    ):
                        try:
                            pages_used += 1
                            page = await self._page_reader.read_page(
                                run.tenant_id, run.run_id, batch, result_index
                            )
                            draft = await self._extractor.extract(need, page)
                            if not isinstance(draft, SourcingPageCandidateDraft):
                                raise ValidationError("公开寻源抽取草稿无效")
                            try:
                                draft_id = await self._drafts.save(
                                    tenant_id=run.tenant_id,
                                    case_id=case_id,
                                    run_id=run.run_id,
                                    plan_id=plan_id,
                                    plan_hash=plan_hash,
                                    query_index=query_index,
                                    result_index=result_index,
                                    draft=draft,
                                )
                            except Exception:  # noqa: BLE001 - 存储异常不得带原文。
                                raise ValidationError("公开寻源安全草稿保存失败") from None
                            draft_ids.append(draft_id)
                            if draft.supplier_name is not None:
                                verifiable_count += 1
                        except ToolGatewayError as error:
                            rejected_page_reason = _TOOL_STOP_REASONS.get(
                                error.category, "page_access_forbidden"
                            )
                            continue
                finally:
                    if batch is not None:
                        self._searcher.release(batch)
        finally:
            self._searcher.discard_all()

        if result_count == 0:
            return self._wait("no_search_results", searches=searches_used, pages=pages_used)
        if not draft_ids and rejected_page_reason is not None:
            return self._wait(rejected_page_reason, searches=searches_used, pages=pages_used)
        if verifiable_count == 0:
            return self._wait(
                "no_verifiable_supplier", searches=searches_used, pages=pages_used
            )
        return (
            "advance",
            "verify_candidates",
            {
                "sourcing_searches_used": searches_used,
                "sourcing_pages_used": pages_used,
                "supplier_candidate_draft_ids": draft_ids,
            },
        )


class FixedWaitStep:
    """后续 Task 的显式无副作用占位，不伪造任何完成事实。"""

    def __init__(self, status: str) -> None:
        self._status = _text(status, "寻源等待状态无效")

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        _base(run)
        return ("wait", None, {"sourcing_wait_status": self._status})


class AwaitPublicPlanStep:
    """入口只等待；收到精确授权事件后携带安全计划绑定推进公开搜索。"""

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        _base(run)
        event = run.context.get("event")
        if event is None:
            return ("wait", None, {"sourcing_wait_status": "approval_required"})
        if not isinstance(event, dict) or set(event) != {"event_type", "payload"}:
            raise ValidationError("公开寻源计划授权事件无效")
        if event.get("event_type") != "SourcingPlanConfirmed":
            raise ValidationError("公开寻源计划授权事件类型无效")
        payload = event.get("payload")
        if not isinstance(payload, dict) or set(payload) != {"plan_id", "plan_hash"}:
            raise ValidationError("公开寻源计划授权载荷无效")
        plan_id = _text(payload.get("plan_id"), "公开寻源计划 ID 无效", maximum=40)
        plan_hash = _text(
            payload.get("plan_hash"), "公开寻源计划哈希无效", maximum=64
        )
        if len(plan_hash) != 64 or any(
            character not in "0123456789abcdef" for character in plan_hash
        ):
            raise ValidationError("公开寻源计划哈希无效")
        return (
            "advance",
            "public_search",
            {
                "sourcing_plan_id": str(SourcingPlanId(plan_id)),
                "sourcing_plan_hash": plan_hash,
            },
        )


__all__ = (
    "AwaitProductCardsStep",
    "AwaitPublicPlanStep",
    "FixedWaitStep",
    "InternalMatchLadderStep",
    "PrepareCandidatesStep",
    "PublicSearchStep",
    "sourcing_search_request_key",
)
