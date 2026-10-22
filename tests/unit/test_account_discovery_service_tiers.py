"""联系人处理自动继续，并在任何 Provider 成本前检查同服务期额度。"""

from __future__ import annotations

import inspect
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from connectors.contact_enrichment.client import (
    ContactCandidate,
    ContactEnrichmentResult,
    ContactSource,
    EnrichmentCostNote,
)
from connectors.email_verification.client import (
    EmailVerificationOutcome,
    EmailVerificationResult,
    VerificationCostNote,
)
from domains.prospecting.service import ScreeningEvidence, ServicePlanState
from shared.errors import ValidationError
from shared.schemas.identifiers import RunId, TenantId
from workflows.account_discovery.flow import (
    build_account_discovery_definition,
    build_account_discovery_handlers,
    build_legacy_account_discovery_definition,
)
from workflows.engine.runner import StepStatus, WorkflowRun

NOW = datetime(2026, 10, 10, 8, tzinfo=UTC)
TENANT = TenantId("tenant_contacts")
ACCOUNT = "pa_contacts"


@pytest.mark.parametrize(
    "definition",
    [build_legacy_account_discovery_definition(), build_account_discovery_definition()],
)
def test_contact_steps_have_resumable_screening_event_wait(definition):
    steps = {step.step_name: step for step in definition.steps}
    for name in ("find_contacts", "verify_contacts"):
        assert steps[name].wait_event_type == "ProspectScreeningReady"
        assert steps[name].run_on_entry is True


def _run(**changes: Any) -> WorkflowRun:
    context = {
        "hypothesis_id": "hyp_contacts",
        "campaign_id": "cmp_contacts",
        "acting_user_id": "employee:manager",
        "role_hints": ["procurement"],
        "assessment_ref": "lia_confirmed",
        "account_id": ACCOUNT,
        "max_contacts": 100,
        "service_tier": "advanced",
    }
    context.update(changes)
    return WorkflowRun(
        RunId("run_contacts"),
        TENANT,
        "account_discovery",
        2,
        "hyp_contacts",
        "find_contacts",
        StepStatus.RUNNING,
        NOW,
        context=context,
    )


def _evidence() -> tuple[ScreeningEvidence, ...]:
    return tuple(
        ScreeningEvidence(
            tenant_id=TENANT,
            account_id=ACCOUNT,
            kind=kind,
            source_ref=f"sig_{kind}",
            source_quote="Verified source passage",
            source_url=(
                "https://industrynews.com/news"
                if kind == "independent_confirmation"
                else "https://manufacturer.com/about"
            ),
            occurred_at=NOW - timedelta(days=2),
            recorded_at=NOW,
            reviewed_by="emp_reviewer" if kind == "human_review" else None,
            reviewed_at=NOW if kind == "human_review" else None,
        )
        for kind in (
            "business_match",
            "recent_need",
            "contact_role",
            "independent_confirmation",
            "human_review",
        )
    )


class _Store:
    def __init__(self, tier: str = "basic") -> None:
        self.state: ServicePlanState | None = ServicePlanState(
            tenant_id=TENANT,
            tier=tier,
            version=1,
            period_started_at=NOW - timedelta(days=1),
            period_ends_at=NOW + timedelta(days=29),
            candidates_reserved=0,
            searches_reserved=0,
            contacts_reserved=0,
            schedule_enabled=False,
        )
        self.evidence = _evidence()
        self.receipts: dict[str, Any] = {}
        self.reserve_calls: list[dict[str, Any]] = []
        self.reject_reservation = False
        self.evidence_reads = 0
        self.evidence_error = None

    async def get(self, tenant_id):
        assert tenant_id == TENANT
        return self.state

    async def get_evidence(self, tenant_id, account_id):
        assert (tenant_id, account_id) == (TENANT, ACCOUNT)
        self.evidence_reads += 1
        if self.evidence_error is not None:
            raise self.evidence_error
        return self.evidence

    async def reservation(self, tenant_id, *, key, now):
        assert tenant_id == TENANT
        receipt = self.receipts.get(key)
        if self.state is None or not (
            self.state.period_started_at <= now < self.state.period_ends_at
        ):
            return None
        return receipt

    async def reserve(self, tenant_id, **request):
        assert tenant_id == TENANT
        self.reserve_calls.append(request)
        if self.reject_reservation:
            raise ValidationError("service_plan_contact_quota_exhausted")
        assert self.state is not None
        self.receipts[request["key"]] = SimpleNamespace(
            key=request["key"],
            period_started_at=self.state.period_started_at,
            candidates=request["candidates"],
            searches=request["searches"],
            contacts=request["contacts"],
        )


