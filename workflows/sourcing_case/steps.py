"""Sourcing Case V2 的内部梯子、产品准备与显式等待步骤。"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Any

from domains.products.service import (
    ProductActor,
    ProductMatchResult,
    ProductService,
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
    ValidatedNeedId,
)
from workflows.engine.runner import WorkflowRun
from workflows.sourcing_case.ports import SourcingNeedReader

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
        required_names = tuple(item.spec_name for item in requirements)
        for match in sorted(
            product_result.qualified_matches,
            key=lambda item: str(item.product.product_id),
        ):
            if not isinstance(match, QualifiedProductMatch):
                raise ValidationError("内部产品匹配缺少逐项规格比较")
            product = match.product
            if product.tenant_id != run.tenant_id:
                raise ValidationError("内部产品匹配结果租户无效")
            comparison_names = tuple(item.spec_name for item in match.spec_comparisons)
            if (
                comparison_names != tuple(sorted(required_names))
                or len(set(comparison_names)) != len(comparison_names)
                or any(
                    item.level is not ProductSpecMatchLevel.EXACT
                    or item.evidence_ref is None
                    for item in match.spec_comparisons
                )
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
                    )
                    for item in matches[0].spec_comparisons
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


class FixedWaitStep:
    """后续 Task 的显式无副作用占位，不伪造任何完成事实。"""

    def __init__(self, status: str) -> None:
        self._status = _text(status, "寻源等待状态无效")

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        _base(run)
        return ("wait", None, {"sourcing_wait_status": self._status})


__all__ = (
    "AwaitProductCardsStep",
    "FixedWaitStep",
    "InternalMatchLadderStep",
    "PrepareCandidatesStep",
)
