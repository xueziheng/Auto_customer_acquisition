"""持久客户准备消费者；只编排规范公共服务，不负责发送或另建扫描循环。"""
from collections.abc import Callable
from datetime import UTC, datetime

from domains.compliance.permissions import ComplianceActor, ComplianceScope
from domains.compliance.schemas import CountryPolicyAction, normalize_country_key
from domains.compliance.service import ComplianceService
from domains.demand.schemas import SignalCaptureRequest
from domains.demand.service import DemandService
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.outreach.permissions import Actor, OutreachScope, ScopeLevel
from domains.outreach.schemas import (
    CampaignView,
    EnrollmentCreateRequest,
    EnrollmentOwnershipBinding,
    SuppressionTarget,
)
from domains.outreach.service import OutreachService, SendingIdentityEligibilityProvider
from domains.prospecting.agent_profile import AgentAccountProfilePatch
from domains.prospecting.inventory_followup import PreparationBinding, PreparationClaim
from domains.prospecting.schemas import (
    ContactPointKind,
    ContactType,
    DiscoveredContactRequest,
    LegalBasisInput,
    LegalBasisType,
    SubjectType,
    VerificationRecordRequest,
    VerificationStatus,
)
from domains.prospecting.service import (
    ProspectingService,
    ProspectingServicePlanStore,
    contact_mission_key,
    tier_policy,
)
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import (
    CampaignId,
    CandidateAllocationId,
    ContactPointId,
    IdempotencyKey,
    NeedHypothesisId,
    ProspectAccountId,
    TenantId,
)
from workflows.account_discovery.ports import ContactVerifier
from workflows.customer_preparation.authority import (
    CustomerAgentScopeAuthority,
    EmployeeScopeFactory,
)
from workflows.customer_preparation.ports import (
    AgentPreparationTransactions,
    PreparationExtractor,
    PreparationQueue,
)


class PreparationBlocked(Exception):
    """固定可见阻断原因，不携带模型/Provider原文。"""
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def preparation_actor(claim: PreparationBinding) -> Actor:
    """调用前由真实当前boss身份核验，作用域进一步收窄至精确账户及Campaign。"""
    return Actor(str(claim.authorization.actor_employee_id), OutreachScope(
        level=ScopeLevel.TENANT,
        allowed_account_ids=frozenset({ProspectAccountId(claim.result_refs["account_id"])}) if "account_id" in claim.result_refs else None,
        allowed_campaign_ids=frozenset({CampaignId(claim.authorization.scope.campaign_id)})), "boss")


async def require_cold_preparation_policy(binding: PreparationBinding, country: str, *, compliance: ComplianceService, outreach: OutreachService) -> None:
    """准备与真实发送共用当前COLD_B2B_EMAIL及已实现requirement检查。"""
    claim, tenant = binding, binding.tenant_id
    try:
        decision = await compliance.get_country_policy_decision(tenant, country, CountryPolicyAction.COLD_B2B_EMAIL,
            actor=ComplianceActor("system:customer-preparation-policy", tenant, ComplianceScope.SYSTEM, "system"))
    except Exception:  # noqa: BLE001 -- 脱敏固定阻断，不把故障当允许
        raise PreparationBlocked("country_policy_unavailable") from None
    if decision.country_key != normalize_country_key(country) or decision.action is not CountryPolicyAction.COLD_B2B_EMAIL:
        raise PreparationBlocked("country_policy_unavailable")
    if not decision.configured or not decision.allowed or decision.active_version_id is None or decision.content_hash is None:
        raise PreparationBlocked("country_policy_denied")
    if not set(decision.requirements).issubset({"retain.assessment_ref", "honor_opt_out"}):
        raise PreparationBlocked("country_policy_requirement_unsupported")
    actor = preparation_actor(claim)
    targets = [SuppressionTarget(account_id=ProspectAccountId(claim.result_refs["account_id"]))]
    if "contact_point_id" in claim.result_refs:
        targets.append(SuppressionTarget(contact_point_id=ContactPointId(claim.result_refs["contact_point_id"])))
    for target in targets:
        if await outreach.is_suppressed(tenant, target, actor=actor):
            raise PreparationBlocked("enrollment_denied")