class _Prospecting:
    def __init__(self, existing: int = 0) -> None:
        self.ids = [f"cp_{index}" for index in range(existing)]
        self.recorded: list[Any] = []
        self.verified: list[Any] = []

    async def get_account_detail(self, tenant_id, account_id):
        assert (tenant_id, account_id) == (TENANT, ACCOUNT)
        return SimpleNamespace(
            account=SimpleNamespace(website_domain="manufacturer.com"),
            contacts=tuple(
                SimpleNamespace(
                    contact_points=(
                        SimpleNamespace(
                            legal_basis_source="hunter",
                            contact_point=SimpleNamespace(contact_point_id=value),
                        ),
                    )
                )
                for value in self.ids
            ),
        )

    async def record_discovered_contact(self, tenant_id, request):
        assert tenant_id == TENANT
        self.recorded.append(request)
        value = f"cp_{len(self.ids)}"
        self.ids.append(value)
        return SimpleNamespace(contact_point_id=value)

    async def record_verification(self, tenant_id, request):
        assert tenant_id == TENANT
        self.verified.append(request)


class _Enricher:
    def __init__(self) -> None:
        self.calls = 0

    async def find_contacts(self, tenant_id, hypothesis_id, account_id, role_hints):
        assert (tenant_id, hypothesis_id, account_id, role_hints) == (
            TENANT,
            "hyp_contacts",
            ACCOUNT,
            ("procurement",),
        )
        self.calls += 1
        return ContactEnrichmentResult(
            tuple(
                ContactCandidate(
                    email=f"buyer{index}@manufacturer.com",
                    sources=(
                        ContactSource(
                            "https://manufacturer.com/team",
                            date(2026, 10, 1),
                            date(2026, 10, 10),
                            True,
                        ),
                    ),
                )
                for index in range(3)
            ),
            "hunter",
            EnrichmentCostNote.COUNTED,
        )


class _Verifier:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def verify(self, tenant_id, contact_point_id):
        assert tenant_id == TENANT
        self.calls.append(contact_point_id)
        return EmailVerificationResult(
            EmailVerificationOutcome.VERIFIED,
            "hunter",
            NOW,
            VerificationCostNote.COUNTED,
        )


def _handlers(store, prospecting, enricher, verifier, now=lambda: NOW):
    assert (
        "service_plans"
        in inspect.signature(build_account_discovery_handlers).parameters
    )
    return build_account_discovery_handlers(
        task_reader=object(),
        capability=object(),
        prospecting=prospecting,
        enricher=enricher,
        verifier=verifier,
        employees=object(),
        outreach=object(),
        actor_resolver=object(),
        now=now,
        service_plans=store,
    )


@pytest.mark.parametrize("existing", [0, 3])
@pytest.mark.parametrize("tier", ["basic", "standard", "advanced"])
@pytest.mark.parametrize("evidence_state", ["empty", "unreviewed", "unavailable"])
async def test_contacts_are_found_and_verified_without_manual_evidence(
    existing: int, tier: str, evidence_state: str,
) -> None:
    store, prospecting, enricher, verifier = (
        _Store(tier), _Prospecting(existing), _Enricher(), _Verifier()
    )
    store.evidence = (
        tuple(item for item in _evidence() if item.kind != "human_review")
        if evidence_state == "unreviewed" else ()
    )
    if evidence_state == "unavailable":
        store.evidence_error = RuntimeError("optional evidence store unavailable")
    handlers = _handlers(store, prospecting, enricher, verifier)
    run = _run(contact_screening_status="needs_evidence",
               contact_screening_missing=["human_review"],
               contact_screening_wait_generation=3)
    action, next_step, patch = await handlers[
        "account_discovery.find_contacts"
    ].execute(run)
    assert (action, next_step) == ("advance", "verify_contacts")
    assert patch["contact_screening_status"] == "ready_for_enrichment"
    assert patch["contact_screening_missing"] == []
    assert enricher.calls == (0 if existing else 1)
    assert len(store.reserve_calls) == 1
    assert store.reserve_calls[0]["key"] == __import__("domains.prospecting.service", fromlist=["contact_mission_key"]).contact_mission_key("cmp_contacts", ACCOUNT)
    run.context.update(patch)
    action, next_step, patch = await handlers[
        "account_discovery.verify_contacts"
    ].execute(run)
    assert (action, next_step) == ("advance", "assign_owner")
    assert patch["contact_screening_status"] == "ready_for_enrichment"
    assert patch["verified_contact_point_ids"] == verifier.calls
    assert len(verifier.calls) == (2 if tier == "advanced" else 1)
    assert len(store.reserve_calls) == 1
    assert store.evidence_reads == 0


