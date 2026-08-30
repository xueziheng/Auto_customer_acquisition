"""Sourcing V2 的 tenant-bound、authorizer-first 领域服务实现。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

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
    MatchExplanation,
    MatchLadderRung,
    PriceRejectionReason,
    PublicSourcingPlan,
    SourcingCase,
    SourcingReview,
    SourcingStopCode,
    SpecComparison,
    SpecMatchLevel,
    SupplierCandidate,
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
    OpenSourcingCase,
    PublicSourcingPlanCommand,
    SourcingHandoffSnapshot,
    SourcingReviewCommand,
    SpecComparisonView,
)
from shared.errors import InvalidStateTransition, ValidationError
from shared.events.catalog import (
    SourcingCandidatesReady,
    SourcingCaseHandedToCosting,
    SourcingCaseOpened,
)
from shared.schemas.identifiers import (
    EmployeeId,
    OpportunityId,
    SourcingCaseId,
    SourcingPlanId,
    SourcingReviewId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
    TenantId,
    new_id,
)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("寻源服务时间必须含时区")
    return value


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


def _candidate_from_submission(
    tenant_id: TenantId,
    case: SourcingCase,
    submission: CandidateSubmission,
    *,
    actor: SourcingActor,
    now: datetime,
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
            )
            for item in submission.specs
        ]
    except ValueError as exc:
        raise ValidationError("候选规格匹配等级无效") from exc
    evidence = EvidenceSnapshot(
        url=submission.evidence_url,
        observed_at=now,
        content_hash=submission.evidence_hash,
        artifact_ref=submission.evidence_artifact_ref,
    )
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
        indicative_price_tiers=dict(submission.indicative_price_tiers),
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
            )
            for item in candidate.verified_specs
        ],
        quoted_prices=dict(candidate.indicative_price_tiers),
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
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not callable(uow_factory):
            raise ValidationError("寻源事务依赖无效")
        if not isinstance(authorizer, SourcingAuthorizer):
            raise ValidationError("寻源授权依赖无效")
        self._uow_factory = uow_factory
        self._authorizer = authorizer
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
        now = _aware(self._now())
        async with self._uow_factory(tenant_id) as uow:
            existing = await uow.cases.get_by_trigger(tenant_id, command.trigger_key)
            if existing is not None:
                if (
                    existing.need_id != command.need.need_id
                    or existing.workflow_version != command.workflow_version
                ):
                    raise ValidationError("开案幂等键已绑定不同需求或工作流版本")
                return existing.case_id
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
            await uow.cases.add(tenant_id, case)
            await uow.bus.publish(
                SourcingCaseOpened(
                    tenant_id=tenant_id,
                    occurred_at=now,
                    case_id=case.case_id,
                    need_id=case.need_id,
                )
            )
            return case.case_id

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
            case = _case_required(await uow.cases.get(tenant_id, case_id))
            if case.state is not CaseState.DISCOVERING:
                raise InvalidStateTransition(
                    f"寻源案例处于 {case.state.value}，不能保存公开计划"
                )
            if command.case_id != case_id:
                raise ValidationError("公开寻源计划案例不匹配")
            checks = await uow.checks.list_for_case(tenant_id, case_id)
            if [item.rung.value for item in checks] != [1, 2, 3, 4, 5]:
                raise ValidationError("公开寻源前必须连续完成匹配梯子 1–5")
            if command.expected_case_version != case.version:
                raise SourcingPlanStaleError("公开寻源计划绑定的 Case 版本已变化")
            active = await uow.plans.get_active_for_case(tenant_id, case_id)
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
            if plan.plan_hash != expected_plan_hash:
                raise SourcingPlanStaleError("确认哈希与当前公开寻源计划不一致")
            active = await uow.plans.get_active_for_case(tenant_id, plan.case_id)
            if active is None or active.plan_id != plan.plan_id:
                raise SourcingPlanStaleError("公开寻源计划已被新版本取代")
            case = _case_required(await uow.cases.get(tenant_id, plan.case_id))
            if case.state is not CaseState.DISCOVERING:
                raise InvalidStateTransition(
                    f"寻源案例处于 {case.state.value}，不能确认公开计划"
                )
            confirmed = plan.confirm(_employee(actor), confirmed_at=now)
            await uow.plans.update(tenant_id, confirmed)
            case.transition_to(CaseState.VERIFYING, changed_at=now)
            await uow.cases.update(tenant_id, case)
            return confirmed

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
        now = _aware(self._now())
        async with self._uow_factory(tenant_id) as uow:
            case = _case_required(await uow.cases.get(tenant_id, case_id))
            if case.state is not CaseState.VERIFYING:
                raise InvalidStateTransition(
                    f"寻源案例处于 {case.state.value}，不能提交候选"
                )
            candidate = _candidate_from_submission(
                tenant_id, case, submission, actor=actor, now=now
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
            if not any(tier <= quantity for tier in candidate.indicative_price_tiers):
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

    async def mark_candidates_ready(
        self,
        tenant_id: TenantId,
        case_id: SourcingCaseId,
        option_ids: tuple[SourcingSupplyOptionId, ...],
        candidate_ids: tuple[SupplierCandidateId, ...],
        *,
        actor: SourcingActor,
    ) -> None:
        self._require(
            tenant_id,
            actor,
            SourcingAction.FACT_PUBLISH,
            SourcingScope.SYSTEM,
        )
        if not option_ids:
            raise ValidationError("候选就绪事件必须至少包含一个供给选项")
        if len(set(option_ids)) != len(option_ids) or len(set(candidate_ids)) != len(
            candidate_ids
        ):
            raise ValidationError("候选就绪事件 ID 不得重复")
        now = _aware(self._now())
        async with self._uow_factory(tenant_id) as uow:
            case = _case_required(await uow.cases.get(tenant_id, case_id))
            if case.state is not CaseState.VERIFYING:
                raise InvalidStateTransition(
                    f"寻源案例处于 {case.state.value}，不能标记候选就绪"
                )
            for option_id in option_ids:
                option = await uow.options.get(tenant_id, option_id)
                if (
                    option is None
                    or option.case_id != case_id
                    or not option.is_qualified
                ):
                    raise ValidationError("供给选项不存在、不属于本 Case 或未合格")
            for candidate_id in candidate_ids:
                candidate = await uow.candidates.get(tenant_id, candidate_id)
                if (
                    candidate is None
                    or candidate.case_id != case_id
                    or candidate.rejected
                    or not candidate.passes_verification()[0]
                ):
                    raise ValidationError("候选不存在、不属于本 Case 或未合格")
            case.transition_to(CaseState.CANDIDATES_READY, changed_at=now)
            await uow.cases.update(tenant_id, case)
            await uow.bus.publish(
                SourcingCandidatesReady(
                    tenant_id=tenant_id,
                    occurred_at=now,
                    case_id=case_id,
                    option_ids=option_ids,
                    candidate_ids=candidate_ids,
                )
            )

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
        if not isinstance(command, SourcingReviewCommand):
            raise ValidationError("寻源审核命令无效")
        now = _aware(self._now())
        async with self._uow_factory(tenant_id) as uow:
            case = _case_required(await uow.cases.get(tenant_id, case_id))
            if case.state is not CaseState.CANDIDATES_READY:
                raise InvalidStateTransition(
                    f"寻源案例处于 {case.state.value}，不能提交审核"
                )
            if await uow.reviews.get_for_case(tenant_id, case_id) is not None:
                raise ValidationError("该寻源案例已有审核事实")
            selections = (command.primary_option_id, *command.alternate_option_ids)
            for option_id in selections:
                option = await uow.options.get(tenant_id, option_id)
                if (
                    option is None
                    or option.case_id != case_id
                    or not option.is_qualified
                ):
                    raise ValidationError("审核只能选择本 Case 的合格供给选项")
            review = SourcingReview.create(
                review_id=SourcingReviewId(new_id("srv")),
                tenant_id=tenant_id,
                case_id=case_id,
                command=command,
                submitted_by=_employee(actor),
                submitted_at=now,
                actual_case_version=case.version,
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
            case = _case_required(await uow.cases.get(tenant_id, review.case_id))
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
        actor: SourcingActor,
    ) -> SourcingHandoffSnapshot:
        self._require(
            tenant_id,
            actor,
            SourcingAction.WORKFLOW_PROGRESS,
            SourcingScope.SYSTEM,
        )
        if not str(opportunity_id).strip():
            raise ValidationError("成本交接必须绑定可信 Opportunity 引用")
        now = _aware(self._now())
        async with self._uow_factory(tenant_id) as uow:
            case = _case_required(await uow.cases.get(tenant_id, case_id))
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
