"""Sourcing V2 的 tenant-bound、authorizer-first 领域服务实现。"""

from __future__ import annotations

import unicodedata
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from urllib.parse import urlsplit

from domains.sourcing.errors import (
    MissingEvidenceSnapshotError,
    SourcingHandoffInvariantError,
    SourcingPlanStaleError,
    SourcingThresholdNotMetError,
)
from domains.sourcing.models import (
    MAX_QUALIFIED_CANDIDATES,
    CaseState,
    EvidenceSnapshot,
    LadderCheck,
    LadderOutcome,
    MatchExplanation,
    MatchLadderRung,
    PriceRejectionReason,
    PublicPlanStatus,
    PublicSourcingPlan,
    SourcingCase,
    SourcingReconciliationStatus,
    SourcingReview,
    SourcingSearchExecution,
    SourcingSearchExecutionStatus,
    SourcingSearchReconciliation,
    SourcingStopCode,
    SourcingSupplyOption,
    SpecComparison,
    SpecMatchLevel,
    SupplierCandidate,
    SupplyOptionSource,
    candidate_set_hash,
)
from domains.sourcing.permissions import (
    SourcingAction,
    SourcingActor,
    SourcingAuthorizer,
    SourcingScope,
)
from domains.sourcing.repository import SourcingUnitOfWork
from domains.sourcing.schemas import (
    CandidateSubmission,
    CandidateView,
    CaseView,
    IndicativePriceTier,
    OpenSourcingCase,
    PublicCandidateDraft,
    PublicSourcingPlanCommand,
    SourcingCandidateProductInput,
    SourcingCandidateProductInputs,
    SourcingCandidateProductPriceInput,
    SourcingHandoffSnapshot,
    SourcingMatchInference,
    SourcingObservedFact,
    SourcingReviewCommand,
    SourcingUncertainReconciliationCommand,
    SpecComparisonView,
    VerifyPublicCandidateDraftsCommand,
    VerifyPublicCandidateDraftsResult,
)
from domains.sourcing.service import (
    CandidateEvidenceSnapshot,
    CandidateEvidenceSnapshotReader,
    ProviderUsageEvidenceReader,
    ProviderUsageEvidenceSnapshot,
    PublicSourcingRunView,
)
from shared.errors import InvalidStateTransition, ValidationError
from shared.events.catalog import (
    SourcingCandidatesReady,
    SourcingCandidatesVerified,
    SourcingCaseHandedToCosting,
    SourcingCaseOpened,
)
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    OpportunityId,
    ProductId,
    RunId,
    SourcingCaseId,
    SourcingPlanId,
    SourcingReviewId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
    TenantId,
    ValidatedNeedId,
    new_id,
)
from shared.schemas.money import CurrencyCode, Money
from shared.schemas.provenance import ProvenanceSummary, SourceType

_PUBLIC_DRAFT_VERIFIER = "public-draft-verifier-v1"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("寻源服务时间必须含时区")
    return value


def _normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _valid_candidate_evidence_projection(
    projection: CandidateEvidenceSnapshot,
) -> bool:
    """在打开事务前核对可信 reader 投影的确定性安全字段。"""

    try:
        parsed = urlsplit(projection.canonical_url)
        port = parsed.port
    except ValueError:
        return False
    return (
        projection.canonical_url == projection.canonical_url.strip()
        and parsed.scheme in {"http", "https"}
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and (port is None or 1 <= port <= 65_535)
        and projection.observed_at.tzinfo is not None
        and projection.observed_at.utcoffset() is not None
        and len(projection.content_hash) == 64
        and all(
            character in "0123456789abcdef" for character in projection.content_hash
        )
        and str(projection.artifact_id).startswith("art_")
        and str(projection.artifact_id) == str(projection.artifact_id).strip()
    )


async def _read_candidate_evidence(
    reader: CandidateEvidenceSnapshotReader,
    tenant_id: TenantId,
    artifact_id: ArtifactId,
) -> CandidateEvidenceSnapshot | None:
    """把不可信 reader 异常限制在本调用帧内，禁止泄露异常链。"""

    try:
        return await reader.read_verified(tenant_id, artifact_id)
    except Exception:  # noqa: BLE001 -- reader 边界必须吞掉全部不可信异常类型
        return None


async def _read_provider_usage_evidence(
    reader: ProviderUsageEvidenceReader | None,
    tenant_id: TenantId,
    artifact_id: ArtifactId,
) -> ProviderUsageEvidenceSnapshot | None:
    """丢弃受信 reader 边界外的自由异常，避免原因链进入日志。"""

    if reader is None:
        return None
    try:
        return await reader.read_verified(tenant_id, artifact_id)
    except Exception:  # noqa: BLE001 -- Provider 原始异常不得跨越领域边界
        return None


def _valid_provider_usage_evidence(
    projection: ProviderUsageEvidenceSnapshot,
    tenant_id: TenantId,
    artifact_id: ArtifactId,
) -> bool:
    return (
        isinstance(projection, ProviderUsageEvidenceSnapshot)
        and projection.tenant_id == tenant_id
        and projection.artifact_id == artifact_id
        and projection.provider == "tavily"
        and len(projection.content_hash) == 64
        and all(character in "0123456789abcdef" for character in projection.content_hash)
        and projection.observed_at.tzinfo is not None
        and projection.observed_at.utcoffset() is not None
    )


def _employee(actor: SourcingActor) -> EmployeeId:
    return EmployeeId(actor.actor_id)


def _case_required(case: SourcingCase | None) -> SourcingCase:
    if case is None:
        raise ValidationError("寻源案例不存在或租户不匹配")
    return case


def _quantity(case: SourcingCase) -> int:
    if case.need_snapshot is None:
        raise ValidationError("寻源案例缺少强类型需求快照")
    value = case.need_snapshot.quantity.value
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValidationError("需求数量必须是正整数")
    return value


def _qualified_internal_product_ids(
    checks: list[LadderCheck],
) -> tuple[ProductId, ...]:
    """从唯一合格内部梯级重建不可变 Product 全集；公开路径返回空集。"""

    qualified = tuple(
        item for item in checks if item.outcome is LadderOutcome.QUALIFIED_SUPPLY_FOUND
    )
    if not qualified:
        return ()
    if len(qualified) != 1 or qualified[0].match_object_type != "product":
        raise ValidationError("现有产品 Option 缺少唯一合格内部梯级事实")
    raw_ids = qualified[0].input_snapshot.get("qualified_product_ids")
    if (
        not isinstance(raw_ids, list)
        or not raw_ids
        or any(not isinstance(item, str) or not item.strip() for item in raw_ids)
        or raw_ids != sorted(set(raw_ids))
        or qualified[0].match_object_id != raw_ids[0]
    ):
        raise ValidationError("合格内部梯级的现有产品冻结集合无效")
    return tuple(ProductId(item) for item in raw_ids)


def _validate_product_evidence_mapping(check: LadderCheck) -> None:
    """独立封住 Product、逐项 comparison 与 Artifact 聚合之间的精确关系。"""

    product_ids = tuple(map(str, _qualified_internal_product_ids([check])))
    raw_mapping = check.input_snapshot.get("product_spec_evidence")
    if not isinstance(raw_mapping, dict) or set(raw_mapping) != set(product_ids):
        raise ValidationError("合格产品梯级证据映射与产品冻结集合不一致")
    evidence_refs = {
        str(item) for item in check.evidence_refs if str(item).strip()
    }
    comparisons_by_product: dict[str, dict[str, SpecComparison]] = {
        product_id: {} for product_id in product_ids
    }
    for comparison in check.spec_comparisons:
        product_id = (
            str(comparison.product_id)
            if comparison.product_id is not None
            else ""
        )
        spec_name = _normalize(comparison.spec_name)
        evidence_ref = (
            str(comparison.evidence_ref)
            if comparison.evidence_ref is not None
            else ""
        )
        product_comparisons = comparisons_by_product.get(product_id)
        if (
            product_comparisons is None
            or not spec_name
            or spec_name in product_comparisons
            or not evidence_ref
            or evidence_ref not in evidence_refs
        ):
            raise ValidationError("合格产品梯级证据映射与逐项比较不一致")
        product_comparisons[spec_name] = comparison
    expected_spec_names: set[str] | None = None
    for product_id in product_ids:
        product_mapping = raw_mapping.get(product_id)
        product_comparisons = comparisons_by_product[product_id]
        product_spec_names = set(product_comparisons)
        if (
            not isinstance(product_mapping, dict)
            or set(product_mapping) != product_spec_names
            or not product_comparisons
        ):
            raise ValidationError("合格产品梯级证据映射的规格集合不一致")
        if expected_spec_names is None:
            expected_spec_names = product_spec_names
        elif product_spec_names != expected_spec_names:
            raise ValidationError("合格产品梯级证据映射的规格集合不一致")
        for spec_name, evidence_ref in product_mapping.items():
            if (
                not isinstance(spec_name, str)
                or spec_name != _normalize(spec_name)
                or not isinstance(evidence_ref, str)
                or not evidence_ref.strip()
                or evidence_ref
                != str(product_comparisons[spec_name].evidence_ref)
                or evidence_ref not in evidence_refs
            ):
                raise ValidationError("合格产品梯级证据映射引用不一致")


