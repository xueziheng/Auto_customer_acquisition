"""账户发现 workflow 的 PII 隔离、验证映射与幂等入组验收。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

from agent_runtime.account_discovery.agent import AccountDiscoveryAgent
from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from apps.scheduler_worker.account_discovery import DemandAccountDiscoveryTaskReader
from connectors.contact_enrichment.client import (
    ContactCandidate,
    ContactEmailKind,
    ContactEnrichmentResult,
    ContactSource,
    EnrichmentCostNote,
)
from connectors.email_verification.client import (
    EmailVerificationOutcome,
    EmailVerificationResult,
    VerificationCostNote,
)
from domains.outreach.schemas import CampaignState
from domains.prospecting.schemas import (
    DiscoveredContactResult,
    ProspectAccountDetailView,
    ProspectAccountView,
    VerificationStatus,
)
from shared.schemas.identifiers import (
    ContactPointId,
    NeedHypothesisId,
    ProspectAccountId,
    ProspectContactId,
    RunId,
    TenantId,
    UserId,
    new_id,
)
from workflows.account_discovery.flow import (
    build_account_discovery_definition,
    build_account_discovery_handlers,
    register_account_discovery,
)
from workflows.account_discovery.ports import (
    AccountDiscoveryOrganizationFact,
    AccountDiscoveryTaskInput,
)
from workflows.account_discovery.steps import (
    AwaitCampaignActivationStep,
    BindCampaignStep,
    EnrollCampaignStep,
    FindCompanyDetailsStep,
    FindContactsStep,
    ResolveAccountStep,
    VerifyContactsStep,
)
from workflows.engine.runner import StepStatus, WorkflowRun


class _UnsafeFreeTextDemandView:
    async def get_hypothesis_for_discovery(self, tenant_id, hypothesis_id):
        del tenant_id
        signal_id = new_id("sig")
        return SimpleNamespace(
            hypothesis_id=str(hypothesis_id),
            account_id=new_id("acc"),
            organization_name="Google",
            country="US",
            website_domain="google.com",
            category="五金",
            source_signal_refs=(signal_id,),
            reasoning="alice and ALICE SMITH may buy hinges",
            evidence=(
                SimpleNamespace(
                    signal_id=signal_id,
                    summary="Alice Smith reported expansion",
                    source_url="https://example.com/people/Alice-SMITH",
                ),
            ),
        )


async def test_task_reader_omits_arbitrary_free_text_and_url_paths() -> None:
    reader = DemandAccountDiscoveryTaskReader(
        _UnsafeFreeTextDemandView(),  # type: ignore[arg-type]
        allowed_countries=("US",),
    )
    hypothesis_id = NeedHypothesisId(new_id("hyp"))

    loaded = await reader.load(
        TenantId(new_id("tn")), hypothesis_id, UserId("employee:manager")
    )

    assert loaded.hypothesis_id == hypothesis_id
    assert loaded.organization.entity_name == "Google"
    assert loaded.organization.country == "US"
    assert loaded.organization.website_domain == "google.com"
    assert loaded.category == "五金"
    assert loaded.source_signal_refs
    assert not hasattr(loaded, "hypothesis")
    serialized = repr(loaded)
    assert "Alice" not in serialized
    assert "alice" not in serialized
    assert "/people/" not in serialized

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)


def _run(**context: object) -> WorkflowRun:
    hypothesis_id = new_id("hyp")
    base: dict[str, object] = {
        "hypothesis_id": hypothesis_id,
        "campaign_id": new_id("cmp"),
        "acting_user_id": "employee:manager",
        "role_hints": ["procurement"],
        "assessment_ref": "lia:synthetic-v1",
        "account_id": new_id("acc"),
        "need_category": "industrial hinges",
        "account_candidate": {"country": "US"},
    }
    base.update(context)
    return WorkflowRun(
        RunId(new_id("run")),
        TenantId(new_id("tn")),
        "account_discovery",
        1,
        hypothesis_id,
        "find_contacts",
        StepStatus.RUNNING,
        NOW,
        context=base,
    )


class _ProspectingForContacts:
    def __init__(self, run: WorkflowRun) -> None:
        self.requests: list[object] = []
        self._account = ProspectAccountView(
            ProspectAccountId(run.context["account_id"]),
            run.tenant_id,
            "Acme",
            "US",
            NOW,
            website_domain="example.com",
        )

    async def get_account_detail(self, tenant_id, account_id):
        assert tenant_id == self._account.tenant_id
        assert account_id == self._account.account_id
        return ProspectAccountDetailView(self._account)

    async def record_discovered_contact(self, tenant_id, request):
        assert tenant_id == self._account.tenant_id
        self.requests.append(request)
        return DiscoveredContactResult(
            request.account_id,
            ProspectContactId(new_id("con")),
            ContactPointId(new_id("cp")),
            True,
        )


class _Enricher:
    async def find_contacts(self, tenant_id, hypothesis_id, account_id, role_hints):
        del tenant_id, hypothesis_id, account_id, role_hints
        return ContactEnrichmentResult(
            (
                ContactCandidate(
                    "alice@example.com",
                    "Alice Buyer",
                    "Procurement Manager",
                    ContactEmailKind.PERSONAL,
                    (
                        ContactSource(
                            "https://example.com/team",
                            date(2026, 8, 1),
                            date(2026, 8, 25),
                            True,
                        ),
                    ),
                ),
            ),
            "hunter",
            EnrichmentCostNote.COUNTED,
        )


async def test_contact_pii_is_consumed_locally_with_complete_legal_basis() -> None:
    run = _run()
    prospecting = _ProspectingForContacts(run)

    action, next_step, patch = await FindContactsStep(
        prospecting, _Enricher(), now=lambda: NOW
    ).execute(run)

    assert (action, next_step) == ("advance", "verify_contacts")
    request = prospecting.requests[0]
    assert request.value == "alice@example.com"
    assert request.legal_basis.basis.value == "legitimate_interest"
    assert request.legal_basis.assessment_ref == "lia:synthetic-v1"
    assert request.legal_basis.source_url == "https://example.com/team"
    assert "alice@example.com" not in repr(patch)
    assert "Alice Buyer" not in repr(patch)
    assert set(patch) == {
        "contact_point_ids",
        "candidate_count",
        "enrichment_cost_note",
    }


class _VerificationProspecting:
    def __init__(self) -> None:
        self.recorded: list[object] = []
        self.erased: list[str] = []

    async def record_verification(self, tenant_id, request):
        del tenant_id
        self.recorded.append(request)

    async def get_contact_point(self, tenant_id, contact_point_id):
        del tenant_id, contact_point_id
        return SimpleNamespace(value="privacy@example.com")

    async def handle_erasure_request(self, tenant_id, value):
        del tenant_id
        self.erased.append(value)


class _Verifier:
    def __init__(self, outcomes: dict[str, EmailVerificationOutcome]) -> None:
        self._outcomes = outcomes

    async def verify(self, tenant_id, contact_point_id):
        del tenant_id
        outcome = self._outcomes[str(contact_point_id)]
        return EmailVerificationResult(
            outcome,
            "hunter",
            NOW,
            VerificationCostNote.PRIVACY_REFUSED
            if outcome is EmailVerificationOutcome.UNVERIFIED
            else VerificationCostNote.COUNTED,
            privacy_claimed=outcome is EmailVerificationOutcome.UNVERIFIED,
        )


async def test_four_verification_outcomes_are_deterministic_and_privacy_erases() -> None:
    ids = {outcome: new_id("cp") for outcome in EmailVerificationOutcome}
    run = _run(contact_point_ids=list(ids.values()))
    prospecting = _VerificationProspecting()

    action, next_step, patch = await VerifyContactsStep(
        prospecting,
        _Verifier({value: outcome for outcome, value in ids.items()}),
    ).execute(run)

    assert (action, next_step) == ("advance", "assign_owner")
    assert patch["verified_contact_point_ids"] == [
        ids[EmailVerificationOutcome.VERIFIED]
    ]
    assert patch["verification_counts"] == {
        status.value: 1 for status in VerificationStatus
    }
    assert [item.result.value for item in prospecting.recorded] == [
        outcome.value for outcome in EmailVerificationOutcome
    ]
    assert prospecting.erased == ["privacy@example.com"]
    assert "privacy@example.com" not in repr(patch)


class _ActorResolver:
    async def resolve(self, tenant_id, acting_user):
        del tenant_id, acting_user
        return SimpleNamespace(outreach=object())


class _Outreach:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def enroll(self, tenant_id, campaign_id, request, *, actor):
        del tenant_id, campaign_id, actor
        self.calls.append(request)
        return SimpleNamespace(enrollment_id=new_id("enr"))

    async def get_campaign(self, tenant_id, campaign_id, *, actor):
        del tenant_id, campaign_id, actor
        return SimpleNamespace(state=CampaignState.PENDING_APPROVAL, version=3)


async def test_definition_discovers_and_verifies_before_durable_campaign_wait() -> None:
    definition = build_account_discovery_definition()
    names = tuple(step.step_name for step in definition.steps)

    assert definition.version == 2
    assert names.index("find_contacts") < names.index("verify_contacts")
    assert names.index("verify_contacts") < names.index("await_campaign_activation")
    assert names.index("await_campaign_activation") < names.index("enroll_campaign")
    wait = next(
        step for step in definition.steps if step.step_name == "await_campaign_activation"
    )
    assert wait.wait_event_type == "CampaignStateChanged"
    assert wait.run_on_entry is True


def test_registration_preserves_exact_v1_and_registers_v2_as_latest() -> None:
    class _Engine:
        def __init__(self) -> None:
            self.definitions: list[object] = []

        def register(self, definition) -> None:
            self.definitions.append(definition)

    engine = _Engine()
    register_account_discovery(engine)  # type: ignore[arg-type]

    assert [definition.version for definition in engine.definitions] == [1, 2]
    legacy, latest = engine.definitions
    assert tuple(step.step_name for step in legacy.steps) == (
        "find_company_details",
        "resolve_account",
        "find_contacts",
        "verify_contacts",
        "assign_owner",
        "enroll_campaign",
    )
    assert next(
        step.handler_ref for step in legacy.steps if step.step_name == "enroll_campaign"
    ) == "account_discovery.enroll_campaign"
    assert next(
        step.handler_ref for step in latest.steps if step.step_name == "enroll_campaign"
    ) == "account_discovery.enroll_campaign_v2"


async def test_persisted_v1_enroll_context_completes_without_campaign_version() -> None:
    contact_id = new_id("cp")
    run = _run(verified_contact_point_ids=[contact_id])
    outreach = _Outreach()
    handlers = build_account_discovery_handlers(
        task_reader=object(),  # type: ignore[arg-type]
        capability=object(),  # type: ignore[arg-type]
        prospecting=object(),  # type: ignore[arg-type]
        enricher=object(),  # type: ignore[arg-type]
        verifier=object(),  # type: ignore[arg-type]
        employees=object(),  # type: ignore[arg-type]
        outreach=outreach,  # type: ignore[arg-type]
        actor_resolver=_ActorResolver(),
        now=lambda: NOW,
    )

    action, next_step, patch = await handlers[
        "account_discovery.enroll_campaign"
    ].execute(run)

    assert (action, next_step) == ("complete", None)
    assert patch["enrollment_ids"]
    assert outreach.calls[0].campaign_version is None


async def test_verified_contacts_wait_for_exact_campaign_version_before_enrollment() -> None:
    run = _run(verified_contact_point_ids=[new_id("cp")])
    outreach = _Outreach()
    bind_action, bind_next, bind_patch = await BindCampaignStep(
        outreach, _ActorResolver()
    ).execute(run)
    assert (bind_action, bind_next, bind_patch) == (
        "advance",
        "find_company_details",
        {"campaign_version": 3},
    )

    context = dict(run.context)
    context.update(bind_patch)
    waiting_run = WorkflowRun(
        run.run_id,
        run.tenant_id,
        run.workflow_type,
        2,
        run.subject_ref,
        "await_campaign_activation",
        StepStatus.RUNNING,
        run.created_at,
        context=context,
    )
    action, next_step, patch = await AwaitCampaignActivationStep(
        outreach, _ActorResolver()
    ).execute(waiting_run)

    assert (action, next_step) == ("wait", None)
    assert patch == {"campaign_activation_state": "pending_approval"}
    assert outreach.calls == []


async def test_activation_event_is_exact_and_enrollment_carries_bound_version() -> None:
    contact_id = new_id("cp")
    run = _run(
        verified_contact_point_ids=[contact_id],
        campaign_version=3,
        event={
            "event_type": "CampaignStateChanged",
            "payload": {
                "campaign_id": None,
                "campaign_version": 3,
                "state": "active",
            },
        },
    )
    run.context["event"]["payload"]["campaign_id"] = run.context["campaign_id"]
    outreach = _Outreach()
    outreach.get_campaign = lambda *args, **kwargs: _campaign_active()  # type: ignore[method-assign]

    action, next_step, _patch = await AwaitCampaignActivationStep(
        outreach, _ActorResolver()
    ).execute(run)
    assert (action, next_step) == ("advance", "enroll_campaign")

    await EnrollCampaignStep(outreach, _ActorResolver()).execute(run)
    assert outreach.calls[0].campaign_version == 3


async def test_stale_active_event_cannot_advance_exact_bound_campaign() -> None:
    run = _campaign_wait_run(state="active", event_version=2, bound_version=3)

    result = await AwaitCampaignActivationStep(
        _CampaignStateOutreach(CampaignState.ACTIVE, 3), _ActorResolver()
    ).execute(run)

    assert result[0:2] == ("fail", "campaign_event_version_mismatch")


async def _campaign_active():
    return SimpleNamespace(state=CampaignState.ACTIVE, version=3)


class _CampaignStateOutreach(_Outreach):
    def __init__(self, state: CampaignState, version: int) -> None:
        super().__init__()
        self.state = state
        self.version = version

    async def get_campaign(self, tenant_id, campaign_id, *, actor):
        del tenant_id, campaign_id, actor
        return SimpleNamespace(state=self.state, version=self.version)


def _campaign_wait_run(
    *,
    state: str,
    event_version: int = 3,
    bound_version: int = 3,
) -> WorkflowRun:
    run = _run(
        campaign_version=bound_version,
        event={
            "event_type": "CampaignStateChanged",
            "payload": {
                "campaign_id": "placeholder",
                "campaign_version": event_version,
                "state": state,
            },
        },
    )
    run.context["event"]["payload"]["campaign_id"] = run.context["campaign_id"]
    return run


async def test_rejected_cancelled_and_revised_campaigns_fail_closed() -> None:
    rejected = _campaign_wait_run(state="rejected")
    rejected_result = await AwaitCampaignActivationStep(
        _CampaignStateOutreach(CampaignState.PENDING_APPROVAL, 3), _ActorResolver()
    ).execute(rejected)
    assert rejected_result[0:2] == ("fail", "campaign_activation_rejected")

    cancelled = _campaign_wait_run(state="cancelled")
    cancelled_result = await AwaitCampaignActivationStep(
        _CampaignStateOutreach(CampaignState.CANCELLED, 3), _ActorResolver()
    ).execute(cancelled)
    assert cancelled_result[0:2] == ("fail", "campaign_activation_rejected")

    revised = _campaign_wait_run(
        state="pending_approval", event_version=4, bound_version=3
    )
    revised_result = await AwaitCampaignActivationStep(
        _CampaignStateOutreach(CampaignState.PENDING_APPROVAL, 4), _ActorResolver()
    ).execute(revised)
    assert revised_result[0:2] == ("fail", "campaign_version_changed")


async def test_activation_event_replay_keeps_contact_bound_enrollment_key() -> None:
    run = _campaign_wait_run(state="active")
    outreach = _CampaignStateOutreach(CampaignState.ACTIVE, 3)
    wait = AwaitCampaignActivationStep(outreach, _ActorResolver())
    first = await wait.execute(run)
    second = await wait.execute(run)
    assert first[0:2] == second[0:2] == ("advance", "enroll_campaign")

    run.context["verified_contact_point_ids"] = [new_id("cp")]
    enrollment = EnrollCampaignStep(outreach, _ActorResolver())
    await enrollment.execute(run)
    await enrollment.execute(run)
    assert len({str(call.idempotency_key) for call in outreach.calls}) == 1
    assert {call.campaign_version for call in outreach.calls} == {3}


async def test_enrollment_replay_uses_stable_contact_bound_idempotency_keys() -> None:
    contact_ids = [new_id("cp"), new_id("cp")]
    run = _run(verified_contact_point_ids=contact_ids, campaign_version=1)
    outreach = _Outreach()
    step = EnrollCampaignStep(outreach, _ActorResolver())

    await step.execute(run)
    await step.execute(run)

    keys = [str(call.idempotency_key) for call in outreach.calls]
    expected_once = [
        f"account-discovery:{run.run_id}:{contact_id}"
        for contact_id in contact_ids
    ]
    assert keys == expected_once + expected_once


class _UnsafeAccountModel:
    async def discover_account(self, *, system_prompt, hypothesis):
        del system_prompt
        return (
            '{"entity_name":"Alice Buyer alice@example.com",'
            '"country":"US","website_domain":"example.com",'
            '"entity_type":"manufacturer","industry":"hardware",'
            '"size_hint":null,"source_signal_refs":["'
            + hypothesis["source_signal_refs"][0]
            + '"]}'
        )


class _AccountTaskReader:
    async def load(self, tenant_id, hypothesis_id, acting_user):
        del tenant_id, acting_user
        signal_id = new_id("sig")
        return AccountDiscoveryTaskInput(
            objective="寻找企业公开官网",
            hypothesis_id=hypothesis_id,
            organization=AccountDiscoveryOrganizationFact(
                account_id=ProspectAccountId(new_id("acc")),
                entity_name="Acme Manufacturing",
                country="US",
                website_domain="example.com",
            ),
            category="industrial hinges",
            source_signal_refs=(signal_id,),
            allowed_countries=("US",),
        )


async def test_unsafe_model_output_never_enters_persisted_context_patch() -> None:
    run = _run()
    agent = AccountDiscoveryAgent(
        "model-v1", _UnsafeAccountModel(), object(), CredentialMarkerGuard()
    )
    step = FindCompanyDetailsStep(_AccountTaskReader(), agent, _ActorResolver())

    action, next_step, patch = await step.execute(run)

    assert (action, next_step) == ("complete", None)
    assert patch == {"company_resolution": "no_evidence"}
    assert "Alice" not in repr(patch)
    assert "alice@example.com" not in repr(patch)


class _MaliciousRedirectModel:
    async def discover_account(self, *, system_prompt, hypothesis):
        del system_prompt, hypothesis
        return (
            '{"evidence_sufficient":true,"website_domain":"google.com",'
            '"source_signal_refs":[]}'
        )


class _BoundAppleTaskReader:
    def __init__(self, account_id: ProspectAccountId, signal_id: str) -> None:
        self.account_id = account_id
        self.signal_id = signal_id

    async def load(self, tenant_id, hypothesis_id, acting_user):
        del tenant_id, acting_user
        return AccountDiscoveryTaskInput(
            objective="验证现有企业证据",
            hypothesis_id=hypothesis_id,
            organization=AccountDiscoveryOrganizationFact(
                account_id=self.account_id,
                entity_name="Apple",
                country="US",
                website_domain="apple.com",
            ),
            category="五金",
            source_signal_refs=(self.signal_id,),
            allowed_countries=("US",),
        )


async def test_malicious_domain_cannot_redirect_existing_account_workflow() -> None:
    run = _run()
    account_id = ProspectAccountId(new_id("acc"))
    signal_id = new_id("sig")
    agent = AccountDiscoveryAgent(
        "model-v1", _MaliciousRedirectModel(), object(), CredentialMarkerGuard()
    )

    action, next_step, patch = await FindCompanyDetailsStep(
        _BoundAppleTaskReader(account_id, signal_id), agent, _ActorResolver()
    ).execute(run)

    assert (action, next_step) == ("complete", None)
    assert patch == {"company_resolution": "no_evidence"}
    assert "google.com" not in repr(patch)


class _SufficientEvidenceModel:
    async def discover_account(self, *, system_prompt, hypothesis):
        del system_prompt
        return (
            '{"evidence_sufficient":true,"source_signal_refs":["'
            + hypothesis["source_signal_refs"][0]
            + '"]}'
        )


class _ExistingAppleProspecting:
    def __init__(self, tenant_id: TenantId, account_id: ProspectAccountId) -> None:
        self.account = ProspectAccountView(
            account_id,
            tenant_id,
            "Apple",
            "US",
            NOW,
            website_domain="apple.com",
        )
        self.resolve_calls = 0

    async def get_account(self, tenant_id, account_id):
        assert (tenant_id, account_id) == (
            self.account.tenant_id,
            self.account.account_id,
        )
        return self.account

    async def resolve_account(self, tenant_id, request):
        del tenant_id, request
        self.resolve_calls += 1
        raise AssertionError("既有 account 绑定不得重新消歧")


async def test_existing_account_id_and_domain_survive_model_and_resolve_step() -> None:
    run = _run()
    account_id = ProspectAccountId(new_id("acc"))
    signal_id = new_id("sig")
    agent = AccountDiscoveryAgent(
        "model-v1", _SufficientEvidenceModel(), object(), CredentialMarkerGuard()
    )
    action, next_step, patch = await FindCompanyDetailsStep(
        _BoundAppleTaskReader(account_id, signal_id), agent, _ActorResolver()
    ).execute(run)
    assert (action, next_step) == ("advance", "resolve_account")
    assert patch["account_candidate"]["account_id"] == account_id
    assert patch["account_candidate"]["website_domain"] == "apple.com"

    bound_context = dict(run.context)
    bound_context.update(patch)
    bound_run = WorkflowRun(
        run.run_id,
        run.tenant_id,
        run.workflow_type,
        run.workflow_version,
        run.subject_ref,
        "resolve_account",
        run.status,
        run.created_at,
        context=bound_context,
    )
    prospecting = _ExistingAppleProspecting(run.tenant_id, account_id)

    action, next_step, resolve_patch = await ResolveAccountStep(prospecting).execute(
        bound_run
    )

    assert (action, next_step) == ("advance", "find_contacts")
    assert resolve_patch == {"account_id": account_id}
    assert prospecting.resolve_calls == 0
