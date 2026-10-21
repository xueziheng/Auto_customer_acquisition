"""已分配客户通过当前归属接入既有触达公共服务。"""

from dataclasses import replace
from typing import Literal, cast

from domains.demand.schemas import HypothesisView
from domains.demand.service import DemandService
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.schemas import (
    EnrollmentCreateRequest,
    EnrollmentExecutionMode,
    EnrollmentView,
)
from domains.outreach.service import EnrollmentStopReason, OutreachService
from domains.prospecting.customer_followup import (
    CustomerAccessView,
    CustomerAllocationView,
    CustomerCampaignOption,
    CustomerContext,
    CustomerContextReader,
    CustomerDetailView,
    CustomerEnrollmentView,
    CustomerFollowupCommand,
    CustomerFollowupMode,
    CustomerFollowupOptions,
    CustomerHypothesisOption,
)
from domains.prospecting.schemas import (
    ContactPointKind,
    ProspectAccountView,
    ProspectContactDetailView,
    VerificationStatus,
)
from domains.prospecting.service import ProspectingService
from shared.errors import (
    PermissionDenied,
    PolicyViolation,
    TransientError,
    ValidationError,
)
from shared.schemas.identifiers import (
    CampaignId,
    CandidateAllocationId,
    ContactPointId,
    EnrollmentId,
    IdempotencyKey,
    NeedHypothesisId,
    ProspectAccountId,
)

from ..identity import RequestIdentity


def enrollment_view(value: EnrollmentView) -> CustomerEnrollmentView:
    """只投影跟进状态，不公开内部请求键、发件身份或发送材料。"""
    return CustomerEnrollmentView(
        **{name: getattr(value, name) for name in CustomerEnrollmentView.model_fields}
    )


def _actor(
    context: CustomerContext,
    account_id: ProspectAccountId,
    campaign_id: CampaignId | None = None,
) -> Actor:
    """真实角色与精确账户求交；管理员详情也收窄到当前客户。"""
    level = {
        "boss": ScopeLevel.TENANT,
        "manager": ScopeLevel.MANAGER,
        "sales": ScopeLevel.SELF,
    }[context.role]
    return Actor(
        str(context.employee_id),
        OutreachScope(
            level=level,
            allowed_account_ids=frozenset({account_id}),
            allowed_campaign_ids=frozenset({campaign_id}) if campaign_id else None,
        ),
        context.role,
    )