def _candidate_from_submission(
    tenant_id: TenantId,
    case: SourcingCase,
    submission: CandidateSubmission,
    *,
    actor: SourcingActor,
    now: datetime,
    evidence: EvidenceSnapshot,
) -> SupplierCandidate:
    try:
        comparisons = [
            SpecComparison(
                spec_name=item.spec_name,
                required=item.required,
                offered=item.offered,
                level=SpecMatchLevel(item.level),
                substitutable=item.substitutable,
                substitution_impact=item.substitution_impact,
                needs_customer_confirmation=item.needs_customer_confirmation,
                customer_confirmation=item.customer_confirmation,
            )
            for item in submission.specs
        ]
    except ValueError as exc:
        raise ValidationError("候选规格匹配等级无效") from exc
    summary = "；".join(f"{item.spec_name}:{item.level.value}" for item in comparisons)
    return SupplierCandidate(
        candidate_id=SupplierCandidateId(new_id("spc")),
        tenant_id=tenant_id,
        case_id=case.case_id,
        supplier_name=submission.supplier_name,
        product_title=submission.product_title,
        created_at=now,
        source_platform=submission.source_platform,
        observed_facts=dict(submission.observed_facts),
        supplier_claims=dict(submission.supplier_claims),
        match_inferences=dict(submission.match_inferences),
        verified_specs=comparisons,
        indicative_price_tiers=tuple(submission.indicative_price_tiers),
        moq=submission.moq,
        price_unit=submission.price_unit,
        currency=submission.currency,
        evidence=evidence,
        evidence_snapshots=(evidence,),
        match=MatchExplanation(
            rung=MatchLadderRung.PUBLIC_SOURCING,
            comparisons=comparisons,
            summary=summary,
        ),
        verified_by=_employee(actor),
    )


def _public_draft_expected_specs(case: SourcingCase) -> tuple[tuple[str, str], ...]:
    """从冻结 Need 重建四个 canonical 规格及可选 application。"""

    if case.need_snapshot is None:
        raise ValidationError("寻源案例缺少强类型需求快照")
    raw = (
        ("product_type", case.need_snapshot.product_category),
        ("material", case.need_snapshot.material),
        ("size", case.need_snapshot.size_spec),
        ("model", case.need_snapshot.model),
        ("application", case.need_snapshot.application),
    )
    values: list[tuple[str, str]] = []
    for name, fact in raw:
        if fact is None:
            continue
        if not isinstance(fact.value, str) or not fact.value.strip():
            raise ValidationError("冻结需求规格必须是非空文本")
        values.append((name, fact.value))
    return tuple(values)


def _public_draft_can_convert(draft: PublicCandidateDraft) -> bool:
    """身份/价格维度不完整的草稿只保留作校准，不创建候选。"""

    tiers = draft.indicative_price_tiers
    return (
        draft.is_verification_complete
        and len({tier.minimum_quantity for tier in tiers}) == len(tiers)
        and len({tier.currency for tier in tiers}) == 1
        and len({tier.unit for tier in tiers}) == 1
    )


def _candidate_from_public_draft(
    tenant_id: TenantId,
    case: SourcingCase,
    draft: PublicCandidateDraft,
    projection: CandidateEvidenceSnapshot,
) -> SupplierCandidate:
    """只从安全草稿和同一可信 Artifact 投影构造确定性候选。"""

    if draft.supplier_name is None or draft.product_title is None:
        raise ValidationError("公开候选草稿身份不完整")
    expected = _public_draft_expected_specs(case)
    expected_by_name = dict(expected)
    offered_by_name: dict[str, str | None] = {}
    for item in draft.specs:
        name = _normalize(item.spec_name)
        if name not in expected_by_name or item.required != expected_by_name[name]:
            raise ValidationError("公开候选草稿规格与冻结需求不一致")
        offered_by_name[name] = item.observed
    artifact_id = projection.artifact_id
    provenance = ProvenanceSummary(
        source_type=SourceType.WEB_PAGE,
        source_id=str(artifact_id),
        extracted_by=_PUBLIC_DRAFT_VERIFIER,
        extracted_at=projection.observed_at,
        confirmed_by=None,
        confirmed_at=None,
    )
    comparisons: list[SpecComparison] = []
    observed_facts: dict[str, SourcingObservedFact] = {}
    for name, required in expected:
        offered = offered_by_name.get(name)
        level = (
            SpecMatchLevel.UNKNOWN
            if offered is None
            else SpecMatchLevel.EXACT
            if _normalize(offered) == _normalize(required)
            else SpecMatchLevel.DIFFERENT
        )
        comparisons.append(
            SpecComparison(
                spec_name=name,
                required=required,
                offered=offered,
                level=level,
                substitutable=None,
                substitution_impact=None,
                needs_customer_confirmation=False,
                customer_confirmation=None,
                evidence_ref=artifact_id if offered is not None else None,
            )
        )
        if offered is not None:
            observed_facts[name] = SourcingObservedFact(
                value=offered,
                provenance=provenance,
                evidence_ref=artifact_id,
            )
    tiers = tuple(
        IndicativePriceTier(
            minimum_quantity=tier.minimum_quantity,
            amount=tier.amount,
            currency=tier.currency,
            unit=tier.unit,
            provenance=provenance,
            evidence_ref=artifact_id,
        )
        for tier in sorted(
            draft.indicative_price_tiers,
            key=lambda item: item.minimum_quantity,
        )
    )
    observed_facts["supplier_name"] = SourcingObservedFact(
        value=draft.supplier_name,
        provenance=provenance,
        evidence_ref=artifact_id,
    )
    observed_facts["product_title"] = SourcingObservedFact(
        value=draft.product_title,
        provenance=provenance,
        evidence_ref=artifact_id,
    )
    if draft.moq is not None:
        observed_facts["moq"] = SourcingObservedFact(
            value=draft.moq, provenance=provenance, evidence_ref=artifact_id
        )
    price_unit = tiers[0].unit
    currency = tiers[0].currency
    observed_facts["price_unit"] = SourcingObservedFact(
        value=price_unit, provenance=provenance, evidence_ref=artifact_id
    )
    observed_facts["currency"] = SourcingObservedFact(
        value=currency, provenance=provenance, evidence_ref=artifact_id
    )
    evidence = EvidenceSnapshot(
        url=projection.canonical_url,
        observed_at=projection.observed_at,
        content_hash=projection.content_hash,
        artifact_ref=str(artifact_id),
    )
    summary = "；".join(
        f"{item.spec_name}:{item.level.value}" for item in comparisons
    )
    return SupplierCandidate(
        candidate_id=SupplierCandidateId(new_id("spc")),
        tenant_id=tenant_id,
        case_id=case.case_id,
        supplier_name=draft.supplier_name,
        product_title=draft.product_title,
        created_at=draft.created_at,
        source_platform="public_web",
        observed_facts=observed_facts,
        supplier_claims={},
        match_inferences={
            "public_draft_match": SourcingMatchInference(
                value=_PUBLIC_DRAFT_VERIFIER,
                based_on=(artifact_id,),
                inferred_by=_PUBLIC_DRAFT_VERIFIER,
                inferred_at=projection.observed_at,
            )
        },
        verified_specs=comparisons,
        indicative_price_tiers=tiers,
        moq=draft.moq,
        price_unit=price_unit,
        currency=currency,
        evidence=evidence,
        evidence_snapshots=(evidence,),
        match=MatchExplanation(
            rung=MatchLadderRung.PUBLIC_SOURCING,
            comparisons=comparisons,
            summary=summary,
        ),
        rejected=False,
        rejection_reasons=[],
        verified_by=None,
        public_draft_source_key=draft.source_key,
    )


def _same_public_candidate(
    canonical: SupplierCandidate, proposed: SupplierCandidate
) -> bool:
    """冲突只允许 candidate_id 不同，其余不可变内容和绑定必须精确一致。"""

    return canonical == replace(proposed, candidate_id=canonical.candidate_id)


async def _public_candidate_proposal(
    reader: CandidateEvidenceSnapshotReader,
    tenant_id: TenantId,
    case: SourcingCase,
    draft: PublicCandidateDraft,
) -> SupplierCandidate:
    """读取同一可信 Artifact 并返回可比较的完整候选 proposal。"""

    projection = await _read_candidate_evidence(
        reader, tenant_id, draft.evidence_artifact_ref
    )
    if (
        projection is None
        or not isinstance(projection, CandidateEvidenceSnapshot)
        or not _valid_candidate_evidence_projection(projection)
        or projection.tenant_id != tenant_id
        or projection.artifact_id != draft.evidence_artifact_ref
        or projection.canonical_url != draft.evidence_url
        or projection.content_hash != draft.evidence_hash
        or projection.observed_at != draft.evidence_observed_at
    ):
        raise MissingEvidenceSnapshotError("公开候选草稿 Artifact 元数据不一致")
    return _candidate_from_public_draft(tenant_id, case, draft, projection)


def _apply_public_candidate_verification(
    candidate: SupplierCandidate,
    *,
    quantity: int,
    qualified_count: int,
) -> None:
    """按普通候选清单、MOQ、数量档与三候选上限写入固定拒绝原因。"""

    passed, _missing = candidate.passes_verification()
    reasons: list[PriceRejectionReason] = []
    if not passed:
        reasons.append(PriceRejectionReason.VERIFICATION_INCOMPLETE)
    if candidate.moq is None or quantity < candidate.moq:
        reasons.append(PriceRejectionReason.MOQ_NOT_MET)
    if not any(
        tier.minimum_quantity <= quantity
        for tier in candidate.indicative_price_tiers
    ):
        reasons.append(PriceRejectionReason.QUANTITY_TIER_MISSING)
    if not reasons and qualified_count >= MAX_QUALIFIED_CANDIDATES:
        reasons.append(PriceRejectionReason.QUALIFIED_LIMIT_REACHED)
    candidate.rejection_reasons = list(dict.fromkeys(reasons))
    candidate.rejected = bool(candidate.rejection_reasons)


