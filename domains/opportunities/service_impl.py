"""OpportunityService 核心实现（S2-10：UoW / create / assign / transition / mark_lost / mark_won）。

依赖注入：``uow_factory``（返回绑定同租户的 ``OpportunityUnitOfWork``）、``scorer``
（策略注入的 ``OpportunityScorer``）、``handoff_policy``（本任务保留，S2-11 使用）、
``now``（时钟，默认 ``datetime.now``）。

硬边界：领域层不 import 外部 SDK。唯一并发恢复**仅限** ``sqlalchemy.exc.IntegrityError``
且 ``orig`` SQLSTATE 为 23505（unique_violation）时重查既有记录；其余异常（领域错误、
编程错误、其他 DB 错误、同名冒充异常）一律原样上抛——不吞、不当作幂等成功。
判定函数只查异常类名/模块与 SQLSTATE，不在领域层引用 sqlalchemy 异常类型。
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from domains.opportunities.errors import (
    AgentInferenceProvenanceError,
    MissingFieldProvenanceError,
    MissingLossReasonError,
)
from domains.opportunities.models import (
    ALLOWED_TRANSITIONS,
    CRITICAL_FIELDS,
    HandoffPolicy,
    LossReason,
    LossRecord,
    Opportunity,
    OpportunityState,
)
from domains.opportunities.repository import OpportunityUnitOfWork
from domains.opportunities.schemas import OpportunityCreateRequest
from domains.opportunities.scoring import OpportunityScorer, ScoringInput
from shared.errors import InvalidStateTransition, TradeOSError, ValidationError
from shared.events.catalog import OpportunityLost, OpportunityQualified, OpportunityWon
from shared.schemas.evidence import ConfidenceTier
from shared.schemas.identifiers import (
    EmployeeId,
    LossRecordId,
    OpportunityId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
    new_id,
)
from shared.schemas.provenance import SourceType


def _is_unique_violation(exc: BaseException) -> bool:
    """是否 SQLAlchemy 唯一约束冲突（``sqlalchemy.exc.IntegrityError`` + SQLSTATE 23505）。

    领域层不 import 外部 SDK：只检查异常**类型名与模块**（防同名自定义异常冒充）、
    ``orig.sqlstate``/``orig.pgcode`` 是否等于 ``"23505"``。**不按错误字符串猜**，
    也不对任意非领域异常无条件重查——只有确认为唯一约束冲突才进入幂等恢复。
    """
    if exc.__class__.__name__ != "IntegrityError":
        return False
    if exc.__class__.__module__ != "sqlalchemy.exc":
        return False  # 非 SQLAlchemy 模块的同名异常不得冒充唯一冲突
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    state = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
    return state == "23505"


def validate_present_critical_provenance(request: OpportunityCreateRequest) -> None:
    """校验 request 中 **present** 的关键字段都有 Provenance（硬边界 4），且无 Agent 推断（硬边界 5）。

    ``account_name``/``country`` 必然 present；其余 CRITICAL_FIELDS 只在值非 None 时要求来源。
    缺失抛 ``MissingFieldProvenanceError``；来源为 ``AGENT_INFERENCE`` 抛
    ``AgentInferenceProvenanceError``。纯函数，无 IO。
    """
    for field in CRITICAL_FIELDS:
        if getattr(request, field, None) is not None:
            prov = request.field_provenance.get(field)
            if prov is None:
                raise MissingFieldProvenanceError(f"关键字段 {field} 缺来源（硬边界 4）")
            if prov.source_type == SourceType.AGENT_INFERENCE:
                raise AgentInferenceProvenanceError(
                    f"关键字段 {field} 来源是 Agent 推断（硬边界 5）：机会只持久化事实"
                )


class OpportunityServiceImpl:
    """``OpportunityService`` 的核心实现（S2-10 范围；handoff/查询方法 S2-11 落地）。"""

    def __init__(
        self,
        uow_factory: Callable[[], OpportunityUnitOfWork],
        scorer: OpportunityScorer,
        handoff_policy: HandoffPolicy,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._uow_factory = uow_factory
        self._scorer = scorer
        self._handoff_policy = handoff_policy  # S2-11 使用；本任务保留不实现
        self._now = now

    async def create_from_need(
        self, tenant_id: TenantId, request: OpportunityCreateRequest
    ) -> OpportunityId | None:
        """从已验证需求创建机会。

        - 幂等：同一 ``need_id`` 已有机会 → 返回既有 ID。
        - 门槛失败：只保留失败快照（scorer 已落库），返回 None，不建机会、不发事件。
        - 通过：建机会、保存 present 关键字段 provenance、发布 ``OpportunityQualified``。
        - 唯一并发：commit 阶段的 DB 异常（``IntegrityError`` 属非领域异常）→ 新 UoW
          重查既有；只有确实找到才返回既有，否则原异常重抛。
        """
        try:
            async with self._uow_factory() as uow:
                existing = await uow.opportunities.find_by_need(
                    tenant_id, ValidatedNeedId(request.need_id)
                )
                if existing is not None:
                    return existing.opportunity_id

                validate_present_critical_provenance(request)

                opportunity_id = OpportunityId(new_id("opp"))
                scoring_input = ScoringInput(
                    has_verified_contact=request.has_verified_contact,
                    evidence_tier=ConfidenceTier(request.evidence_tier),
                    category_allowed=request.category_allowed,
                    minimum_order_value=request.minimum_order_value,
                    estimated_order_value=request.estimated_order_value,
                    supply_available=request.supply_available,
                    is_repeat_buyer_likely=request.is_repeat_buyer_likely,
                )
                snapshot = await self._scorer.score(
                    uow.snapshots, tenant_id, opportunity_id, scoring_input
                )
                if snapshot.failed_gates:
                    return None  # 门槛失败：失败快照已入库，不建机会/不发事件

                opp = Opportunity(
                    opportunity_id=opportunity_id,
                    tenant_id=tenant_id,
                    account_id=ProspectAccountId(request.account_id),
                    need_id=ValidatedNeedId(request.need_id),
                    product_category=request.product_category,
                    created_at=self._now(),
                    account_name=request.account_name,
                    country=request.country,
                    quantity=request.quantity,
                    spec_summary=request.spec_summary,
                    application=request.application,
                    destination=request.destination,
                    required_by=request.required_by,
                    target_price=request.target_price,
                    current_supply_solution=request.current_supply_solution,
                    current_supply_problem=request.current_supply_problem,
                )
                await uow.opportunities.add(opp)
                # 只保存 CRITICAL_FIELDS 中在 request 上实际 present（值非 None）的字段；
                # account_name/country 必然 present。不保存任何未知/多余键。
                for field in CRITICAL_FIELDS:
                    if getattr(request, field, None) is not None:
                        await uow.provenance.save(
                            tenant_id,
                            "opportunity",
                            opp.opportunity_id,
                            field,
                            request.field_provenance[field],
                        )
                await uow.bus.publish(
                    OpportunityQualified(
                        tenant_id=tenant_id,
                        occurred_at=self._now(),
                        run_id=None,
                        opportunity_id=opp.opportunity_id,
                        rank_bucket=snapshot.rank_bucket,
                    )
                )
                return opp.opportunity_id
        except TradeOSError:
            # 领域错误（校验/状态/币种）不重试，直接上抛。
            raise
        except Exception as exc:
            # 唯一并发：仅当确认为 DB 唯一约束冲突（SQLSTATE 23505）才进入幂等恢复；
            # 编程错误/其他 DB 错误一律原样抛出（不吞异常、不当幂等成功）。
            if not _is_unique_violation(exc):
                raise
            async with self._uow_factory() as uow:
                existing = await uow.opportunities.find_by_need(
                    tenant_id, ValidatedNeedId(request.need_id)
                )
            if existing is not None:
                return existing.opportunity_id
            raise

    async def assign(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        owner: EmployeeId,
        assigned_by: EmployeeId,
    ) -> None:
        """分配负责人并落审计字段（``assigned_at`` 用注入时钟）；失败不静默。"""
        async with self._uow_factory() as uow:
            ok = await uow.opportunities.assign_owner(
                tenant_id, opportunity_id, owner, assigned_by, self._now()
            )
            if not ok:
                raise InvalidStateTransition(
                    f"机会 {opportunity_id} 不存在或已变更，无法分配"
                )

    async def transition(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        target: OpportunityState,
    ) -> None:
        """状态推进：拒绝 WON/LOST 走普通转换；读当前态→状态机校验→原子 advance_state。"""
        if target in (OpportunityState.WON, OpportunityState.LOST):
            raise InvalidStateTransition(
                f"终态 {target.value} 不能走普通 transition；必须经 mark_lost / mark_won"
            )
        async with self._uow_factory() as uow:
            opp = await uow.opportunities.get(tenant_id, opportunity_id)
            if opp is None:
                raise ValidationError(f"机会 {opportunity_id} 不存在")
            if not opp.can_transition_to(target):
                allowed = sorted(ALLOWED_TRANSITIONS[opp.state], key=lambda s: s.value)
                raise InvalidStateTransition(
                    f"非法转换：{opp.state.value} → {target.value}；"
                    f"允许：{', '.join(s.value for s in allowed)}"
                )
            ok = await uow.opportunities.advance_state(
                tenant_id, opportunity_id, opp.state, target
            )
            if not ok:
                raise InvalidStateTransition(
                    f"并发已变更：{opp.state.value} → {target.value} 未生效"
                )

    async def mark_lost(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        reason: LossReason | None,
        actor: EmployeeId,
        confirmed_at: datetime,
        detail: str | None = None,
    ) -> None:
        """终结机会（**人工确认动作**）：close_lost_if_state → 只增 LossRecord → 发布 OpportunityLost。

        ``reason=None`` 先抛 ``MissingLossReasonError``（反馈闭环）。``died_at_state`` 记录
        关闭前状态；``confirmed_by/confirmed_at`` 与 ``actor/confirmed_at`` 一致；
        ``recorded_at`` 用注入时钟。终态只能走 close_*，禁止普通 update。
        """
        if reason is None:
            raise MissingLossReasonError("终结机会必须带 LossReason（反馈闭环）")
        async with self._uow_factory() as uow:
            opp = await uow.opportunities.get(tenant_id, opportunity_id)
            if opp is None:
                raise ValidationError(f"机会 {opportunity_id} 不存在")
            if opp.state in (OpportunityState.WON, OpportunityState.LOST):
                raise InvalidStateTransition(
                    f"机会已处于终态 {opp.state.value}，不能重复终结"
                )
            ok = await uow.opportunities.close_lost_if_state(
                tenant_id, opportunity_id, opp.state, reason, detail, actor, confirmed_at
            )
            if not ok:
                raise InvalidStateTransition(
                    f"并发已变更：机会 {opportunity_id} 与预期状态不符，未终结"
                )
            record = LossRecord(
                loss_record_id=LossRecordId(new_id("loss")),
                tenant_id=tenant_id,
                opportunity_id=opportunity_id,
                loss_reason=reason,
                died_at_state=opp.state,  # 关闭前状态
                confirmed_by=actor,
                confirmed_at=confirmed_at,
                recorded_at=self._now(),
                detail=detail,
            )
            await uow.loss_records.add(tenant_id, record)
            await uow.bus.publish(
                OpportunityLost(
                    tenant_id=tenant_id,
                    occurred_at=confirmed_at,
                    run_id=None,
                    opportunity_id=opportunity_id,
                    loss_reason=reason.value,
                    died_at_state=opp.state.value,
                )
            )

    async def mark_won(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        actor: EmployeeId,
        confirmed_at: datetime,
    ) -> None:
        """终结为成交（**人工确认动作**）：仅 NEGOTIATING → close_won_if_state → 发布 OpportunityWon。"""
        async with self._uow_factory() as uow:
            opp = await uow.opportunities.get(tenant_id, opportunity_id)
            if opp is None:
                raise ValidationError(f"机会 {opportunity_id} 不存在")
            if opp.state != OpportunityState.NEGOTIATING:
                raise InvalidStateTransition(
                    f"mark_won 仅能从 negotiating 转入；当前状态 {opp.state.value}"
                )
            ok = await uow.opportunities.close_won_if_state(
                tenant_id, opportunity_id, actor, confirmed_at
            )
            if not ok:
                raise InvalidStateTransition(f"并发已变更：机会 {opportunity_id} 未成交")
            await uow.bus.publish(
                OpportunityWon(
                    tenant_id=tenant_id,
                    occurred_at=confirmed_at,
                    run_id=None,
                    opportunity_id=opportunity_id,
                    closed_by=actor,
                )
            )
