"""账户发现步骤：企业公开事实 → Provider PII → 验证 → 归属 → 入组。

联系人候选只在 ``FindContactsStep.execute`` 的受信调用栈内存在，立即经
ProspectingService 原子持久化；workflow context 只记录 typed ID、计数与固定枚举。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from agent_runtime.base import AgentTask
from connectors.contact_enrichment.client import ContactEmailKind
from connectors.email_verification.client import EmailVerificationOutcome
from domains.employees.service import EmployeeService
from domains.outreach.schemas import EnrollmentCreateRequest
from domains.outreach.service import OutreachService
from domains.prospecting.schemas import (
    AccountResolveRequest,
    ContactPointKind,
    ContactType,
    DiscoveredContactRequest,
    LegalBasisInput,
    LegalBasisType,
    SubjectType,
    VerificationRecordRequest,
    VerificationStatus,
)
from domains.prospecting.service import ProspectingService
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import (
    CampaignId,
    ContactPointId,
    IdempotencyKey,
    NeedHypothesisId,
    ProspectAccountId,
    UserId,
)
from workflows.account_discovery.ports import (
    AccountDiscoveryActorResolver,
    AccountDiscoveryCapability,
    AccountDiscoveryTaskReader,
    ContactEnricher,
    ContactVerifier,
)
from workflows.engine.runner import WorkflowRun


def _exact_text(value: object, message: str, *, max_len: int = 200) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > max_len
    ):
        raise ValidationError(message)
    return value


def _base_context(
    run: WorkflowRun,
) -> tuple[NeedHypothesisId, CampaignId, UserId, tuple[str, ...], str]:
    if run.subject_ref != run.context.get("hypothesis_id"):
        raise ValidationError("账户发现 workflow subject 无效")
    hypothesis_id = NeedHypothesisId(
        _exact_text(run.context.get("hypothesis_id"), "账户发现缺少 hypothesis", max_len=40)
    )
    campaign_id = CampaignId(
        _exact_text(run.context.get("campaign_id"), "账户发现缺少 campaign", max_len=40)
    )
    acting_user = UserId(
        _exact_text(run.context.get("acting_user_id"), "账户发现缺少发起人", max_len=64)
    )
    raw_hints = run.context.get("role_hints")
    if not isinstance(raw_hints, list) or len(raw_hints) > 10:
        raise ValidationError("账户发现 role hints 无效")
    role_hints = tuple(
        dict.fromkeys(_exact_text(value, "账户发现 role hints 无效") for value in raw_hints)
    )
    assessment_ref = _exact_text(
        run.context.get("assessment_ref"),
        "账户发现缺少正当利益评估",
        max_len=200,
    )
    return hypothesis_id, campaign_id, acting_user, role_hints, assessment_ref


def _account_id(run: WorkflowRun) -> ProspectAccountId:
    return ProspectAccountId(
        _exact_text(run.context.get("account_id"), "账户发现缺少 account", max_len=40)
    )


class FindCompanyDetailsStep:
    def __init__(
        self,
        task_reader: AccountDiscoveryTaskReader,
        capability: AccountDiscoveryCapability,
    ) -> None:
        self._task_reader = task_reader
        self._capability = capability

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        hypothesis_id, _campaign_id, acting_user, _hints, _assessment = _base_context(run)
        source = await self._task_reader.load(run.tenant_id, hypothesis_id, acting_user)
        result = await self._capability.run(
            AgentTask(
                tenant_id=run.tenant_id,
                run_id=run.run_id,
                acting_user=acting_user,
                objective=source.objective,
                inputs={
                    "hypothesis": source.hypothesis,
                    "allowed_countries": source.allowed_countries,
                },
            ),
            context=None,
        )
        if result.tenant_id != run.tenant_id or result.run_id != run.run_id:
            raise TenantIsolationViolation("账户发现 ChangeSet 租户或 run 不一致")
        if not result.changes:
            return ("complete", None, {"company_resolution": "no_evidence"})
        if len(result.changes) != 1:
            raise ValidationError("账户发现 ChangeSet 数量无效")
        change = result.changes[0]
        if (
            change.get("domain") != "prospecting"
            or change.get("operation") != "resolve_account"
            or change.get("risk_level") != "low"
            or not isinstance(change.get("payload"), dict)
        ):
            raise ValidationError("账户发现 ChangeSet 越界")
        payload = change["payload"]
        return (
            "advance",
            "resolve_account",
            {
                "account_candidate": payload,
                "need_category": _exact_text(
                    source.hypothesis.get("category"),
                    "账户发现需求类别无效",
                ),
            },
        )


class ResolveAccountStep:
    def __init__(self, prospecting: ProspectingService) -> None:
        self._prospecting = prospecting

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        _base_context(run)
        payload = run.context.get("account_candidate")
        if not isinstance(payload, dict):
            raise ValidationError("账户发现企业候选缺失")
        refs = payload.get("source_signal_refs")
        if not isinstance(refs, (list, tuple)):
            raise ValidationError("账户发现企业来源无效")
        account_id = await self._prospecting.resolve_account(
            run.tenant_id,
            AccountResolveRequest(
                entity_name=_exact_text(payload.get("entity_name"), "账户发现企业名无效"),
                country=_exact_text(payload.get("country"), "账户发现国家无效", max_len=64),
                website_domain=_exact_text(
                    payload.get("website_domain"), "账户发现官网无效", max_len=253
                ),
                entity_type=(
                    None
                    if payload.get("entity_type") is None
                    else _exact_text(payload.get("entity_type"), "账户发现企业类型无效")
                ),
                industry=(
                    None
                    if payload.get("industry") is None
                    else _exact_text(payload.get("industry"), "账户发现行业无效")
                ),
                size_hint=(
                    None
                    if payload.get("size_hint") is None
                    else _exact_text(payload.get("size_hint"), "账户发现规模无效")
                ),
                source_signal_refs=tuple(
                    _exact_text(value, "账户发现企业来源无效", max_len=40)
                    for value in refs
                ),
            ),
        )
        return ("advance", "find_contacts", {"account_id": str(account_id)})


class FindContactsStep:
    def __init__(
        self,
        prospecting: ProspectingService,
        enricher: ContactEnricher,
        *,
        now: Callable[[], datetime],
    ) -> None:
        self._prospecting = prospecting
        self._enricher = enricher
        self._now = now

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        hypothesis_id, _campaign_id, _acting, role_hints, assessment_ref = _base_context(run)
        account_id = _account_id(run)
        detail = await self._prospecting.get_account_detail(run.tenant_id, account_id)
        existing_ids = tuple(
            item.contact_point.contact_point_id
            for contact in detail.contacts
            for item in contact.contact_points
            if item.legal_basis_source == "hunter"
        )
        if existing_ids:
            return (
                "advance",
                "verify_contacts",
                {
                    "contact_point_ids": [str(value) for value in existing_ids],
                    "candidate_count": len(existing_ids),
                    "enrichment_cost_note": "existing_provider_records",
                },
            )
        domain = detail.account.website_domain
        if domain is None:
            raise ValidationError("账户发现企业缺少官网域名")
        result = await self._enricher.find_contacts(
            run.tenant_id, hypothesis_id, account_id, role_hints
        )
        contact_point_ids: list[str] = []
        collected_at = self._now()
        for candidate in result.candidates:
            source = candidate.sources[0]
            recorded = await self._prospecting.record_discovered_contact(
                run.tenant_id,
                DiscoveredContactRequest(
                    account_id=account_id,
                    kind=ContactPointKind.EMAIL,
                    value=candidate.email,
                    full_name=candidate.full_name,
                    role_title=candidate.role_title,
                    legal_basis=LegalBasisInput(
                        basis=LegalBasisType.LEGITIMATE_INTEREST,
                        subject_type=SubjectType.LEGAL_ENTITY,
                        contact_type=(
                            ContactType.ROLE_BASED
                            if candidate.email_kind is ContactEmailKind.GENERIC
                            else ContactType.PERSONAL_BUSINESS
                        ),
                        source=result.provider,
                        source_url=source.uri,
                        collected_at=collected_at,
                        assessment_ref=assessment_ref,
                    ),
                    enrichment_cost_note=result.cost_note.value,
                ),
            )
            contact_point_ids.append(str(recorded.contact_point_id))
        return (
            "advance",
            "verify_contacts",
            {
                "contact_point_ids": list(dict.fromkeys(contact_point_ids)),
                "candidate_count": len(result.candidates),
                "enrichment_cost_note": result.cost_note.value,
            },
        )


class VerifyContactsStep:
    def __init__(
        self,
        prospecting: ProspectingService,
        verifier: ContactVerifier,
    ) -> None:
        self._prospecting = prospecting
        self._verifier = verifier

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        _base_context(run)
        raw_ids = run.context.get("contact_point_ids")
        if not isinstance(raw_ids, list):
            raise ValidationError("账户发现联系方式集合无效")
        verified: list[str] = []
        counts = {status.value: 0 for status in VerificationStatus}
        mapping = {
            EmailVerificationOutcome.VERIFIED: VerificationStatus.VERIFIED,
            EmailVerificationOutcome.INVALID: VerificationStatus.INVALID,
            EmailVerificationOutcome.RISKY: VerificationStatus.RISKY,
            EmailVerificationOutcome.UNVERIFIED: VerificationStatus.UNVERIFIED,
        }
        for raw_id in dict.fromkeys(raw_ids):
            contact_point_id = ContactPointId(
                _exact_text(raw_id, "账户发现联系方式标识无效", max_len=40)
            )
            result = await self._verifier.verify(run.tenant_id, contact_point_id)
            status = mapping[result.outcome]
            await self._prospecting.record_verification(
                run.tenant_id,
                VerificationRecordRequest(
                    contact_point_id=contact_point_id,
                    result=status,
                    provider=result.provider,
                    checked_at=result.checked_at,
                    cost_note=result.cost_note.value,
                ),
            )
            counts[status.value] += 1
            if result.privacy_claimed:
                point = await self._prospecting.get_contact_point(
                    run.tenant_id, contact_point_id
                )
                await self._prospecting.handle_erasure_request(
                    run.tenant_id, point.value
                )
                continue
            if status is VerificationStatus.VERIFIED:
                verified.append(str(contact_point_id))
        return (
            "advance",
            "assign_owner",
            {
                "verified_contact_point_ids": verified,
                "verification_counts": counts,
            },
        )


class AssignOwnerStep:
    def __init__(
        self,
        employees: EmployeeService,
        actor_resolver: AccountDiscoveryActorResolver,
    ) -> None:
        self._employees = employees
        self._actor_resolver = actor_resolver

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        _hypothesis, _campaign, acting_user, _hints, _assessment = _base_context(run)
        account_id = _account_id(run)
        category = _exact_text(run.context.get("need_category"), "账户发现需求类别无效")
        # country 是企业事实；Prospecting 已在上一步校验并持久化。
        # EmployeeService 自己负责八级规则与原子归属锁。
        country = _exact_text(
            run.context["account_candidate"].get("country")
            if isinstance(run.context.get("account_candidate"), dict)
            else None,
            "账户发现国家无效",
            max_len=64,
        )
        actors = await self._actor_resolver.resolve(run.tenant_id, acting_user)
        owner = await self._employees.resolve_owner(
            run.tenant_id,
            account_id,
            actor=actors.employee,
            country=country,
            need_category=category,
        )
        return ("advance", "enroll_campaign", {"owner_id": str(owner.owner)})


class EnrollCampaignStep:
    def __init__(
        self,
        outreach: OutreachService,
        actor_resolver: AccountDiscoveryActorResolver,
    ) -> None:
        self._outreach = outreach
        self._actor_resolver = actor_resolver

    async def execute(self, run: WorkflowRun) -> tuple[str, str | None, dict[str, Any]]:
        _hypothesis_id, campaign_id, acting_user, _hints, _assessment = _base_context(run)
        account_id = _account_id(run)
        raw_ids = run.context.get("verified_contact_point_ids")
        if not isinstance(raw_ids, list):
            raise ValidationError("账户发现已验证联系方式集合无效")
        actors = await self._actor_resolver.resolve(run.tenant_id, acting_user)
        enrollment_ids: list[str] = []
        for raw_id in dict.fromkeys(raw_ids):
            contact_point_id = ContactPointId(
                _exact_text(raw_id, "账户发现联系方式标识无效", max_len=40)
            )
            enrollment = await self._outreach.enroll(
                run.tenant_id,
                campaign_id,
                EnrollmentCreateRequest(
                    account_id=account_id,
                    contact_point_id=contact_point_id,
                    idempotency_key=IdempotencyKey(
                        f"account-discovery:{run.run_id}:{contact_point_id}"
                    ),
                ),
                actor=actors.outreach,
            )
            enrollment_ids.append(str(enrollment.enrollment_id))
        return (
            "complete",
            None,
            {
                "enrollment_ids": enrollment_ids,
                "verified_count": len(raw_ids),
            },
        )


__all__ = (
    "AssignOwnerStep",
    "EnrollCampaignStep",
    "FindCompanyDetailsStep",
    "FindContactsStep",
    "ResolveAccountStep",
    "VerifyContactsStep",
)