async def test_exhausted_contact_budget_blocks_provider_call() -> None:
    store, prospecting, enricher, verifier = (
        _Store(),
        _Prospecting(),
        _Enricher(),
        _Verifier(),
    )
    store.reject_reservation = True
    handlers = _handlers(store, prospecting, enricher, verifier)
    with pytest.raises(ValidationError, match="quota_exhausted"):
        await handlers["account_discovery.find_contacts"].execute(_run())
    assert enricher.calls == 0
    assert prospecting.recorded == []


@pytest.mark.parametrize(
    ("tier", "limit"), [("basic", 1), ("standard", 1), ("advanced", 2)]
)
@pytest.mark.parametrize("existing", [0, 3])
async def test_current_tier_limits_provider_and_historical_contacts(
    tier: str, limit: int, existing: int
) -> None:
    store, prospecting, enricher, verifier = (
        _Store(tier),
        _Prospecting(existing),
        _Enricher(),
        _Verifier(),
    )
    handlers = _handlers(store, prospecting, enricher, verifier)
    run = _run()
    action, next_step, patch = await handlers[
        "account_discovery.find_contacts"
    ].execute(run)
    assert (action, next_step) == ("advance", "verify_contacts")
    assert len(patch["contact_point_ids"]) == patch["candidate_count"] == limit
    assert len(prospecting.recorded) == (0 if existing else limit)
    assert enricher.calls == (0 if existing else 1)
    assert store.reserve_calls == [
        {
            "key": __import__("domains.prospecting.service", fromlist=["contact_mission_key"]).contact_mission_key("cmp_contacts", ACCOUNT),
            "candidates": 0,
            "searches": 0,
            "contacts": limit,
            "now": NOW,
        }
    ]
    run.context.update(patch)
    await handlers["account_discovery.verify_contacts"].execute(run)
    assert len(verifier.calls) == limit
    assert len(store.reserve_calls) == 1


async def test_find_replay_reuses_receipt_without_second_reservation() -> None:
    store, prospecting, enricher, verifier = (
        _Store(),
        _Prospecting(),
        _Enricher(),
        _Verifier(),
    )
    handlers = _handlers(store, prospecting, enricher, verifier)
    first = await handlers["account_discovery.find_contacts"].execute(_run())
    second = await handlers["account_discovery.find_contacts"].execute(_run())
    assert first[2]["contact_point_ids"] == second[2]["contact_point_ids"]
    assert len(store.reserve_calls) == 1
    assert enricher.calls == 1


async def test_evidence_change_during_reservation_does_not_interrupt_procurement() -> None:
    class ChangedEvidenceStore(_Store):
        async def reserve(self, tenant_id, **request):
            await super().reserve(tenant_id, **request)
            self.evidence = ()

    store, prospecting, enricher, verifier = (
        ChangedEvidenceStore(), _Prospecting(), _Enricher(), _Verifier()
    )
    handler = _handlers(store, prospecting, enricher, verifier)[
        "account_discovery.find_contacts"
    ]
    run = _run()
    result = await handler.execute(run)
    assert result[:2] == ("advance", "verify_contacts")
    assert enricher.calls == 1
    assert len(store.reserve_calls) == 1
    assert store.evidence_reads == 0
    assert (await handler.execute(run))[:2] == ("advance", "verify_contacts")
    assert enricher.calls == 1
    assert len(store.reserve_calls) == 1


@pytest.mark.parametrize("step_name", ["find_contacts", "verify_contacts"])
@pytest.mark.parametrize("outside", [NOW - timedelta(days=2), NOW + timedelta(days=29)])
async def test_outside_service_period_blocks_provider(
    step_name: str, outside: datetime
) -> None:
    store, prospecting, enricher, verifier = (
        _Store(),
        _Prospecting(),
        _Enricher(),
        _Verifier(),
    )
    handlers = _handlers(store, prospecting, enricher, verifier, now=lambda: outside)
    with pytest.raises(ValidationError, match="service_plan_period_inactive"):
        await handlers[f"account_discovery.{step_name}"].execute(
            _run(contact_point_ids=["cp_1"])
        )
    assert enricher.calls == 0
    assert verifier.calls == []