class CustomerFollowupApplication:
    def __init__(
        self,
        reader: CustomerContextReader,
        prospecting: ProspectingService,
        demand: DemandService,
        outreach: OutreachService,
        *,
        automatic_enabled: bool,
    ) -> None:
        self._reader, self._prospecting, self._demand, self._outreach = (
            reader,
            prospecting,
            demand,
            outreach,
        )
        self._automatic_enabled = automatic_enabled

    async def access(self, identity: RequestIdentity) -> CustomerAccessView:
        async with self._reader.guard(
            identity.tenant_id, identity.employee.employee_id, None
        ) as context:
            context.require_identity(
                identity.tenant_id,
                identity.employee.employee_id,
                identity.employee.user_id,
            )
            return CustomerAccessView(
                role=cast(Literal["boss", "manager", "sales"], context.role),
                can_search=context.role == "boss",
                can_assign=context.role == "boss",
            )

    async def _facts(
        self, context: CustomerContext
    ) -> tuple[
        ProspectAccountView | None,
        tuple[ProspectContactDetailView, ...],
        list[HypothesisView],
        tuple[str, ...],
    ]:
        allocation = context.allocation
        assert allocation is not None
        if allocation.account_id is None:
            return None, (), [], ("account_pending",)
        account = await self._prospecting.get_account(
            context.tenant_id, allocation.account_id
        )
        contacts = tuple(
            await self._prospecting.list_contacts_for_account(
                context.tenant_id, allocation.account_id
            )
        )
        hypotheses = await self._demand.list_account_outreach_hypotheses(
            context.tenant_id, allocation.account_id
        )
        reasons = []
        if (
            context.ownership_owner != allocation.employee_id
            or context.ownership_owner is None
        ):
            reasons.append("ownership_conflict")
        if not any(
            p.contact_point.kind is ContactPointKind.EMAIL
            and p.contact_point.verification is VerificationStatus.VERIFIED
            and p.contact_point.verified_at is not None
            for c in contacts
            for p in c.contact_points
        ):
            reasons.append("contact_unverified")
        if not account.entity_type:
            reasons.append("customer_type_missing")
        if not hypotheses:
            reasons.append("product_fit_missing")
        if not self._automatic_enabled:
            reasons.append("automatic_followup_disabled")
        return account, contacts, hypotheses, tuple(reasons)

    async def detail(
        self, identity: RequestIdentity, allocation_id: CandidateAllocationId
    ) -> CustomerDetailView:
        async with self._reader.guard(
            identity.tenant_id, identity.employee.employee_id, allocation_id
        ) as context:
            context.require_identity(
                identity.tenant_id,
                identity.employee.employee_id,
                identity.employee.user_id,
            )
            allocation = context.require(
                expected_user_id=identity.employee.user_id, own=False
            )
            account, contacts, _, reasons = await self._facts(context)
            values = []
            if allocation.account_id is not None:
                actor = _actor(context, allocation.account_id)
                values = await self._outreach.list_enrollments(
                    context.tenant_id, actor.scope, limit=200, actor=actor
                )
            return CustomerDetailView(
                allocation=CustomerAllocationView.from_allocation(allocation),
                account=account,
                owner=context.ownership_owner
                if allocation.account_id is not None
                else allocation.employee_id,
                contacts=contacts,
                enrollments=tuple(enrollment_view(v) for v in values),
                can_stop=context.ownership_owner
                == context.employee_id
                == allocation.employee_id,
                blocking_reasons=reasons,
                preparation=context.preparation,
            )

    async def options(
        self,
        identity: RequestIdentity,
        allocation_id: CandidateAllocationId,
        *,
        execution_mode: CustomerFollowupMode = "automatic",
    ) -> CustomerFollowupOptions:
        if execution_mode not in {"automatic", "manual"}:
            raise ValidationError("客户跟进执行模式无效")
        async with self._reader.guard(
            identity.tenant_id, identity.employee.employee_id, allocation_id
        ) as context:
            context.require_identity(
                identity.tenant_id,
                identity.employee.employee_id,
                identity.employee.user_id,
            )
            allocation = context.require(
                expected_user_id=identity.employee.user_id, own=False
            )
            account, contacts, hypotheses, reasons = await self._facts(context)
            if execution_mode == "manual":
                reasons = tuple(
                    reason
                    for reason in reasons
                    if reason != "automatic_followup_disabled"
                )
            campaigns = []
            if account is not None:
                for campaign_id in await self._reader.active_campaign_ids(
                    context.tenant_id
                ):
                    value = await self._outreach.get_campaign(
                        context.tenant_id,
                        CampaignId(campaign_id),
                        actor=_actor(
                            context, account.account_id, CampaignId(campaign_id)
                        ),
                    )
                    if value.state.value != "active":
                        continue
                    boundary = value.boundary
                    blockers = list(reasons)
                    if (
                        account.country not in boundary.markets
                        or account.entity_type not in boundary.target_entity_types
                        or not any(
                            h.category in boundary.allowed_categories
                            for h in hypotheses
                        )
                    ):
                        blockers.append("campaign_boundary_mismatch")
                    eligible = []
                    gate_reasons: list[str] = []
                    if (
                        (self._automatic_enabled or execution_mode == "manual")
                        and context.ownership_owner
                        == context.employee_id
                        == allocation.employee_id
                    ):
                        for contact in contacts:
                            for point in contact.contact_points:
                                cp = point.contact_point
                                if (
                                    cp.kind is ContactPointKind.EMAIL
                                    and cp.verification is VerificationStatus.VERIFIED
                                    and cp.verified_at is not None
                                ):
                                    checks = await self._outreach.assess_enrollment(
                                        context.tenant_id,
                                        value.campaign_id,
                                        account.account_id,
                                        cp.contact_point_id,
                                        campaign_version=value.version,
                                        actor=_actor(
                                            context,
                                            account.account_id,
                                            value.campaign_id,
                                        ),
                                    )
                                    if not checks:
                                        eligible.append(str(cp.contact_point_id))
                                    else:
                                        gate_reasons.extend(checks)
                    if not eligible:
                        blockers.extend(gate_reasons)
                        if (
                            context.ownership_owner != context.employee_id
                            or allocation.employee_id != context.employee_id
                        ):
                            blockers.append("not_customer_owner")
                    campaigns.append(
                        CustomerCampaignOption(
                            campaign_id=value.campaign_id,
                            name=value.name,
                            campaign_version=value.version,
                            markets=boundary.markets,
                            target_entity_types=boundary.target_entity_types,
                            allowed_categories=boundary.allowed_categories,
                            blocking_reasons=tuple(dict.fromkeys(blockers)),
                            eligible_contact_point_ids=tuple(eligible),
                            can_start=bool(eligible) and not blockers,
                        )
                    )
            if not campaigns and account is not None:
                reasons = (*reasons, "no_active_campaign")
            verified = tuple(
                replace(
                    c,
                    contact_points=tuple(
                        p
                        for p in c.contact_points
                        if p.contact_point.kind is ContactPointKind.EMAIL
                        and p.contact_point.verification is VerificationStatus.VERIFIED
                        and p.contact_point.verified_at is not None
                    ),
                )
                for c in contacts
            )
            return CustomerFollowupOptions(
                allocation_id=allocation.id,
                account_id=allocation.account_id,
                contacts=tuple(c for c in verified if c.contact_points),
                hypotheses=tuple(
                    CustomerHypothesisOption(
                        hypothesis_id=h.hypothesis_id,
                        category=h.category,
                        status=h.status,
                    )
                    for h in hypotheses
                ),
                campaigns=tuple(campaigns),
                blocking_reasons=reasons,
                execution_mode=execution_mode,
            )

    async def start(
        self,
        identity: RequestIdentity,
        allocation_id: CandidateAllocationId,
        command: CustomerFollowupCommand,
        idempotency_key: str,
    ) -> CustomerEnrollmentView:
        async with self._reader.guard(
            identity.tenant_id, identity.employee.employee_id, allocation_id
        ) as context:
            context.require_identity(
                identity.tenant_id,
                identity.employee.employee_id,
                identity.employee.user_id,
            )
            allocation = context.require(
                expected_user_id=identity.employee.user_id, own=True
            )
            account_id = allocation.account_id
            assert account_id is not None
            actor = _actor(context, account_id, CampaignId(command.campaign_id))
            request = EnrollmentCreateRequest(
                account_id,
                ContactPointId(command.contact_point_id),
                IdempotencyKey(idempotency_key),
                NeedHypothesisId(command.source_hypothesis_id),
                command.campaign_version,
                execution_mode=EnrollmentExecutionMode(command.execution_mode),
            )
            existing = await self._outreach.replay_enrollment(
                context.tenant_id, CampaignId(command.campaign_id), request, actor=actor
            )
            if existing is not None:
                return enrollment_view(existing)
            hypothesis = await self._demand.get_hypothesis(
                context.tenant_id, NeedHypothesisId(command.source_hypothesis_id)
            )
            if (
                hypothesis.account_id != account_id
                or hypothesis.status not in {"inferred", "contacting"}
                or not hypothesis.evidence
            ):
                raise ValidationError("需求假设必须属于该客户且仍有有效证据")
            # 真实同客户证据由Demand复核，公开邮箱不能当成已验证联系人。
            await self._demand.get_hypothesis_for_discovery(
                context.tenant_id, NeedHypothesisId(command.source_hypothesis_id)
            )
            campaign = await self._outreach.get_campaign(
                context.tenant_id, CampaignId(command.campaign_id), actor=actor
            )
            if hypothesis.category not in campaign.boundary.allowed_categories:
                raise PolicyViolation("产品适配证据超出批准开发任务范围")
            if command.execution_mode == "automatic" and not self._automatic_enabled:
                raise TransientError(
                    "自动跟进未启用：联系人、发件身份与回复配置尚未完整装配"
                )
            value = await self._outreach.enroll(
                context.tenant_id,
                CampaignId(command.campaign_id),
                request,
                actor=actor,
            )
            return enrollment_view(value)

    async def stop(
        self,
        identity: RequestIdentity,
        allocation_id: CandidateAllocationId,
        enrollment_id: EnrollmentId,
    ) -> CustomerEnrollmentView:
        async with self._reader.guard(
            identity.tenant_id, identity.employee.employee_id, allocation_id
        ) as context:
            context.require_identity(
                identity.tenant_id,
                identity.employee.employee_id,
                identity.employee.user_id,
            )
            allocation = context.require(
                expected_user_id=identity.employee.user_id, own=True
            )
            assert allocation.account_id is not None
            actor = _actor(context, allocation.account_id)
            # 公开服务自己核对Enrollment的真实account；不信任客户端资源组合。
            value = await self._outreach.get_enrollment(
                context.tenant_id, enrollment_id, actor=actor
            )
            if value.account_id != allocation.account_id:
                raise PermissionDenied("跟进记录不属于该客户")
            actor = _actor(context, allocation.account_id, value.campaign_id)
            return enrollment_view(
                await self._outreach.stop_enrollment(
                    context.tenant_id,
                    enrollment_id,
                    EnrollmentStopReason.MANUAL,
                    actor=actor,
                )
            )
