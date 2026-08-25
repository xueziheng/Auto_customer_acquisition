"""账户发现 workflow 的 PII 隔离、验证映射与幂等入组验收。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

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
from domains.prospecting.schemas import (
    DiscoveredContactResult,
    ProspectAccountDetailView,
    ProspectAccountView,
    VerificationStatus,
)
from shared.schemas.identifiers import (
    ContactPointId,
    ProspectAccountId,
    ProspectContactId,
    RunId,
    TenantId,
    new_id,
)
from workflows.account_discovery.steps import (
    EnrollCampaignStep,
    FindContactsStep,
    VerifyContactsStep,
)
from workflows.engine.runner import StepStatus, WorkflowRun

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


async def test_enrollment_replay_uses_stable_contact_bound_idempotency_keys() -> None:
    contact_ids = [new_id("cp"), new_id("cp")]
    run = _run(verified_contact_point_ids=contact_ids)
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
