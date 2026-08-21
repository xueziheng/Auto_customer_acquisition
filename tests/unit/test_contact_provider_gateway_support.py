"""联系人 Provider 的 Gateway preflight、配额与 task-local 槽合同。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from domains.outreach.schemas import SuppressionTarget
from domains.prospecting.schemas import (
    ContactPointKind,
    ContactPointView,
    VerificationStatus,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ContactPointId,
    NeedHypothesisId,
    ProspectAccountId,
    ProspectContactId,
    TenantId,
    UserId,
)
from tool_gateway.checks.contact_provider import (
    ContactCountryPolicyCheck,
    ContactDiscoveryPreflight,
    ContactEnrichmentPlaybookCheck,
    ContactProviderRateLimitCheck,
    ContactProviderSuppressionCheck,
    ContactResourceTenantCheck,
    ContactVerificationPreflight,
    InMemoryHunterQuotaGuard,
)
from tool_gateway.errors import ToolErrorCategory, ToolGatewayError
from tool_gateway.handlers.single_result_slot import ContextLocalSingleResultSlot
from tool_gateway.pipeline import ToolCallContext, ToolInvocationState

TENANT = TenantId("ten_01J00000000000000000000000")
OTHER_TENANT = TenantId("ten_01J00000000000000000000001")
USER = UserId("usr_01J00000000000000000000000")
HYPOTHESIS = NeedHypothesisId("hyp_01J00000000000000000000000")
ACCOUNT = ProspectAccountId("acc_01J00000000000000000000000")
CONTACT = ProspectContactId("con_01J00000000000000000000000")
POINT = ContactPointId("cp_01J00000000000000000000000")
NOW = datetime(2026, 8, 21, 12, tzinfo=UTC)


def _state(tool_id: str) -> ToolInvocationState:
    return ToolInvocationState(
        manifest=SimpleNamespace(
            tool_id=tool_id,
            version="v1",
            risk_level=SimpleNamespace(value="medium"),
            cost_class=SimpleNamespace(value="low"),
            idempotency=SimpleNamespace(value="none"),
            checks=("tenant",),
        ),
        tool_call_id="tcl_01J00000000000000000000000",
    )


def _ctx(tool_id: str, params: dict[str, object]) -> ToolCallContext:
    return ToolCallContext(TENANT, USER, tool_id, params)


def _point(*, checked_at: datetime | None = NOW) -> ContactPointView:
    observed = checked_at is not None
    return ContactPointView(
        POINT,
        TENANT,
        CONTACT,
        ACCOUNT,
        ContactPointKind.EMAIL,
        "private-point-canary@example.com",
        VerificationStatus.INVALID if observed else VerificationStatus.UNVERIFIED,
        NOW - timedelta(days=90),
        verification_provider="hunter" if observed else None,
        verification_checked_at=checked_at,
        verification_cost_note=(
            "hunter.email_verifier.counted" if observed else None
        ),
    )


class _DiscoveryReader:
    def __init__(self, result: ContactDiscoveryPreflight | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    async def preflight(
        self,
        tenant_id: TenantId,
        hypothesis_id: NeedHypothesisId,
        account_id: ProspectAccountId,
    ) -> ContactDiscoveryPreflight:
        self.calls.append((tenant_id, hypothesis_id, account_id))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class _CountryReader:
    def __init__(self, allowed: bool | BaseException) -> None:
        self.allowed = allowed
        self.calls: list[tuple[object, ...]] = []

    async def allows_contact_enrichment(
        self, tenant_id: TenantId, country: str
    ) -> bool:
        self.calls.append((tenant_id, country))
        if isinstance(self.allowed, BaseException):
            raise self.allowed
        return self.allowed


class _PointReader:
    def __init__(self, point: ContactPointView | BaseException) -> None:
        self.point = point
        self.calls: list[tuple[object, ...]] = []

    async def get_contact_point(
        self, tenant_id: TenantId, contact_point_id: ContactPointId
    ) -> ContactPointView:
        self.calls.append((tenant_id, contact_point_id))
        if isinstance(self.point, BaseException):
            raise self.point
        return self.point


class _SuppressionReader:
    def __init__(self, suppressed: bool | BaseException = False) -> None:
        self.suppressed = suppressed
        self.targets: list[SuppressionTarget] = []

    async def is_suppressed(
        self, tenant_id: TenantId, target: SuppressionTarget
    ) -> bool:
        assert tenant_id == TENANT
        self.targets.append(target)
        if isinstance(self.suppressed, BaseException):
            raise self.suppressed
        return self.suppressed


class _Quota:
    def __init__(self, retry_after: int | None | BaseException = None) -> None:
        self.retry_after = retry_after
        self.calls: list[tuple[object, ...]] = []

    async def reserve(
        self, tenant_id: TenantId, capability: str, now: datetime
    ) -> int | None:
        self.calls.append((tenant_id, capability, now))
        if isinstance(self.retry_after, BaseException):
            raise self.retry_after
        return self.retry_after


@pytest.mark.asyncio
async def test_resource_shape_rejects_before_any_reader() -> None:
    check = ContactResourceTenantCheck()
    for tool_id, params in (
        ("contact.enrich", {"hypothesis_id": "bad", "account_id": str(ACCOUNT)}),
        ("contact.enrich", {"hypothesis_id": str(HYPOTHESIS), "account_id": "bad"}),
        ("contact.verify", {"contact_point_id": "bad"}),
        ("contact.unknown", {}),
    ):
        rejection = await check.check(_ctx(tool_id, params), _state(tool_id))
        assert rejection is not None
        assert rejection.rule == "tenant:contact_resource_binding"
    assert await check.check(
        _ctx(
            "contact.enrich",
            {"hypothesis_id": str(HYPOTHESIS), "account_id": str(ACCOUNT)},
        ),
        _state("contact.enrich"),
    ) is None
    assert await check.check(
        _ctx("contact.verify", {"contact_point_id": str(POINT)}),
        _state("contact.verify"),
    ) is None


@pytest.mark.asyncio
async def test_playbook_binds_tenant_ids_and_authoritative_facts() -> None:
    preflight = ContactDiscoveryPreflight(
        TENANT,
        HYPOTHESIS,
        ACCOUNT,
        "industrial_hardware",
        "DE",
        "example.com",
    )
    reader = _DiscoveryReader(preflight)
    check = ContactEnrichmentPlaybookCheck(reader)
    ctx = _ctx(
        "contact.enrich",
        {
            "hypothesis_id": str(HYPOTHESIS),
            "account_id": str(ACCOUNT),
            "country": "forged-country",
            "category": "forged-category",
        },
    )
    state = _state("contact.enrich")
    assert await check.check(ctx, state) is None
    assert state.preflight == preflight
    assert reader.calls == [(TENANT, HYPOTHESIS, ACCOUNT)]


def test_discovery_preflight_accepts_authoritative_unicode_and_hyphenated_domain() -> None:
    preflight = ContactDiscoveryPreflight(
        TENANT,
        HYPOTHESIS,
        ACCOUNT,
        "工业 五金",
        "DE",
        "buyer-tools.example.com",
    )
    assert preflight.category == "工业 五金"
    assert preflight.website_domain == "buyer-tools.example.com"


@pytest.mark.asyncio
async def test_playbook_rejects_mismatched_returned_binding() -> None:
    reader = _DiscoveryReader(
        ContactDiscoveryPreflight(
            OTHER_TENANT,
            HYPOTHESIS,
            ACCOUNT,
            "category",
            "DE",
            "example.com",
        )
    )
    check = ContactEnrichmentPlaybookCheck(reader)
    state = _state("contact.enrich")
    rejection = await check.check(
        _ctx(
            "contact.enrich",
            {"hypothesis_id": str(HYPOTHESIS), "account_id": str(ACCOUNT)},
        ),
        state,
    )
    assert rejection is not None
    assert state.preflight is None


@pytest.mark.asyncio
async def test_unknown_country_defaults_deny_and_dependency_failure_fails_closed() -> None:
    preflight = ContactDiscoveryPreflight(
        TENANT, HYPOTHESIS, ACCOUNT, "category", "ZZ", "example.com"
    )
    state = _state("contact.enrich")
    state.preflight = preflight
    denied = ContactCountryPolicyCheck(_CountryReader(False))
    rejection = await denied.check(_ctx("contact.enrich", {}), state)
    assert rejection is not None
    assert rejection.rule == "country_policy:contact_enrichment"

    failed = ContactCountryPolicyCheck(_CountryReader(RuntimeError("private")))
    with pytest.raises(ToolGatewayError) as captured:
        await failed.check(_ctx("contact.enrich", {}), state)
    assert captured.value.category is ToolErrorCategory.PROVIDER_TRANSIENT
    assert "private" not in repr(captured.value)


@pytest.mark.asyncio
async def test_enrichment_suppression_checks_authoritative_account() -> None:
    suppression = _SuppressionReader(True)
    check = ContactProviderSuppressionCheck(
        _PointReader(_point()), suppression, now=lambda: NOW
    )
    state = _state("contact.enrich")
    state.preflight = ContactDiscoveryPreflight(
        TENANT, HYPOTHESIS, ACCOUNT, "category", "DE", "example.com"
    )
    rejection = await check.check(_ctx("contact.enrich", {}), state)
    assert rejection is not None
    assert rejection.rule == "suppression:contact_provider"
    assert suppression.targets == [SuppressionTarget(account_id=ACCOUNT)]


@pytest.mark.asyncio
async def test_verification_suppression_stores_cache_aware_typed_preflight() -> None:
    point_reader = _PointReader(_point(checked_at=NOW - timedelta(days=29)))
    suppression = _SuppressionReader(False)
    check = ContactProviderSuppressionCheck(
        point_reader, suppression, now=lambda: NOW
    )
    state = _state("contact.verify")
    assert await check.check(
        _ctx("contact.verify", {"contact_point_id": str(POINT)}), state
    ) is None
    assert isinstance(state.preflight, ContactVerificationPreflight)
    assert state.preflight.cache_valid is True
    assert "private-point-canary" not in repr(state.preflight)
    assert point_reader.calls == [(TENANT, POINT)]
    assert suppression.targets == [SuppressionTarget(contact_point_id=POINT)]


@pytest.mark.asyncio
async def test_exact_cache_expiry_reserves_quota_but_fresh_cache_skips() -> None:
    quota = _Quota()
    check = ContactProviderRateLimitCheck(quota, now=lambda: NOW)
    ctx = _ctx("contact.verify", {"contact_point_id": str(POINT)})

    fresh = _state("contact.verify")
    fresh.preflight = ContactVerificationPreflight(TENANT, _point(), True)
    assert await check.check(ctx, fresh) is None
    assert quota.calls == []

    expired = _state("contact.verify")
    expired.preflight = ContactVerificationPreflight(
        TENANT,
        _point(checked_at=NOW - timedelta(days=30)),
        False,
    )
    assert await check.check(ctx, expired) is None
    assert quota.calls == [(TENANT, "contact.verify", NOW)]


@pytest.mark.asyncio
async def test_quota_rejection_is_fixed_rate_limit_and_errors_fail_closed() -> None:
    state = _state("contact.enrich")
    state.preflight = ContactDiscoveryPreflight(
        TENANT, HYPOTHESIS, ACCOUNT, "category", "DE", "example.com"
    )
    with pytest.raises(ToolGatewayError) as limited:
        await ContactProviderRateLimitCheck(
            _Quota(17), now=lambda: NOW
        ).check(_ctx("contact.enrich", {}), state)
    assert limited.value.category is ToolErrorCategory.RATE_LIMITED
    assert limited.value.retry_after_seconds == 17

    with pytest.raises(ToolGatewayError) as failed:
        await ContactProviderRateLimitCheck(
            _Quota(RuntimeError("private")), now=lambda: NOW
        ).check(_ctx("contact.enrich", {}), state)
    assert failed.value.category is ToolErrorCategory.PROVIDER_TRANSIENT


@pytest.mark.asyncio
async def test_in_memory_hunter_quota_uses_exact_per_second_limits() -> None:
    guard = InMemoryHunterQuotaGuard()
    for _ in range(15):
        assert await guard.reserve(TENANT, "contact.enrich", NOW) is None
    assert await guard.reserve(TENANT, "contact.enrich", NOW) == 1
    assert await guard.reserve(
        TENANT, "contact.enrich", NOW + timedelta(seconds=1)
    ) is None

    for _ in range(10):
        assert await guard.reserve(OTHER_TENANT, "contact.verify", NOW) is None
    assert await guard.reserve(OTHER_TENANT, "contact.verify", NOW) == 1


@pytest.mark.asyncio
async def test_in_memory_hunter_quota_uses_exact_per_minute_limits() -> None:
    guard = InMemoryHunterQuotaGuard()
    for index in range(500):
        observed_at = NOW + timedelta(seconds=index // 9)
        assert await guard.reserve(TENANT, "contact.enrich", observed_at) is None
    assert await guard.reserve(
        TENANT, "contact.enrich", NOW + timedelta(seconds=55)
    ) == 5

    for index in range(300):
        observed_at = NOW + timedelta(seconds=index // 9)
        assert await guard.reserve(OTHER_TENANT, "contact.verify", observed_at) is None
    assert await guard.reserve(
        OTHER_TENANT, "contact.verify", NOW + timedelta(seconds=33)
    ) == 27


def test_slot_is_single_use_and_wrong_handle_does_not_consume() -> None:
    ids = iter(
        [
            "ceb_01J00000000000000000000000",
            "ceb_01J00000000000000000000001",
        ]
    )
    slot = ContextLocalSingleResultSlot[str]("ceb", lambda _prefix: next(ids))
    handle = slot.put("tenant-one-private")
    with pytest.raises(ValidationError, match="single result slot 已占用"):
        slot.put("second")
    with pytest.raises(ValidationError, match="single result handle 无效"):
        slot.take("ceb_01J00000000000000000000009")
    assert slot.take(handle) == "tenant-one-private"
    with pytest.raises(ValidationError, match="single result handle 无效"):
        slot.take(handle)
    second = slot.put("clear-me")
    slot.discard_all()
    with pytest.raises(ValidationError):
        slot.take(second)
    assert slot.is_empty


@pytest.mark.asyncio
async def test_twenty_concurrent_tasks_never_cross_slot_values() -> None:
    counter = 0

    def make_id(prefix: str) -> str:
        nonlocal counter
        counter += 1
        return f"{prefix}_01J00000000000000000000{counter:03d}"

    slot = ContextLocalSingleResultSlot[str]("veb", make_id)
    ready = asyncio.Barrier(20)

    async def worker(index: int) -> str:
        value = f"tenant-canary-{index:02d}"
        handle = slot.put(value)
        await ready.wait()
        return slot.take(handle)

    results = await asyncio.gather(*(worker(index) for index in range(20)))
    assert results == [f"tenant-canary-{index:02d}" for index in range(20)]
    assert slot.is_empty


@pytest.mark.asyncio
async def test_child_task_cannot_consume_parent_and_gets_independent_slot() -> None:
    ids = iter(
        [
            "ceb_01J00000000000000000000000",
            "ceb_01J00000000000000000000001",
        ]
    )
    slot = ContextLocalSingleResultSlot[str]("ceb", lambda _prefix: next(ids))
    parent_handle = slot.put("parent-private")

    async def child() -> str:
        with pytest.raises(ValidationError):
            slot.take(parent_handle)
        child_handle = slot.put("child-private")
        return slot.take(child_handle)

    assert await asyncio.create_task(child()) == "child-private"
    assert slot.take(parent_handle) == "parent-private"


@pytest.mark.asyncio
async def test_cancellation_cleanup_can_discard_task_local_value() -> None:
    slot = ContextLocalSingleResultSlot[str](
        "ceb", lambda _prefix: "ceb_01J00000000000000000000000"
    )
    cleaned = asyncio.Event()

    async def worker() -> None:
        slot.put("private-cancelled-value")
        try:
            await asyncio.Event().wait()
        finally:
            slot.discard_all()
            assert slot.is_empty
            cleaned.set()

    task = asyncio.create_task(worker())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