class CustomerPreparationConsumer:
    def __init__(self, *, tenant_id: TenantId, queue: PreparationQueue,
                 authority: CustomerAgentScopeAuthority, employees: EmployeeScopeFactory,
                 prospecting: ProspectingService, outreach: OutreachService,
                 compliance: ComplianceService, senders: SendingIdentityEligibilityProvider,
                 transactions: AgentPreparationTransactions, extractor: PreparationExtractor | None,
                 verifier: ContactVerifier | None, service_plans: ProspectingServicePlanStore | None = None, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self.service_plans = service_plans
        self.tenant, self.queue, self.authority = tenant_id, queue, authority
        self.employees, self.prospecting, self.outreach = employees, prospecting, outreach
        self.compliance, self.senders = compliance, senders
        self.transactions, self.extractor, self.verifier, self.now = transactions, extractor, verifier, now

    async def contact_budget(self, claim: PreparationClaim) -> None:
        """公开邮箱也占同一联系人处理预留；验证重读回执，不二次扣额。"""
        if self.service_plans is None:
            return
        state = await self.service_plans.get(self.tenant)
        if state is None:
            return
        key = contact_mission_key(claim.authorization.scope.campaign_id, claim.result_refs["account_id"])
        receipt = await self.service_plans.reservation(self.tenant, key=key, now=self.now())
        if receipt is None:
            await self.service_plans.reserve(self.tenant, key=key, candidates=0, searches=0,
                contacts=1, now=self.now())
            receipt = await self.service_plans.reservation(self.tenant, key=key, now=self.now())
        if (receipt is None or not state.period_started_at <= self.now() < state.period_ends_at
            or receipt.period_started_at != state.period_started_at or receipt.contacts < 1
            or tier_policy(state.tier).max_contacts_per_company < 1):
            raise PreparationBlocked("service_plan_inactive")

    async def guard(self, claim: PreparationClaim) -> tuple[CampaignView, list[dict[str, object]]]:
        await self.queue.require_preparation_claim(self.tenant, claim)
        campaign, context = await self.authority.check(claim.authorization)
        if "account_id" in claim.result_refs:
            async with self.employees(self.tenant) as service:
                ownership = await service.get_ownership(self.tenant, ProspectAccountId(claim.result_refs["account_id"]),
                    actor=EmployeeActor(str(claim.authorization.actor_employee_id), EmployeeScope.TENANT, "boss"))
            if (ownership is None or ownership.tenant_id != self.tenant
                or ownership.owner != claim.authorization.owner_employee_id
                or ownership.locked_at != claim.ownership_locked_at):
                raise PreparationBlocked("ownership_changed")
        return campaign, context

    async def policy(self, claim: PreparationBinding, country: str) -> None:
        await require_cold_preparation_policy(claim, country, compliance=self.compliance, outreach=self.outreach)

    async def scan(self) -> None:
        """每轮至多一项，由现singleton cycle调用；故障状态持久，不打印原文。"""
        initial = await self.queue.claim_preparation(self.tenant)
        if initial is None:
            return
        claim: PreparationClaim = initial
        external = False
        try:
            campaign, _ = await self.guard(claim)
            if self.verifier is None:
                raise PreparationBlocked("verification_unavailable")
            if self.extractor is None:
                raise PreparationBlocked("evidence_unavailable")
            sender_ready = False
            for identity in campaign.boundary.sender_identity_ids:
                sender_snapshot = await self.senders.get_sending_identity_eligibility(
                    self.tenant, identity
                )
                if (
                    sender_snapshot.tenant_id == self.tenant
                    and sender_snapshot.identity_id == identity
                    and sender_snapshot.authentication_passed
                    and sender_snapshot.sendable
                ):
                    sender_ready = True
                    break
            if not sender_ready:
                raise PreparationBlocked("sender_unavailable")
            if claim.stage == "authorize":
                claim = await self.queue.checkpoint_preparation(
                    self.tenant, claim, stage="assign"
                )
            if claim.stage == "assign":
                await self.guard(claim)
                claim = await self.queue.assign_preparation(self.tenant, claim)
            allocation = await self.queue.get_allocation(
                self.tenant, CandidateAllocationId(claim.allocation_id)
            )
            if (
                allocation is None
                or allocation.tenant_id != self.tenant
                or str(allocation.account_id) != claim.result_refs.get("account_id")
            ):
                raise PreparationBlocked("ownership_changed")
            await self.policy(claim, allocation.candidate.country)
            if claim.stage == "evidence":
                await self.contact_budget(claim)
                campaign, context = await self.guard(claim)
                claim = await self.queue.checkpoint_preparation(
                    self.tenant, claim, stage="evidence", external_started=True
                )
                external = True

                async def external_guard() -> None:
                    await self.guard(claim)
                    await self.policy(claim, allocation.candidate.country)

                extracted = await self.extractor.extract(
                    claim,
                    allocation,
                    context,
                    external_guard,
                    target_entity_types=campaign.boundary.target_entity_types,
                )
                await external_guard()
                observation, snapshot = extracted.observation, extracted.snapshot
                if not observation.matched:
                    raise PreparationBlocked("product_fit_missing")
                observation.require_evidence(
                    snapshot.text,
                    allocation.candidate.canonical_domain,
                    campaign.boundary.allowed_categories,
                )
                if (
                    snapshot.tenant_id != self.tenant
                    or observation.entity_type.value
                    not in campaign.boundary.target_entity_types
                ):
                    raise PreparationBlocked("evidence_invalid")
                policy = campaign.boundary.customer_agent_policy
                if policy is None or not policy.assessment_ref:
                    raise PreparationBlocked("contact_basis_missing")

                async def persist(
                    prospecting: ProspectingService, demand: DemandService
                ) -> dict[str, str]:
                    await self.authority.check(claim.authorization)
                    account = await prospecting.fill_account_profile_from_evidence(
                        self.tenant,
                        ProspectAccountId(claim.result_refs["account_id"]),
                        AgentAccountProfilePatch(
                            entity_type=observation.entity_type,
                            industry=observation.industry,
                            artifact_id=snapshot.artifact_id,
                            source_url=snapshot.source_url,
                            page_hash=snapshot.page_hash,
                            extracted_by=extracted.extracted_by,
                            extracted_at=extracted.extracted_at,
                        ),
                        evidence=extracted.evidence,
                    )
                    signal = await demand.capture_signal(
                        self.tenant,
                        SignalCaptureRequest(
                            signal_type="complementary_category",
                            entity_name=account.name,
                            raw_observation=observation.fit_quote,
                            observed_at=snapshot.observed_at,
                            source_type="web_page",
                            source_id=snapshot.page_hash,
                            extracted_by=extracted.extracted_by,
                            source_url=snapshot.source_url,
                            page_hash=snapshot.page_hash,
                            snapshot_artifact_ref=str(snapshot.artifact_id),
                        ),
                    )
                    hypothesis = await demand.create_hypothesis(
                        self.tenant,
                        account.account_id,
                        observation.category,
                        [signal],
                        observation.fit_reasoning,
                        extracted.extracted_by,
                    )
                    contact = await prospecting.record_discovered_contact(
                        self.tenant,
                        DiscoveredContactRequest(
                            account_id=account.account_id,
                            kind=ContactPointKind.EMAIL,
                            value=observation.public_email,
                            legal_basis=LegalBasisInput(
                                basis=LegalBasisType.LEGITIMATE_INTEREST,
                                subject_type=SubjectType.LEGAL_ENTITY,
                                contact_type=ContactType.ROLE_BASED,
                                source=f"campaign:{campaign.campaign_id}:v{campaign.version}",
                                collected_at=snapshot.observed_at,
                                source_url=snapshot.source_url,
                                assessment_ref=policy.assessment_ref,
                            ),
                        ),
                    )
                    await self.authority.check(claim.authorization)
                    return {
                        "signal_id": str(signal),
                        "hypothesis_id": str(hypothesis),
                        "contact_point_id": str(contact.contact_point_id),
                    }

                claim = await self.transactions.apply(claim, persist)
                external = False
            if claim.stage == "verify":
                await self.contact_budget(claim)
                await self.guard(claim)
                await self.policy(claim, allocation.candidate.country)
                claim = await self.queue.checkpoint_preparation(
                    self.tenant, claim, stage="verify", external_started=True
                )
                external = True
                point_id = ContactPointId(claim.result_refs["contact_point_id"])
                result = await self.verifier.verify(self.tenant, point_id)
                await self.prospecting.record_verification(
                    self.tenant,
                    VerificationRecordRequest(
                        contact_point_id=point_id,
                        result=VerificationStatus(result.outcome.value),
                        provider=result.provider,
                        checked_at=result.checked_at,
                        cost_note=result.cost_note.value,
                    ),
                )
                if result.privacy_claimed:
                    point = await self.prospecting.get_contact_point(
                        self.tenant, point_id
                    )
                    await self.prospecting.handle_erasure_request(
                        self.tenant, point.value
                    )
                    raise PreparationBlocked("privacy_requested")
                if result.outcome.value != "verified":
                    raise PreparationBlocked("verification_failed")
                await self.guard(claim)
                claim = await self.queue.checkpoint_preparation(
                    self.tenant, claim, stage="enroll"
                )
                external = False
            if claim.stage == "enroll":
                await self.guard(claim)
                await self.policy(claim, allocation.candidate.country)
                assert claim.ownership_locked_at is not None
                request = EnrollmentCreateRequest(
                    account_id=ProspectAccountId(claim.result_refs["account_id"]),
                    contact_point_id=ContactPointId(
                        claim.result_refs["contact_point_id"]
                    ),
                    source_hypothesis_id=NeedHypothesisId(
                        claim.result_refs["hypothesis_id"]
                    ),
                    campaign_version=claim.authorization.scope.campaign_version,
                    ownership=EnrollmentOwnershipBinding(
                        claim.authorization.owner_employee_id, claim.ownership_locked_at
                    ),
                    idempotency_key=IdempotencyKey(
                        f"customer-preparation:{claim.allocation_id}:{claim.request_key_hash}"
                    ),
                )
                enrollment = await self.outreach.enroll(
                    self.tenant,
                    CampaignId(claim.authorization.scope.campaign_id),
                    request,
                    actor=preparation_actor(claim),
                )
                claim = await self.queue.checkpoint_preparation(
                    self.tenant,
                    claim,
                    stage="completed",
                    result_refs={"enrollment_id": str(enrollment.enrollment_id)},
                )
        except PreparationBlocked as error:
            await self.queue.block_preparation(self.tenant, claim, reason=error.reason)
        except PermissionDenied:
            await self.queue.block_preparation(
                self.tenant, claim, reason="authorization_changed"
            )
        except ValidationError:
            await self.queue.block_preparation(
                self.tenant,
                claim,
                reason="evidence_invalid"
                if claim.stage == "evidence"
                else "enrollment_denied",
            )
        except Exception:  # noqa: BLE001 -- 未知外部结果只能人工核对，其他故障也留固定状态
            reason = (
                "external_reconciliation_required" if external else "preparation_failed"
            )
            # Gateway可包装外部调用后的撤权；重读当前事实只细化阻断原因，绝不重派。
            try:
                await self.guard(claim)
            except PreparationBlocked as error:
                reason = error.reason
            except PermissionDenied:
                reason = "authorization_changed"
            except Exception:  # noqa: BLE001, S110 -- 事实读取故障不能解除未知结果的持久围栏
                pass
            await self.queue.block_preparation(self.tenant, claim, reason=reason)