def _candidate_view(candidate: SupplierCandidate) -> CandidateView:
    return CandidateView(
        candidate_id=str(candidate.candidate_id),
        supplier_name=candidate.supplier_name,
        product_title=candidate.product_title,
        source_platform=candidate.source_platform,
        specs=[
            SpecComparisonView(
                spec_name=item.spec_name,
                required=item.required,
                offered=item.offered,
                level=item.level.value,
                substitutable=item.substitutable,
                substitution_impact=item.substitution_impact,
                needs_customer_confirmation=item.needs_customer_confirmation,
                customer_confirmation=item.customer_confirmation,
            )
            for item in candidate.verified_specs
        ],
        quoted_prices={
            tier.minimum_quantity: Money(tier.amount, CurrencyCode(tier.currency))
            for tier in candidate.indicative_price_tiers
        },
        match_summary=candidate.match.summary if candidate.match is not None else None,
        moq=candidate.moq,
        price_unit=candidate.price_unit,
        currency=candidate.currency,
        rejected=candidate.rejected,
        rejection_reasons=[item.value for item in candidate.rejection_reasons],
        evidence_url=(
            candidate.evidence.url if candidate.evidence is not None else None
        ),
    )


def _case_view(case: SourcingCase, candidates: list[SupplierCandidate]) -> CaseView:
    return CaseView(
        case_id=str(case.case_id),
        need_id=str(case.need_id),
        state=case.state.value,
        opened_at=case.opened_at,
        ladder_checked_to=(
            case.ladder_checked_to.value if case.ladder_checked_to is not None else None
        ),
        candidates=[_candidate_view(item) for item in candidates],
        qualified_count=sum(
            not item.rejected and item.passes_verification()[0] for item in candidates
        ),
        failed_reason=case.failed_reason,
        completed_at=case.completed_at,
    )