@pytest.mark.parametrize("outcome", [
    EmailVerificationOutcome.INVALID,
    EmailVerificationOutcome.RISKY,
    EmailVerificationOutcome.UNVERIFIED,
])
async def test_automatic_processing_still_excludes_unreachable_contacts(outcome) -> None:
    class UnreachableVerifier(_Verifier):
        async def verify(self, tenant_id, contact_point_id):
            await super().verify(tenant_id, contact_point_id)
            return EmailVerificationResult(
                outcome, "hunter", NOW, VerificationCostNote.COUNTED
            )

    store, prospecting, enricher, verifier = (
        _Store("advanced"), _Prospecting(), _Enricher(), UnreachableVerifier()
    )
    store.evidence = ()
    handlers = _handlers(store, prospecting, enricher, verifier)
    run = _run()
    run.context.update(
        (await handlers["account_discovery.find_contacts"].execute(run))[2]
    )
    action, next_step, patch = await handlers[
        "account_discovery.verify_contacts"
    ].execute(run)
    assert (action, next_step) == ("advance", "assign_owner")
    assert patch["verified_contact_point_ids"] == []
    assert len(verifier.calls) == len(prospecting.verified) == 2
    assert len(store.reserve_calls) == 1
    assert store.evidence_reads == 0


@pytest.mark.parametrize(
    ("original", "current", "limit"),
    [("advanced", "basic", 1), ("basic", "advanced", 1)],
)
async def test_changed_tier_does_not_expand_existing_receipt(
    original: str, current: str, limit: int
) -> None:
    store, prospecting, enricher, verifier = (
        _Store(original),
        _Prospecting(),
        _Enricher(),
        _Verifier(),
    )
    handlers = _handlers(store, prospecting, enricher, verifier)
    run = _run()
    run.context.update(
        (await handlers["account_discovery.find_contacts"].execute(run))[2]
    )
    store.state = store.state.model_copy(update={"tier": current, "version": 2})
    run.context["contact_point_ids"] = ["cp_0", "cp_1", "cp_2"]
    await handlers["account_discovery.verify_contacts"].execute(run)
    assert len(verifier.calls) == limit
    repeated = await handlers["account_discovery.find_contacts"].execute(run)
    assert repeated[2]["candidate_count"] == limit
    assert len(store.reserve_calls) == 1


async def test_verification_without_receipt_fails_closed() -> None:
    store, prospecting, enricher, verifier = (
        _Store(),
        _Prospecting(),
        _Enricher(),
        _Verifier(),
    )
    handlers = _handlers(store, prospecting, enricher, verifier)
    with pytest.raises(ValidationError, match="contact_reservation_missing"):
        await handlers["account_discovery.verify_contacts"].execute(
            _run(contact_point_ids=["cp_1"])
        )
    assert verifier.calls == []
    assert store.reserve_calls == []


async def test_new_service_period_cannot_consume_old_period_receipt() -> None:
    store, prospecting, enricher, verifier = (
        _Store(),
        _Prospecting(),
        _Enricher(),
        _Verifier(),
    )
    clock = [NOW]
    handlers = _handlers(store, prospecting, enricher, verifier, now=lambda: clock[0])
    run = _run()
    run.context.update(
        (await handlers["account_discovery.find_contacts"].execute(run))[2]
    )
    clock[0] = NOW + timedelta(days=30)
    store.state = store.state.model_copy(
        update={
            "period_started_at": NOW + timedelta(days=29),
            "period_ends_at": NOW + timedelta(days=59),
        }
    )
    with pytest.raises(ValidationError, match="contact_reservation_period_mismatch"):
        await handlers["account_discovery.verify_contacts"].execute(run)
    assert verifier.calls == []


async def test_unassigned_tenant_retains_legacy_processing() -> None:
    store, prospecting, enricher, verifier = (
        _Store(),
        _Prospecting(),
        _Enricher(),
        _Verifier(),
    )
    store.state = None
    handlers = _handlers(store, prospecting, enricher, verifier)
    run = _run()
    run.context.update(
        (await handlers["account_discovery.find_contacts"].execute(run))[2]
    )
    await handlers["account_discovery.verify_contacts"].execute(run)
    assert len(prospecting.recorded) == len(verifier.calls) == 3
    assert store.reserve_calls == []

async def test_new_and_legacy_path_share_campaign_account_contact_receipt():
    from domains.prospecting.service import contact_mission_key
    store, prospecting, enricher, verifier = _Store('advanced'), _Prospecting(), _Enricher(), _Verifier()
    # 新准备路径已预留一位联系人；legacy不得另预留两位或扩大旧收据。
    await store.reserve(TENANT,key=contact_mission_key('cmp_contacts',ACCOUNT),candidates=0,searches=0,contacts=1,now=NOW)
    handlers = _handlers(store,prospecting,enricher,verifier)
    run = _run()
    run.context.update((await handlers['account_discovery.find_contacts'].execute(run))[2])
    await handlers['account_discovery.verify_contacts'].execute(run)
    assert len(store.reserve_calls) == 1 and len(verifier.calls) == 1