class SourcingServiceImpl:
    """所有公开写入口先授权，再进入同租户 Sourcing UoW。"""

    def __init__(
        self,
        uow_factory: Callable[[TenantId], SourcingUnitOfWork],
        authorizer: SourcingAuthorizer,
        evidence_reader: CandidateEvidenceSnapshotReader,
        *,
        provider_usage_evidence_reader: ProviderUsageEvidenceReader | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(uow_factory):
            raise ValidationError("寻源事务依赖无效")
        if not isinstance(authorizer, SourcingAuthorizer):
            raise ValidationError("寻源授权依赖无效")
        if not isinstance(evidence_reader, CandidateEvidenceSnapshotReader):
            raise ValidationError("候选 Evidence reader 无效")
        self._uow_factory = uow_factory
        self._authorizer = authorizer
        self._evidence_reader = evidence_reader
        if provider_usage_evidence_reader is not None and not isinstance(
            provider_usage_evidence_reader, ProviderUsageEvidenceReader
        ):
            raise ValidationError("Provider 用量 Evidence reader 无效")
        self._provider_usage_evidence_reader = provider_usage_evidence_reader
        self._now = now or (lambda: datetime.now(UTC))

    def _require(
        self,
        tenant_id: TenantId,
        actor: SourcingActor,
        action: SourcingAction,
        scope: SourcingScope,
    ) -> None:
        self._authorizer.require(actor, action, scope, tenant_id)

    async def open_case(
        self,
        tenant_id: TenantId,
        command: OpenSourcingCase,
        *,
        actor: SourcingActor,
    ) -> SourcingCaseId:
        self._require(tenant_id, actor, SourcingAction.CASE_OPEN, SourcingScope.SYSTEM)
        if not isinstance(command, OpenSourcingCase):
            raise ValidationError("寻源开案命令无效")
        if command.need.completeness < 3:
            raise SourcingThresholdNotMetError("已验证需求完整度不足 3")
        expected_trigger = f"sourcing-case:v2:{tenant_id}:{command.need.need_id}"
        if command.trigger_key != expected_trigger:
            raise ValidationError("寻源开案 trigger_key 与可信业务键不一致")
        now = _aware(self._now())
        async with self._uow_factory(tenant_id) as uow:
            case = SourcingCase(
                case_id=SourcingCaseId(new_id("src")),
                tenant_id=tenant_id,
                need_id=command.need.need_id,
                opened_at=now,
                workflow_version=command.workflow_version,
                trigger_key=command.trigger_key,
                need_snapshot=command.need,
                need_snapshot_hash=command.need.snapshot_hash,
                state_changed_at=now,
            )
            canonical, created = await uow.cases.get_or_create(tenant_id, case)
            if (
                canonical.need_id != command.need.need_id
                or canonical.workflow_version != command.workflow_version
            ):
                raise ValidationError("开案幂等键已绑定不同需求或工作流版本")
            if created:
                await uow.bus.publish(
                    SourcingCaseOpened(
                        tenant_id=tenant_id,
                        occurred_at=now,
                        case_id=canonical.case_id,
                        need_id=canonical.need_id,
                    )
                )
            return canonical.case_id

    async def record_ladder_check(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        check: LadderCheck,
        *,
        actor: SourcingActor,
    ) -> None:
        self._require(
            tenant_id,
            actor,
            SourcingAction.WORKFLOW_PROGRESS,
            SourcingScope.SYSTEM,
        )
        if not isinstance(check, LadderCheck):
            raise ValidationError("匹配梯子检查命令无效")
        if not isinstance(check.outcome, LadderOutcome):
            raise ValidationError("匹配梯子结果必须使用类型化 outcome")
        if (
            check.outcome is LadderOutcome.NO_QUALIFIED_SUPPLY
            and "product_spec_evidence" in check.input_snapshot
        ):
            raise ValidationError("无合格供给梯级不得携带产品证据映射")
        if (
            check.outcome is LadderOutcome.QUALIFIED_SUPPLY_FOUND
            and check.match_object_type == "product"
        ):
            comparisons = check.spec_comparisons
            normalized_keys = tuple(
                (
                    str(item.product_id) if item.product_id is not None else "",
                    _normalize(item.spec_name),
                )
                for item in comparisons
            )
            if (
                not comparisons
                or len(set(normalized_keys)) != len(normalized_keys)
                or any(
                    not product_id
                    or not name
                    or item.level is not SpecMatchLevel.EXACT
                    or item.offered is None
                    or not item.offered.strip()
                    or item.evidence_ref is None
                    for (product_id, name), item in zip(
                        normalized_keys, comparisons, strict=True
                    )
                )
                or not check.evidence_refs
            ):
                raise ValidationError("合格产品梯级必须保存有证据的逐项 exact 比较")
            _validate_product_evidence_mapping(check)
        now = _aware(self._now())
        async with self._uow_factory(tenant_id) as uow:
            case = _case_required(await uow.cases.get(tenant_id, case_id))
            if case.state not in {CaseState.OPENED, CaseState.DISCOVERING}:
                raise InvalidStateTransition(
                    f"寻源案例处于 {case.state.value}，不能记录匹配梯子"
                )
            if check.tenant_id != tenant_id or check.case_id != case_id:
                raise ValidationError("匹配梯子检查租户或案例不匹配")
            checks = await uow.checks.list_for_case(tenant_id, case_id)
            existing_same_rung = next(
                (item for item in checks if item.rung is check.rung), None
            )
            if existing_same_rung is not None:
                replay = replace(
                    check,
                    checked_by=_employee(actor),
                    checked_at=existing_same_rung.checked_at,
                )
                if replay == existing_same_rung:
                    return
                raise ValidationError("匹配梯子同级事实冲突")
            if any(
                item.outcome is LadderOutcome.QUALIFIED_SUPPLY_FOUND for item in checks
            ):
                raise ValidationError("较早梯级已找到合格供给，禁止继续向后检查")
            expected = len(checks) + 1
            if (
                expected > 5
                or check.sequence_number != expected
                or check.rung.value != expected
            ):
                raise ValidationError(
                    f"匹配梯子必须连续记录 1–5；当前期望第 {expected} 级"
                )
            trusted = replace(
                check,
                checked_by=_employee(actor),
                checked_at=now,
            )
            await uow.checks.add(tenant_id, trusted)
            if case.state is CaseState.OPENED:
                case.transition_to(CaseState.DISCOVERING, changed_at=now)
            else:
                case.version += 1
            case.ladder_checked_to = trusted.rung
            await uow.cases.update(tenant_id, case)

    async def save_public_plan(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        command: PublicSourcingPlanCommand,
        *,
        actor: SourcingActor,
    ) -> PublicSourcingPlan:
        self._require(
            tenant_id,
            actor,
            SourcingAction.PLAN_DRAFT,
            SourcingScope.TENANT,
        )
        if not isinstance(command, PublicSourcingPlanCommand):
            raise ValidationError("公开寻源计划命令无效")
        now = _aware(self._now())
        async with self._uow_factory(tenant_id) as uow:
            case = _case_required(await uow.cases.get_for_update(tenant_id, case_id))
            if case.state not in {CaseState.DISCOVERING, CaseState.VERIFYING}:
                raise InvalidStateTransition(
                    f"寻源案例处于 {case.state.value}，不能保存公开计划"
                )
            if command.case_id != case_id:
                raise ValidationError("公开寻源计划案例不匹配")
            checks = await uow.checks.list_for_case(tenant_id, case_id)
            if any(
                item.outcome is not LadderOutcome.NO_QUALIFIED_SUPPLY for item in checks
            ):
                raise ValidationError("公开寻源前五级必须全部确认无合格供给")
            if [item.rung.value for item in checks] != [1, 2, 3, 4, 5]:
                raise ValidationError("公开寻源前必须连续完成匹配梯子 1–5")
            if command.expected_case_version != case.version:
                raise SourcingPlanStaleError("公开寻源计划绑定的 Case 版本已变化")
            active = await uow.plans.get_active_for_case(tenant_id, case_id)
            if case.active_search_plan_id is not None:
                bound = await uow.plans.get(tenant_id, case.active_search_plan_id)
                if bound is None:
                    raise SourcingPlanStaleError("Case 绑定的公开寻源计划不存在")
                if bound.status is PublicPlanStatus.RUNNING:
                    raise InvalidStateTransition("公开寻源已经运行，不能替换计划范围")
            expected_plan_version = 1 if active is None else active.version + 1
            if command.version != expected_plan_version:
                raise SourcingPlanStaleError("公开寻源计划版本不连续")
            if active is not None and command.plan_id == active.plan_id:
                raise SourcingPlanStaleError("范围变化必须创建新的计划 ID 与版本")
            plan = PublicSourcingPlan.create(tenant_id, command, created_at=now)
            await uow.plans.add(tenant_id, plan)
            return plan

    async def create_public_plan(
        self,
        tenant_id: TenantId,
        actor: SourcingActor,
        command: PublicSourcingPlanCommand,
        *,
        created_at: datetime,
    ) -> PublicSourcingPlan:
        """兼容旧公共名；时间仍由服务可信时钟统一决定。"""

        del created_at
        return await self.save_public_plan(
            tenant_id, command.case_id, command, actor=actor
        )

    async def confirm_public_plan(
        self,
        tenant_id: TenantId,
        plan_id: SourcingPlanId,
        expected_plan_hash: str,
        *,
        actor: SourcingActor,
        expected_case_id: SourcingCaseId | None = None,
    ) -> PublicSourcingPlan:
        self._require(
            tenant_id,
            actor,
            SourcingAction.PLAN_CONFIRM,
            SourcingScope.TENANT,
        )
        now = _aware(self._now())
        async with self._uow_factory(tenant_id) as uow:
            plan = await uow.plans.get(tenant_id, plan_id)
            if plan is None:
                raise ValidationError("公开寻源计划不存在或租户不匹配")
            if expected_case_id is not None and plan.case_id != expected_case_id:
                raise ValidationError("公开寻源计划与 Case 不一致")
            case = _case_required(
                await uow.cases.get_for_update(tenant_id, plan.case_id)
            )
            plan = await uow.plans.get_for_update(tenant_id, plan_id)
            if plan is None or plan.case_id != case.case_id:
                raise ValidationError("公开寻源计划不存在或租户不匹配")
            if plan.plan_hash != expected_plan_hash:
                raise SourcingPlanStaleError("确认哈希与当前公开寻源计划不一致")
            active = await uow.plans.get_active_for_case(tenant_id, plan.case_id)
            if active is None or active.plan_id != plan.plan_id:
                raise SourcingPlanStaleError("公开寻源计划已被新版本取代")
            if case.state not in {CaseState.DISCOVERING, CaseState.VERIFYING}:
                raise InvalidStateTransition(
                    f"寻源案例处于 {case.state.value}，不能确认公开计划"
                )
            confirmed = plan.confirm(_employee(actor), confirmed_at=now)
            await uow.plans.update(tenant_id, confirmed)
            case.active_search_plan_id = plan.plan_id
            if case.state is CaseState.DISCOVERING:
                case.transition_to(CaseState.VERIFYING, changed_at=now)
            else:
                case.version += 1
                case.state_changed_at = now
            await uow.cases.update(tenant_id, case)
            return confirmed

    async def _public_plan_run_view(
        self,
        uow: SourcingUnitOfWork,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        plan_id: SourcingPlanId,
        expected_plan_hash: str,
        *,
        lock: bool,
    ) -> PublicSourcingRunView:
        case = _case_required(
            await (
                uow.cases.get_for_update(tenant_id, case_id)
                if lock
                else uow.cases.get(tenant_id, case_id)
            )
        )
        plan = await (
            uow.plans.get_for_update(tenant_id, plan_id)
            if lock
            else uow.plans.get(tenant_id, plan_id)
        )
        active = await uow.plans.get_active_for_case(tenant_id, case_id)
        if (
            plan is None
            or plan.case_id != case_id
            or active is None
            or active.plan_id != plan_id
            or case.active_search_plan_id != plan_id
            or plan.plan_hash != expected_plan_hash
            or plan.authorized_plan_hash != plan.plan_hash
            or plan.status not in {PublicPlanStatus.AUTHORIZED, PublicPlanStatus.RUNNING}
        ):
            raise SourcingPlanStaleError("公开寻源计划不是当前精确授权版本")
        if case.state is not CaseState.VERIFYING:
            raise InvalidStateTransition(
                f"寻源案例处于 {case.state.value}，不能运行公开计划"
            )
        return PublicSourcingRunView(
            tenant_id=tenant_id,
            case_id=case.case_id,
            need_id=case.need_id,
            case_version=case.version,
            active_plan=plan,
        )

    async def get_public_plan_run_view(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        plan_id: SourcingPlanId,
        expected_plan_hash: str,
        *,
        actor: SourcingActor,
    ) -> PublicSourcingRunView:
        self._require(tenant_id, actor, SourcingAction.PLAN_RUN, SourcingScope.TENANT)
        async with self._uow_factory(tenant_id) as uow:
            return await self._public_plan_run_view(
                uow,
                tenant_id,
                case_id,
                plan_id,
                expected_plan_hash,
                lock=False,
            )

    async def authorize_public_plan_run(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        plan_id: SourcingPlanId,
        expected_plan_hash: str,
        *,
        actor: SourcingActor,
    ) -> PublicSourcingPlan:
        self._require(tenant_id, actor, SourcingAction.PLAN_RUN, SourcingScope.TENANT)
        async with self._uow_factory(tenant_id) as uow:
            view = await self._public_plan_run_view(
                uow,
                tenant_id,
                case_id,
                plan_id,
                expected_plan_hash,
                lock=True,
            )
            if view.active_plan.status is PublicPlanStatus.RUNNING:
                return view.active_plan
            running = view.active_plan.transition_to(PublicPlanStatus.RUNNING)
            await uow.plans.update(tenant_id, running)
            return running

    async def _uncertain_execution(
        self,
        uow: SourcingUnitOfWork,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        run_id: RunId,
        request_key: str,
    ) -> SourcingSearchExecution:
        execution = await uow.search_executions.get_by_request_key(
            tenant_id, request_key
        )
        if (
            execution is None
            or execution.tenant_id != tenant_id
            or execution.case_id != case_id
            or execution.run_id != str(run_id)
            or execution.request_key != request_key
            or execution.provider_status is not SourcingSearchExecutionStatus.UNCERTAIN
        ):
            raise ValidationError("不确定搜索执行不存在或绑定不一致")
        case = _case_required(await uow.cases.get(tenant_id, case_id))
        plan = await uow.plans.get(tenant_id, execution.plan_id)
        active = await uow.plans.get_active_for_case(tenant_id, case_id)
        if (
            case.state is not CaseState.VERIFYING
            or case.active_search_plan_id != execution.plan_id
            or plan is None
            or active is None
            or active.plan_id != plan.plan_id
            or plan.status is not PublicPlanStatus.RUNNING
            or plan.plan_hash != execution.plan_hash
            or plan.authorized_plan_hash != execution.plan_hash
        ):
            raise SourcingPlanStaleError("不确定搜索执行不属于当前运行计划")
        return execution

    async def get_uncertain_search_execution(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        run_id: RunId,
        request_key: str,
        *,
        actor: SourcingActor,
    ) -> SourcingSearchExecution:
        self._require(
            tenant_id,
            actor,
            SourcingAction.SEARCH_RECONCILE,
            SourcingScope.TENANT,
        )
        async with self._uow_factory(tenant_id) as uow:
            return await self._uncertain_execution(
                uow, tenant_id, case_id, run_id, request_key
            )

    async def record_confirmed_consumed_reconciliation(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        command: SourcingUncertainReconciliationCommand,
        *,
        actor: SourcingActor,
    ) -> SourcingSearchReconciliation:
        self._require(
            tenant_id,
            actor,
            SourcingAction.SEARCH_RECONCILE,
            SourcingScope.TENANT,
        )
        if not isinstance(command, SourcingUncertainReconciliationCommand):
            raise ValidationError("不确定搜索核对命令无效")
        projection = await _read_provider_usage_evidence(
            self._provider_usage_evidence_reader,
            tenant_id,
            command.provider_usage_artifact_ref,
        )
        if projection is None or not _valid_provider_usage_evidence(
            projection, tenant_id, command.provider_usage_artifact_ref
        ):
            raise ValidationError("Provider 用量核对证据不可验证")
        now = _aware(self._now())
        async with self._uow_factory(tenant_id) as uow:
            execution = await self._uncertain_execution(
                uow, tenant_id, case_id, command.run_id, command.request_key
            )
            reconciliation = SourcingSearchReconciliation(
                reconciliation_id=command.reconciliation_id,
                tenant_id=tenant_id,
                execution_id=execution.execution_id,
                status=SourcingReconciliationStatus.CONFIRMED_CONSUMED,
                reason=command.reason,
                provider_usage_artifact_ref=command.provider_usage_artifact_ref,
                created_at=now,
                reconciled_by=_employee(actor),
                reconciled_at=now,
            )
            canonical = await uow.reconciliations.get_or_create_canonical(
                tenant_id, reconciliation
            )
            if (
                canonical.reconciliation_id != reconciliation.reconciliation_id
                or canonical.tenant_id != tenant_id
                or canonical.execution_id != reconciliation.execution_id
                or canonical.status
                is not SourcingReconciliationStatus.CONFIRMED_CONSUMED
                or canonical.reason != reconciliation.reason
                or canonical.provider_usage_artifact_ref
                != reconciliation.provider_usage_artifact_ref
                or canonical.reconciled_by != reconciliation.reconciled_by
            ):
                raise SourcingPlanStaleError("不确定搜索核对事实冲突")
            return canonical

    async def submit_candidate(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        submission: CandidateSubmission,
        *,
        actor: SourcingActor,
    ) -> SupplierCandidateId:
        action = (
            SourcingAction.WORKFLOW_PROGRESS
            if actor.scope is SourcingScope.SYSTEM
            else SourcingAction.CANDIDATE_ENTER
        )
        self._require(tenant_id, actor, action, actor.scope)
        if not isinstance(submission, CandidateSubmission):
            raise ValidationError("候选提交命令无效")
        artifact_id = ArtifactId(submission.evidence_artifact_ref)
        projection = await _read_candidate_evidence(
            self._evidence_reader, tenant_id, artifact_id
        )
        if projection is None:
            raise MissingEvidenceSnapshotError("无法读取可信候选证据快照")
        if not isinstance(
            projection, CandidateEvidenceSnapshot
        ) or not _valid_candidate_evidence_projection(projection):
            raise MissingEvidenceSnapshotError("可信候选证据快照形状无效")
        evidence = EvidenceSnapshot(
            url=projection.canonical_url,
            observed_at=projection.observed_at,
            content_hash=projection.content_hash,
            artifact_ref=str(projection.artifact_id),
        )
        if (
            projection.tenant_id != tenant_id
            or projection.artifact_id != artifact_id
            or projection.canonical_url != submission.evidence_url
            or projection.content_hash != submission.evidence_hash
            or not evidence.url
            or not evidence.content_hash
        ):
            raise MissingEvidenceSnapshotError("候选证据快照与提交字段不一致")
        evidence_refs = {projection.artifact_id}
        observed_fact_refs = {
            item.evidence_ref for item in submission.observed_facts.values()
        }
        supplier_claim_refs = {
            item.evidence_ref for item in submission.supplier_claims.values()
        }
        field_refs = observed_fact_refs | supplier_claim_refs
        inference_refs = {
            ref
            for item in submission.match_inferences.values()
            for ref in item.based_on
        }
        tier_refs = {item.evidence_ref for item in submission.indicative_price_tiers}
        if (
            not field_refs <= evidence_refs
            or not inference_refs <= evidence_refs
            or not tier_refs <= evidence_refs
        ):
            raise MissingEvidenceSnapshotError("候选字段 Provenance 未绑定可信证据快照")
        now = _aware(self._now())
        async with self._uow_factory(tenant_id) as uow:
            case = _case_required(await uow.cases.get(tenant_id, case_id))
            if case.state is not CaseState.VERIFYING:
                raise InvalidStateTransition(
                    f"寻源案例处于 {case.state.value}，不能提交候选"
                )
            if case.candidates_verified_at is not None:
                raise InvalidStateTransition("候选集合已封存，不能继续提交候选")
            candidate = _candidate_from_submission(
                tenant_id,
                case,
                submission,
                actor=actor,
                now=now,
                evidence=evidence,
            )
            passed, missing = candidate.passes_verification()
            if "evidence_snapshot" in missing:
                raise MissingEvidenceSnapshotError("候选缺少有效不可变证据快照")
            quantity = _quantity(case)
            reasons: list[PriceRejectionReason] = []
            if not passed:
                reasons.append(PriceRejectionReason.VERIFICATION_INCOMPLETE)
            if candidate.moq is None or quantity < candidate.moq:
                reasons.append(PriceRejectionReason.MOQ_NOT_MET)
            if not any(
                tier.minimum_quantity <= quantity
                for tier in candidate.indicative_price_tiers
            ):
                reasons.append(PriceRejectionReason.QUANTITY_TIER_MISSING)
            if (
                not reasons
                and await uow.candidates.count_qualified(tenant_id, case_id)
                >= MAX_QUALIFIED_CANDIDATES
            ):
                reasons.append(PriceRejectionReason.QUALIFIED_LIMIT_REACHED)
            candidate.rejection_reasons = list(dict.fromkeys(reasons))
            candidate.rejected = bool(candidate.rejection_reasons)
            await uow.candidates.add(tenant_id, candidate)
            case.version += 1
            await uow.cases.update(tenant_id, case)
            return candidate.candidate_id

    async def complete_case(
        self,
        tenant_id: TenantId,
        actor: SourcingActor,
        case_id: SourcingCaseId,
    ) -> None:
        """旧单事件完成入口在 V2 关闭，防止绕过候选与审核门槛。"""

        self._require(
            tenant_id,
            actor,
            SourcingAction.WORKFLOW_PROGRESS,
            SourcingScope.SYSTEM,
        )
        raise InvalidStateTransition(
            f"V2 Case {case_id} 不支持 complete_case；必须走候选就绪与成本交接"
        )

    async def fail_case(
        self,
        tenant_id: TenantId,
        actor: SourcingActor,
        case_id: SourcingCaseId,
        reason: str,
    ) -> None:
        """以固定公开停止码结束早期 Case；不接受 Provider 自由文本。"""

        self._require(
            tenant_id,
            actor,
            SourcingAction.WORKFLOW_PROGRESS,
            SourcingScope.SYSTEM,
        )
        allowed = {
            SourcingStopCode.QUOTA_STATUS_UNKNOWN,
            SourcingStopCode.PAID_USAGE_ENABLED,
            SourcingStopCode.QUOTA_EXHAUSTED,
            SourcingStopCode.RECONCILIATION_REQUIRED,
            SourcingStopCode.NO_SEARCH_RESULTS,
            SourcingStopCode.PROVIDER_TIMEOUT,
            SourcingStopCode.PROVIDER_RATE_LIMITED,
            SourcingStopCode.PAGE_ACCESS_FORBIDDEN,
            SourcingStopCode.LOGIN_OR_CAPTCHA,
            SourcingStopCode.UNSAFE_REDIRECT,
            SourcingStopCode.NO_VERIFIABLE_SUPPLIER,
            SourcingStopCode.NO_QUALIFIED_CANDIDATE,
        }
        try:
            stop_code = SourcingStopCode(reason)
        except ValueError as exc:
            raise ValidationError("寻源失败原因必须使用固定公开停止码") from exc
        if stop_code not in allowed:
            raise ValidationError("寻源失败原因不属于 V2 公开停止码")
        now = _aware(self._now())
        async with self._uow_factory(tenant_id) as uow:
            case = _case_required(await uow.cases.get(tenant_id, case_id))
            case.transition_to(CaseState.FAILED, changed_at=now)
            case.stop_code = stop_code
            case.stop_detail = None
            case.failed_reason = stop_code.value
            await uow.cases.update(tenant_id, case)

    async def get_case(
        self,
        tenant_id: TenantId,
        actor: SourcingActor,
        case_id: SourcingCaseId,
    ) -> CaseView:
        self._require(tenant_id, actor, SourcingAction.CASE_READ, SourcingScope.TENANT)
        async with self._uow_factory(tenant_id) as uow:
            case = _case_required(await uow.cases.get(tenant_id, case_id))
            candidates = await uow.candidates.list_for_case(tenant_id, case_id, True)
            return _case_view(case, candidates)

    async def list_open_cases(
        self,
        tenant_id: TenantId,
        actor: SourcingActor,
        limit: int = 50,
    ) -> list[CaseView]:
        self._require(tenant_id, actor, SourcingAction.CASE_LIST, SourcingScope.TENANT)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 50
        ):
            raise ValidationError("寻源案例列表 limit 必须在 1–50")
        cases: list[SourcingCase] = []
        async with self._uow_factory(tenant_id) as uow:
            for state in (
                CaseState.OPENED,
                CaseState.DISCOVERING,
                CaseState.VERIFYING,
                CaseState.CANDIDATES_READY,
            ):
                cases.extend(await uow.cases.list_by_state(tenant_id, state, limit))
            cases.sort(key=lambda item: (item.opened_at, str(item.case_id)))
            views: list[CaseView] = []
            for case in cases[:limit]:
                candidates = await uow.candidates.list_for_case(
                    tenant_id, case.case_id, True
                )
                views.append(_case_view(case, candidates))
            return views

    @staticmethod
    def _require_candidate_generation(
        case: SourcingCase,
        *,
        expected_case_version: int | None,
        expected_candidate_set_hash: str | None,
        allow_ready: bool,
    ) -> None:
        """核对产品卡投影绑定的精确候选 generation。"""

        if (
            not case.sealed_candidate_ids
            or case.candidate_set_hash is None
            or case.candidates_verified_at is None
            or expected_case_version is None
            or expected_candidate_set_hash is None
        ):
            raise ValidationError("供应商候选路径缺少已封存 generation")
        expected_current_version = expected_case_version
        if allow_ready and case.state is CaseState.CANDIDATES_READY:
            expected_current_version += 1
        if (
            case.version != expected_current_version
            or case.candidate_set_hash != expected_candidate_set_hash
        ):
            raise ValidationError("供应商候选投影 generation 已过期")

    async def mark_candidates_ready(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        option_ids: tuple[SourcingSupplyOptionId, ...],
        candidate_ids: tuple[SupplierCandidateId, ...],
        *,
        expected_case_version: int | None = None,
        expected_candidate_set_hash: str | None = None,
        actor: SourcingActor,
    ) -> None:
        self._require(
            tenant_id,
            actor,
            SourcingAction.FACT_PUBLISH,
            SourcingScope.SYSTEM,
        )
        if len(set(option_ids)) != len(option_ids) or len(set(candidate_ids)) != len(
            candidate_ids
        ):
            raise ValidationError("候选就绪事件 ID 不得重复")
        if not option_ids:
            raise ValidationError("候选就绪至少需要一个合格供应 Option")
        now = _aware(self._now())
        async with self._uow_factory(tenant_id) as uow:
            case = _case_required(await uow.cases.get_for_update(tenant_id, case_id))
            if case.state not in {CaseState.VERIFYING, CaseState.CANDIDATES_READY}:
                raise InvalidStateTransition(
                    f"寻源案例处于 {case.state.value}，不能标记候选就绪"
                )
            candidates = await uow.candidates.list_for_case(tenant_id, case_id, False)
            qualified_candidates = tuple(
                item
                for item in candidates
                if not item.rejected and item.passes_verification()[0]
            )
            options = await uow.options.list_for_case(tenant_id, case_id)
            qualified_options = tuple(item for item in options if item.is_qualified)
            frozen_product_ids = _qualified_internal_product_ids(
                await uow.checks.list_for_case(tenant_id, case_id)
            )
            existing_product_ids = tuple(
                sorted(
                    (
                        item.product_id
                        for item in qualified_options
                        if item.source is SupplyOptionSource.EXISTING_PRODUCT
                    ),
                    key=str,
                )
            )
            canonical_candidate_ids = tuple(
                sorted(
                    (item.candidate_id for item in qualified_candidates),
                    key=str,
                )
            )
            canonical_option_ids = tuple(item.option_id for item in qualified_options)
            if (
                not canonical_option_ids
                or set(candidate_ids) != set(canonical_candidate_ids)
                or len(candidate_ids) != len(canonical_candidate_ids)
                or set(option_ids) != set(canonical_option_ids)
                or len(option_ids) != len(canonical_option_ids)
            ):
                raise ValidationError("候选就绪必须提交仓储重建的完整合格集合")
            if existing_product_ids != frozen_product_ids:
                raise ValidationError("现有产品 Option 必须精确等于合格梯级冻结集合")
            if canonical_candidate_ids:
                self._require_candidate_generation(
                    case,
                    expected_case_version=expected_case_version,
                    expected_candidate_set_hash=expected_candidate_set_hash,
                    allow_ready=True,
                )
                if canonical_candidate_ids != case.sealed_candidate_ids:
                    raise ValidationError("候选就绪集合与已封存 generation 不一致")
            elif (
                case.sealed_candidate_ids
                or expected_case_version is not None
                or expected_candidate_set_hash is not None
            ):
                raise ValidationError("仅现有产品路径不得携带候选 generation")
            supplier_candidate_ids = {
                item.supplier_candidate_id
                for item in qualified_options
                if item.source is SupplyOptionSource.SUPPLIER_CANDIDATE
            }
            if supplier_candidate_ids != set(canonical_candidate_ids):
                raise ValidationError("供应商候选产品卡或供给选项尚未完整登记")
            if case.state is CaseState.CANDIDATES_READY:
                return
            case.transition_to(CaseState.CANDIDATES_READY, changed_at=now)
            await uow.cases.update(tenant_id, case)
            await uow.bus.publish(
                SourcingCandidatesReady(
                    tenant_id=tenant_id,
                    occurred_at=now,
                    case_id=case_id,
                    option_ids=canonical_option_ids,
                    candidate_ids=canonical_candidate_ids,
                )
            )

    async def mark_candidates_verified(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        candidate_ids: tuple[SupplierCandidateId, ...],
        *,
        actor: SourcingActor,
    ) -> SourcingCandidatesVerified:
        self._require(
            tenant_id,
            actor,
            SourcingAction.FACT_PUBLISH,
            SourcingScope.SYSTEM,
        )
        if not candidate_ids or len(set(candidate_ids)) != len(candidate_ids):
            raise ValidationError("候选核验事件必须包含非空且不重复的候选集合")
        now = _aware(self._now())
        async with self._uow_factory(tenant_id) as uow:
            case = _case_required(await uow.cases.get(tenant_id, case_id))
            if case.state is not CaseState.VERIFYING:
                raise InvalidStateTransition(
                    f"寻源案例处于 {case.state.value}，不能发布候选核验事实"
                )
            candidates = await uow.candidates.list_for_case(tenant_id, case_id, False)
            canonical_ids = tuple(
                sorted(
                    (
                        item.candidate_id
                        for item in candidates
                        if not item.rejected and item.passes_verification()[0]
                    ),
                    key=str,
                )
            )
            if set(candidate_ids) != set(canonical_ids) or len(candidate_ids) != len(
                canonical_ids
            ):
                raise ValidationError("候选核验事件必须是精确的合格候选全集")
            generation_hash = candidate_set_hash(canonical_ids)
            if case.sealed_candidate_ids:
                if (
                    case.sealed_candidate_ids != canonical_ids
                    or case.candidate_set_hash != generation_hash
                    or case.candidates_verified_at is None
                ):
                    raise ValidationError("候选集合已由不同 generation 封存")
                return SourcingCandidatesVerified(
                    tenant_id=tenant_id,
                    occurred_at=case.candidates_verified_at,
                    case_id=case_id,
                    candidate_ids=canonical_ids,
                    case_version=case.version,
                    candidate_set_hash=generation_hash,
                )
            case.sealed_candidate_ids = canonical_ids
            case.candidate_set_hash = generation_hash
            case.candidates_verified_at = now
            case.version += 1
            await uow.cases.update(tenant_id, case)
            event = SourcingCandidatesVerified(
                tenant_id=tenant_id,
                occurred_at=now,
                case_id=case_id,
                candidate_ids=canonical_ids,
                case_version=case.version,
                candidate_set_hash=generation_hash,
            )
            await uow.bus.publish(event)
            return event

    async def verify_public_candidate_drafts(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        command: VerifyPublicCandidateDraftsCommand,
        *,
        actor: SourcingActor,
    ) -> VerifyPublicCandidateDraftsResult:
        """在一个 Case 锁和 UoW 内转换草稿、重建全集并封存一次。"""

        self._require(
            tenant_id,
            actor,
            SourcingAction.FACT_PUBLISH,
            SourcingScope.SYSTEM,
        )
        if not isinstance(command, VerifyPublicCandidateDraftsCommand):
            raise ValidationError("公开候选草稿核验命令无效")
        now = _aware(self._now())
        async with self._uow_factory(tenant_id) as uow:
            case = _case_required(await uow.cases.get_for_update(tenant_id, case_id))
            plan = await uow.plans.get(tenant_id, command.plan_id)
            if (
                case.workflow_version != 2
                or case.state is not CaseState.VERIFYING
                or case.active_search_plan_id != command.plan_id
                or plan is None
                or plan.case_id != case_id
                or plan.plan_hash != command.plan_hash
                or plan.authorized_plan_hash != command.plan_hash
                or plan.status is not PublicPlanStatus.RUNNING
            ):
                raise SourcingPlanStaleError("公开候选草稿未绑定当前运行计划")
            drafts = await uow.candidate_drafts.list_exact_for_verification(
                tenant_id,
                case_id,
                command.run_id,
                command.plan_id,
                command.plan_hash,
            )
            if tuple(draft.draft_id for draft in drafts) != command.draft_ids:
                raise ValidationError("公开候选草稿集合或顺序不一致")
            if any(
                draft.tenant_id != tenant_id
                or draft.case_id != case_id
                or draft.run_id != command.run_id
                or draft.plan_id != command.plan_id
                or draft.plan_hash != command.plan_hash
                for draft in drafts
            ):
                raise ValidationError("公开候选草稿绑定漂移")

            calibration_ids = tuple(
                draft.draft_id for draft in drafts if not _public_draft_can_convert(draft)
            )
            convertible = tuple(
                draft for draft in drafts if _public_draft_can_convert(draft)
            )
            draft_sources = {draft.source_key for draft in drafts}
            existing = await uow.candidates.list_for_case(tenant_id, case_id, True)
            qualified_count = sum(
                not candidate.rejected and candidate.passes_verification()[0]
                for candidate in existing
                if candidate.public_draft_source_key not in draft_sources
            )
            quantity = _quantity(case)
            if case.sealed_candidate_ids:
                converted: list[SupplierCandidateId] = []
                rejected: list[SupplierCandidateId] = []
                for draft in drafts:
                    canonical = await uow.candidates.get_by_public_draft_source_key(
                        tenant_id, draft.source_key
                    )
                    if not _public_draft_can_convert(draft):
                        if canonical is not None:
                            raise ValidationError("已封存公开候选转换状态漂移")
                        continue
                    proposal = await _public_candidate_proposal(
                        self._evidence_reader, tenant_id, case, draft
                    )
                    _apply_public_candidate_verification(
                        proposal,
                        quantity=quantity,
                        qualified_count=qualified_count,
                    )
                    if canonical is None or canonical.case_id != case_id:
                        raise ValidationError("已封存公开候选缺少 canonical 绑定")
                    if not _same_public_candidate(canonical, proposal):
                        raise ValidationError("已封存公开候选内容或绑定漂移")
                    converted.append(canonical.candidate_id)
                    if canonical.rejected:
                        rejected.append(canonical.candidate_id)
                    elif canonical.passes_verification()[0]:
                        qualified_count += 1
                qualified = tuple(
                    sorted(
                        (
                            candidate.candidate_id
                            for candidate in await uow.candidates.list_for_case(
                                tenant_id, case_id, False
                            )
                            if candidate.passes_verification()[0]
                        ),
                        key=str,
                    )
                )
                if qualified != case.sealed_candidate_ids:
                    raise ValidationError("已封存公开候选 generation 已漂移")
                verified_at = case.candidates_verified_at
                if verified_at is None:
                    raise ValidationError("已封存公开候选缺少核验时间")
                replay_event = SourcingCandidatesVerified(
                    tenant_id=tenant_id,
                    occurred_at=verified_at,
                    case_id=case_id,
                    candidate_ids=qualified,
                    case_version=case.version,
                    candidate_set_hash=case.candidate_set_hash or "",
                )
                return VerifyPublicCandidateDraftsResult(
                    calibration_draft_ids=calibration_ids,
                    converted_candidate_ids=tuple(converted),
                    rejected_candidate_ids=tuple(rejected),
                    qualified_candidate_ids=qualified,
                    verified_event=replay_event,
                )

            converted_ids: list[SupplierCandidateId] = []
            rejected_ids: list[SupplierCandidateId] = []
            for draft in convertible:
                candidate = await _public_candidate_proposal(
                    self._evidence_reader, tenant_id, case, draft
                )
                _apply_public_candidate_verification(
                    candidate,
                    quantity=quantity,
                    qualified_count=qualified_count,
                )
                canonical, _created = await uow.candidates.get_or_create_public_draft(
                    tenant_id, candidate
                )
                if not _same_public_candidate(canonical, candidate):
                    raise ValidationError("公开草稿 source key 已绑定不同候选内容")
                converted_ids.append(canonical.candidate_id)
                if canonical.rejected:
                    rejected_ids.append(canonical.candidate_id)
                elif canonical.passes_verification()[0]:
                    qualified_count += 1

            qualified_ids = tuple(
                sorted(
                    (
                        candidate.candidate_id
                        for candidate in await uow.candidates.list_for_case(
                            tenant_id, case_id, False
                        )
                        if candidate.passes_verification()[0]
                    ),
                    key=str,
                )
            )
            if len(qualified_ids) > MAX_QUALIFIED_CANDIDATES:
                raise ValidationError("合格候选全集超过固定上限")
            verified_event: SourcingCandidatesVerified | None = None
            if qualified_ids:
                generation_hash = candidate_set_hash(qualified_ids)
                case.sealed_candidate_ids = qualified_ids
                case.candidate_set_hash = generation_hash
                case.candidates_verified_at = now
                case.version += 1
                await uow.cases.update(tenant_id, case)
                verified_event = SourcingCandidatesVerified(
                    tenant_id=tenant_id,
                    occurred_at=now,
                    case_id=case_id,
                    candidate_ids=qualified_ids,
                    case_version=case.version,
                    candidate_set_hash=generation_hash,
                )
                await uow.bus.publish(verified_event)
            return VerifyPublicCandidateDraftsResult(
                calibration_draft_ids=calibration_ids,
                converted_candidate_ids=tuple(converted_ids),
                rejected_candidate_ids=tuple(rejected_ids),
                qualified_candidate_ids=qualified_ids,
                verified_event=verified_event,
            )

    async def register_supplier_candidate_option(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        candidate_id: SupplierCandidateId,
        product_id: ProductId,
        *,
        expected_case_version: int | None = None,
        expected_candidate_set_hash: str | None = None,
        actor: SourcingActor,
    ) -> SourcingSupplyOptionId:
        self._require(
            tenant_id,
            actor,
            SourcingAction.WORKFLOW_PROGRESS,
            SourcingScope.SYSTEM,
        )
        if not str(product_id).strip():
            raise ValidationError("候选产品卡 ProductId 不能为空")
        now = _aware(self._now())
        async with self._uow_factory(tenant_id) as uow:
            case = _case_required(await uow.cases.get(tenant_id, case_id))
            if case.state not in {CaseState.VERIFYING, CaseState.CANDIDATES_READY}:
                raise InvalidStateTransition(
                    f"寻源案例处于 {case.state.value}，不能登记候选供给选项"
                )
            self._require_candidate_generation(
                case,
                expected_case_version=expected_case_version,
                expected_candidate_set_hash=expected_candidate_set_hash,
                allow_ready=True,
            )
            if candidate_id not in case.sealed_candidate_ids:
                raise ValidationError("候选不属于已封存 generation")
            candidate = await uow.candidates.get(tenant_id, candidate_id)
            if (
                candidate is None
                or candidate.case_id != case_id
                or candidate.rejected
                or not candidate.passes_verification()[0]
            ):
                raise ValidationError("只能为本 Case 的合格供应商候选登记产品卡")
            option = SourcingSupplyOption(
                option_id=SourcingSupplyOptionId(new_id("sop")),
                tenant_id=tenant_id,
                case_id=case_id,
                source=SupplyOptionSource.SUPPLIER_CANDIDATE,
                product_id=product_id,
                supplier_candidate_id=candidate_id,
                is_qualified=True,
                created_at=now,
            )
            canonical, _ = await uow.options.get_or_create_supplier_candidate(
                tenant_id, option
            )
            if canonical.product_id != product_id:
                raise ValidationError("候选已绑定不同的 canonical 产品卡")
            return canonical.option_id

    async def get_candidate_product_inputs(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        candidate_ids: tuple[SupplierCandidateId, ...],
        *,
        expected_case_version: int,
        expected_candidate_set_hash: str,
        actor: SourcingActor,
    ) -> SourcingCandidateProductInputs:
        """从仓储重建事件指定的精确封存候选 generation。"""

        self._require(
            tenant_id,
            actor,
            SourcingAction.FACT_PUBLISH,
            SourcingScope.SYSTEM,
        )
        async with self._uow_factory(tenant_id) as uow:
            case = _case_required(await uow.cases.get(tenant_id, case_id))
            if case.workflow_version != 2 or case.state not in {
                CaseState.VERIFYING,
                CaseState.CANDIDATES_READY,
            }:
                raise InvalidStateTransition(
                    f"寻源案例处于 {case.state.value}，不能读取候选产品投影"
                )
            self._require_candidate_generation(
                case,
                expected_case_version=expected_case_version,
                expected_candidate_set_hash=expected_candidate_set_hash,
                allow_ready=True,
            )
            if candidate_ids != case.sealed_candidate_ids:
                raise ValidationError("候选产品投影与封存 generation 不一致")
            candidates = tuple(
                sorted(
                    (
                        item
                        for item in await uow.candidates.list_for_case(
                            tenant_id, case_id, False
                        )
                        if not item.rejected and item.passes_verification()[0]
                    ),
                    key=lambda item: str(item.candidate_id),
                )
            )
            if tuple(item.candidate_id for item in candidates) != candidate_ids:
                raise ValidationError("仓储候选全集与封存 generation 不一致")
            need = case.need_snapshot
            if need is None or not isinstance(need.product_category.value, str):
                raise ValidationError("封存 Case 缺少产品品类事实")
            commands: list[SourcingCandidateProductInput] = []
            for candidate in candidates:
                if candidate.moq is None or candidate.match is None:
                    raise ValidationError("封存候选缺少产品卡必需事实")
                evidence_refs = tuple(
                    sorted(
                        {
                            ArtifactId(snapshot.artifact_ref)
                            for snapshot in candidate.evidence_snapshots
                        },
                        key=str,
                    )
                )
                prices = tuple(
                    SourcingCandidateProductPriceInput(
                        minimum_quantity=tier.minimum_quantity,
                        unit_amount=tier.amount,
                        currency=tier.currency,
                        unit=tier.unit,
                        evidence_ref=tier.evidence_ref,
                    )
                    for tier in sorted(
                        candidate.indicative_price_tiers,
                        key=lambda item: item.minimum_quantity,
                    )
                )
                commands.append(
                    SourcingCandidateProductInput(
                        sourcing_case_id=case_id,
                        supplier_candidate_id=candidate.candidate_id,
                        name_zh=candidate.product_title,
                        name_en=candidate.product_title,
                        category=need.product_category.value,
                        spec_summary=candidate.match.summary,
                        moq=candidate.moq,
                        evidence_refs=evidence_refs,
                        indicative_prices=prices,
                    )
                )
            return SourcingCandidateProductInputs(
                tenant_id=tenant_id,
                case_id=case_id,
                candidate_ids=candidate_ids,
                case_version=expected_case_version,
                candidate_set_hash=expected_candidate_set_hash,
                commands=tuple(commands),
            )

    async def register_existing_product_option(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        product_id: ProductId,
        *,
        actor: SourcingActor,
    ) -> SourcingSupplyOptionId:
        """把内部梯级已证明的真实 Product 原子登记为 canonical Option。"""

        self._require(
            tenant_id,
            actor,
            SourcingAction.WORKFLOW_PROGRESS,
            SourcingScope.SYSTEM,
        )
        if not str(product_id).strip():
            raise ValidationError("现有产品 ProductId 不能为空")
        now = _aware(self._now())
        async with self._uow_factory(tenant_id) as uow:
            case = _case_required(await uow.cases.get_for_update(tenant_id, case_id))
            if case.state not in {
                CaseState.DISCOVERING,
                CaseState.VERIFYING,
                CaseState.CANDIDATES_READY,
            }:
                raise InvalidStateTransition(
                    f"寻源案例处于 {case.state.value}，不能登记现有产品供给选项"
                )
            frozen_product_ids = _qualified_internal_product_ids(
                await uow.checks.list_for_case(tenant_id, case_id)
            )
            if product_id not in frozen_product_ids:
                raise ValidationError("现有产品不属于合格内部梯级冻结集合")
            existing_options = await uow.options.list_for_case(tenant_id, case_id)
            canonical_existing = next(
                (
                    item
                    for item in existing_options
                    if item.source is SupplyOptionSource.EXISTING_PRODUCT
                    and item.product_id == product_id
                ),
                None,
            )
            if canonical_existing is not None:
                return canonical_existing.option_id
            if case.state is CaseState.CANDIDATES_READY:
                raise InvalidStateTransition("候选就绪后禁止新增现有产品 Option")
            option = SourcingSupplyOption(
                option_id=SourcingSupplyOptionId(new_id("sop")),
                tenant_id=tenant_id,
                case_id=case_id,
                source=SupplyOptionSource.EXISTING_PRODUCT,
                product_id=product_id,
                supplier_candidate_id=None,
                is_qualified=True,
                created_at=now,
            )
            canonical, _ = await uow.options.get_or_create_existing_product(
                tenant_id, option
            )
            if canonical.product_id != product_id:
                raise ValidationError("现有产品已绑定不同的 canonical Option")
            if case.state is CaseState.DISCOVERING:
                case.transition_to(CaseState.VERIFYING, changed_at=now)
                await uow.cases.update(tenant_id, case)
            return canonical.option_id

    async def review(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        command: SourcingReviewCommand,
        *,
        actor: SourcingActor,
    ) -> SourcingReview:
        self._require(
            tenant_id,
            actor,
            SourcingAction.REVIEW_SUBMIT,
            SourcingScope.TENANT,
        )
        confirm_on_submit = actor.role == "boss"
        if confirm_on_submit:
            self._require(
                tenant_id,
                actor,
                SourcingAction.REVIEW_CONFIRM,
                SourcingScope.TENANT,
            )
        if not isinstance(command, SourcingReviewCommand):
            raise ValidationError("寻源审核命令无效")
        submitted_by = _employee(actor)
        async with self._uow_factory(tenant_id) as uow:
            case = _case_required(await uow.cases.get_for_update(tenant_id, case_id))
            existing = await uow.reviews.get_for_case(tenant_id, case_id)
            if existing is not None:
                if (
                    existing.tenant_id == tenant_id
                    and existing.case_id == case_id
                    and existing.primary_option_id == command.primary_option_id
                    and existing.alternate_option_ids == command.alternate_option_ids
                    and existing.reason == command.reason
                    and existing.expected_case_version == command.expected_case_version
                ):
                    if confirm_on_submit and existing.confirmed_by is None:
                        if (
                            case.state is not CaseState.CANDIDATES_READY
                            or case.version != existing.expected_case_version
                        ):
                            raise InvalidStateTransition(
                                "寻源审核绑定的 Case 状态或版本已变化"
                            )
                        confirmed = existing.confirm(
                            submitted_by, confirmed_at=_aware(self._now())
                        )
                        await uow.reviews.update(tenant_id, confirmed)
                        return confirmed
                    return existing
                raise ValidationError("该寻源案例已有审核事实")
            if case.state is not CaseState.CANDIDATES_READY:
                raise InvalidStateTransition(
                    f"寻源案例处于 {case.state.value}，不能提交审核"
                )
            selections = (command.primary_option_id, *command.alternate_option_ids)
            for option_id in selections:
                option = await uow.options.get(tenant_id, option_id)
                if (
                    option is None
                    or option.case_id != case_id
                    or not option.is_qualified
                ):
                    raise ValidationError("审核只能选择本 Case 的合格供给选项")
            now = _aware(self._now())
            review = SourcingReview.create(
                review_id=SourcingReviewId(new_id("srv")),
                tenant_id=tenant_id,
                case_id=case_id,
                command=command,
                submitted_by=submitted_by,
                submitted_at=now,
                actual_case_version=case.version,
            )
            if confirm_on_submit:
                review = review.confirm(
                    submitted_by, confirmed_at=now
                )
            await uow.reviews.add(tenant_id, review)
            return review

    async def submit_review(
        self,
        tenant_id: TenantId,
        actor: SourcingActor,
        case_id: SourcingCaseId,
        command: SourcingReviewCommand,
    ) -> SourcingReview:
        """兼容旧公共方法名。"""

        return await self.review(tenant_id, case_id, command, actor=actor)

    async def confirm_review(
        self,
        tenant_id: TenantId,
        review_id: SourcingReviewId,
        *,
        actor: SourcingActor,
    ) -> SourcingReview:
        self._require(
            tenant_id,
            actor,
            SourcingAction.REVIEW_CONFIRM,
            SourcingScope.TENANT,
        )
        now = _aware(self._now())
        async with self._uow_factory(tenant_id) as uow:
            review = await uow.reviews.get(tenant_id, review_id)
            if review is None:
                raise ValidationError("寻源审核不存在或租户不匹配")
            case = _case_required(
                await uow.cases.get_for_update(tenant_id, review.case_id)
            )
            canonical = await uow.reviews.get_for_case(tenant_id, review.case_id)
            if canonical is None or canonical.review_id != review_id:
                raise ValidationError("寻源审核 canonical 绑定无效")
            review = canonical
            if review.confirmed_by is not None:
                return review
            if (
                case.state is not CaseState.CANDIDATES_READY
                or case.version != review.expected_case_version
            ):
                raise InvalidStateTransition("寻源审核绑定的 Case 状态或版本已变化")
            confirmed = review.confirm(_employee(actor), confirmed_at=now)
            await uow.reviews.update(tenant_id, confirmed)
            return confirmed

    async def hand_to_costing(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        opportunity_id: OpportunityId,
        *,
        expected_need_id: ValidatedNeedId,
        actor: SourcingActor,
    ) -> SourcingHandoffSnapshot:
        self._require(
            tenant_id,
            actor,
            SourcingAction.WORKFLOW_PROGRESS,
            SourcingScope.SYSTEM,
        )
        if not str(opportunity_id).strip() or not str(expected_need_id).strip():
            raise ValidationError("成本交接必须绑定可信 Opportunity 引用")
        now = _aware(self._now())
        async with self._uow_factory(tenant_id) as uow:
            case = _case_required(await uow.cases.get_for_update(tenant_id, case_id))
            if case.need_id != expected_need_id:
                raise ValidationError("成本交接 Case 与 owning Run Need 不一致")
            review = await uow.reviews.get_for_case(tenant_id, case_id)
            if review is None or review.confirmed_by is None:
                raise ValidationError("成本交接前必须有已确认的人工审核")
            if case.version != review.expected_case_version:
                raise InvalidStateTransition("寻源审核绑定的 Case 版本已变化")
            case.opportunity_id = opportunity_id
            case.transition_to(CaseState.HANDED_TO_COSTING, changed_at=now)
            case.completed_at = now
            await uow.cases.update(tenant_id, case)
            await uow.bus.publish(
                SourcingCaseHandedToCosting(
                    tenant_id=tenant_id,
                    occurred_at=now,
                    case_id=case_id,
                    need_id=case.need_id,
                    opportunity_id=opportunity_id,
                    review_id=review.review_id,
                )
            )
            snapshot = await uow.handoffs.get_snapshot(
                tenant_id, case_id, review.review_id
            )
            if snapshot is None:
                raise SourcingHandoffInvariantError(
                    "成本交接快照缺少终态、版本、产品或价格证据"
                )
            return snapshot

    async def get_handoff_snapshot(
        self,
        tenant_id: TenantId,
        actor: SourcingActor,
        case_id: SourcingCaseId,
        review_id: SourcingReviewId,
    ) -> SourcingHandoffSnapshot:
        self._require(
            tenant_id,
            actor,
            SourcingAction.COSTING_HANDOFF_READ,
            SourcingScope.TENANT,
        )
        async with self._uow_factory(tenant_id) as uow:
            snapshot = await uow.handoffs.get_snapshot(tenant_id, case_id, review_id)
            if snapshot is None:
                raise SourcingHandoffInvariantError(
                    "成本交接快照不存在或终态版本不匹配"
                )
            return snapshot


__all__ = ("SourcingServiceImpl",)
