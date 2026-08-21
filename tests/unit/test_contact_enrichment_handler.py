"""contact.enrich handler、可信 adapter 与固定错误映射合同。"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from connectors.contact_enrichment.client import (
    ContactCandidate,
    ContactEmailKind,
    ContactEnrichmentResult,
    ContactSource,
    EnrichmentCostNote,
)
from connectors.hunter.client import (
    HunterAuthRequiredError,
    HunterPermanentError,
    HunterRateLimitedError,
    HunterTransientError,
    HunterUncertainError,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    NeedHypothesisId,
    ProspectAccountId,
    TenantId,
    UserId,
)
from tool_gateway.checks.contact_provider import ContactDiscoveryPreflight
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.contact_enrichment import (
    MANIFEST,
    ContactEnrichmentHandler,
    ToolGatewayContactEnricher,
)
from tool_gateway.handlers.contact_provider_errors import map_contact_provider_error
from tool_gateway.handlers.single_result_slot import ContextLocalSingleResultSlot
from tool_gateway.manifest import CostClass, IdempotencyRequirement, RiskLevel
from tool_gateway.pipeline import ToolCallContext, ToolCallResult

TENANT = TenantId("ten_01J00000000000000000000000")
HYPOTHESIS = NeedHypothesisId("hyp_01J00000000000000000000000")
ACCOUNT = ProspectAccountId("acc_01J00000000000000000000000")
USER = UserId("usr_01J00000000000000000000000")


def _result(email: str = "private@example.com") -> ContactEnrichmentResult:
    source = ContactSource(
        "https://source.example/private",
        date(2026, 8, 1),
        date(2026, 8, 20),
        True,
    )
    return ContactEnrichmentResult(
        (ContactCandidate(email, sources=(source,), email_kind=ContactEmailKind.PERSONAL),),
        "hunter",
        EnrichmentCostNote.COUNTED,
    )


class _Reader:
    def __init__(self, result: ContactEnrichmentResult | BaseException) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    async def find_contacts(
        self,
        tenant_id: TenantId,
        company_domain: str,
        role_hints: tuple[str, ...],
    ) -> ContactEnrichmentResult:
        self.calls.append((tenant_id, company_domain, role_hints))
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _slot() -> ContextLocalSingleResultSlot[ContactEnrichmentResult]:
    return ContextLocalSingleResultSlot(
        "ceb", lambda _prefix: "ceb_01J00000000000000000000000"
    )


def _handler(
    reader: _Reader,
    slot: ContextLocalSingleResultSlot[ContactEnrichmentResult] | None = None,
) -> ContactEnrichmentHandler:
    return ContactEnrichmentHandler(
        reader,
        slot or _slot(),
        HmacFingerprintProvider("contact-v1", b"c" * 32),
    )


def _ctx(params: dict[str, object]) -> ToolCallContext:
    return ToolCallContext(TENANT, USER, "contact.enrich", params)


def _preflight() -> ContactDiscoveryPreflight:
    return ContactDiscoveryPreflight(
        TENANT, HYPOTHESIS, ACCOUNT, "工业 五金", "DE", "example.com"
    )


def test_manifest_is_exact_medium_paid_read_contract() -> None:
    assert MANIFEST.tool_id == "contact.enrich"
    assert MANIFEST.version == "v1"
    assert MANIFEST.risk_level is RiskLevel.MEDIUM
    assert MANIFEST.cost_class is CostClass.MEDIUM
    assert MANIFEST.requires_approval is False
    assert MANIFEST.idempotency is IdempotencyRequirement.NONE
    assert MANIFEST.required_permissions == ("contact:enrich",)
    assert MANIFEST.checks == (
        "tenant",
        "permission",
        "playbook",
        "country_policy",
        "suppression",
        "rate_limit",
    )
    assert MANIFEST.redact_fields == ()
    assert MANIFEST.input_schema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_prepare_canonicalizes_hints_and_uses_safe_audit_projection() -> None:
    handler = _handler(_Reader(_result()))
    ctx = _ctx(
        {
            "hypothesis_id": str(HYPOTHESIS),
            "account_id": str(ACCOUNT),
            "role_hints": (" SALES  Manager ", "procurement", "sales manager"),
        }
    )
    prepared = await handler.prepare(ctx, _preflight())
    expected, version = HmacFingerprintProvider(
        "contact-v1", b"c" * 32
    ).fingerprint(
        (
            str(TENANT).encode(),
            str(HYPOTHESIS).encode(),
            str(ACCOUNT).encode(),
            b"example.com",
            b"procurement",
            b"sales manager",
        )
    )
    assert prepared.request_fingerprint == expected
    assert prepared.fingerprint_version == version
    assert dict(prepared.audit_projection) == {
        "hypothesis_id": str(HYPOTHESIS),
        "account_id": str(ACCOUNT),
        "role_hint_count": 2,
    }
    assert "example.com" not in repr(prepared)
    assert "sales" not in repr(prepared).casefold()


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"hypothesis_id": str(HYPOTHESIS), "account_id": str(ACCOUNT), "role_hints": (), "extra": 1},
        {"hypothesis_id": "bad", "account_id": str(ACCOUNT), "role_hints": ()},
        {"hypothesis_id": str(HYPOTHESIS), "account_id": "bad", "role_hints": ()},
        {"hypothesis_id": str(HYPOTHESIS), "account_id": str(ACCOUNT), "role_hints": ["sales"]},
        {"hypothesis_id": str(HYPOTHESIS), "account_id": str(ACCOUNT), "role_hints": tuple(str(i) for i in range(11))},
        {"hypothesis_id": str(HYPOTHESIS), "account_id": str(ACCOUNT), "role_hints": ("",)},
    ],
)
@pytest.mark.asyncio
async def test_prepare_rejects_non_exact_params(params: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="contact enrichment params 无效"):
        await _handler(_Reader(_result())).prepare(_ctx(params), _preflight())


@pytest.mark.asyncio
async def test_prepare_requires_matching_authoritative_preflight() -> None:
    params = {
        "hypothesis_id": str(HYPOTHESIS),
        "account_id": str(ACCOUNT),
        "role_hints": (),
    }
    wrong = ContactDiscoveryPreflight(
        TenantId("ten_01J00000000000000000000001"),
        HYPOTHESIS,
        ACCOUNT,
        "category",
        "DE",
        "example.com",
    )
    with pytest.raises(ValidationError, match="contact enrichment preflight 无效"):
        await _handler(_Reader(_result())).prepare(_ctx(params), wrong)


@pytest.mark.asyncio
async def test_execute_returns_only_handle_and_take_consumes() -> None:
    reader = _Reader(_result())
    slot = _slot()
    handler = _handler(reader, slot)
    prepared = await handler.prepare(
        _ctx({"hypothesis_id": str(HYPOTHESIS), "account_id": str(ACCOUNT), "role_hints": ()}),
        _preflight(),
    )
    output = await handler.execute(TENANT, prepared)
    assert output == {"provider_ref": "ceb_01J00000000000000000000000"}
    assert reader.calls == [(TENANT, "example.com", ())]
    result = slot.take(output["provider_ref"])
    assert result.candidates[0].email == "private@example.com"
    assert slot.is_empty


@pytest.mark.parametrize(
    ("error", "category", "retry_after"),
    [
        (HunterAuthRequiredError(), ToolErrorCategory.PROVIDER_AUTH_REQUIRED, None),
        (HunterRateLimitedError(19), ToolErrorCategory.RATE_LIMITED, 19),
        (HunterPermanentError(), ToolErrorCategory.PROVIDER_PERMANENT, None),
        (HunterTransientError(), ToolErrorCategory.PROVIDER_TRANSIENT, None),
        (HunterUncertainError(), ToolErrorCategory.RECONCILIATION_REQUIRED, None),
    ],
)
def test_connector_error_mapping_is_single_fixed_boundary(
    error: BaseException,
    category: ToolErrorCategory,
    retry_after: int | None,
) -> None:
    mapped = map_contact_provider_error(error)
    assert mapped.category is category
    assert mapped.retry_after_seconds == retry_after


@pytest.mark.asyncio
async def test_handler_error_clears_slot_and_maps_uncertainty() -> None:
    slot = _slot()
    handler = _handler(_Reader(HunterUncertainError()), slot)
    prepared = await handler.prepare(
        _ctx({"hypothesis_id": str(HYPOTHESIS), "account_id": str(ACCOUNT), "role_hints": ()}),
        _preflight(),
    )
    with pytest.raises(ToolGatewayError) as captured:
        await handler.execute(TENANT, prepared)
    assert captured.value.category is ToolErrorCategory.RECONCILIATION_REQUIRED
    assert slot.is_empty


class _Gateway:
    def __init__(self, result: ToolCallResult | BaseException) -> None:
        self.result = result
        self.calls = 0

    async def invoke(self, _ctx: ToolCallContext) -> ToolCallResult:
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


@pytest.mark.asyncio
async def test_trusted_adapter_takes_once_and_clears_all_failure_paths() -> None:
    slot = _slot()
    handle = slot.put(_result())
    success = ToolCallResult(
        "contact.enrich",
        ToolCallStatus.SUCCEEDED,
        output={"provider_ref": handle},
        tool_call_id="tcl_01J00000000000000000000000",
    )
    adapter = ToolGatewayContactEnricher(_Gateway(success), slot, USER)
    result = await adapter.find_contacts(TENANT, HYPOTHESIS, ACCOUNT, ())
    assert result.candidates[0].email == "private@example.com"
    assert slot.is_empty

    for gateway_result, expected_error in (
        (
            ToolCallResult(
                "contact.enrich",
                ToolCallStatus.REJECTED,
                rejected=__import__("tool_gateway.pipeline", fromlist=["CheckRejection"]).CheckRejection("permission", "permission:denied", "无权限"),
                tool_call_id="tcl_01J00000000000000000000001",
            ),
            ToolGatewayError,
        ),
        (RuntimeError("private gateway failure"), RuntimeError),
        (asyncio.CancelledError(), asyncio.CancelledError),
    ):
        failure_slot = _slot()
        failure_slot.put(_result())
        failure_gateway = _Gateway(gateway_result)
        failing = ToolGatewayContactEnricher(failure_gateway, failure_slot, USER)
        with pytest.raises(expected_error):
            await failing.find_contacts(TENANT, HYPOTHESIS, ACCOUNT, ())
        assert failure_slot.is_empty
        assert failure_gateway.calls == 1


@pytest.mark.asyncio
async def test_twenty_concurrent_trusted_calls_do_not_cross_results() -> None:
    counter = 0

    def make_id(prefix: str) -> str:
        nonlocal counter
        counter += 1
        return f"{prefix}_01J00000000000000000000{counter:03d}"

    slot = ContextLocalSingleResultSlot[ContactEnrichmentResult]("ceb", make_id)

    class PuttingGateway:
        async def invoke(self, ctx: ToolCallContext) -> ToolCallResult:
            email = f"private-{ctx.params['account_id']}@example.com"
            handle = slot.put(_result(email))
            await asyncio.sleep(0)
            return ToolCallResult(
                "contact.enrich",
                ToolCallStatus.SUCCEEDED,
                output={"provider_ref": handle},
                tool_call_id="tcl_01J00000000000000000000000",
            )

    adapter = ToolGatewayContactEnricher(PuttingGateway(), slot, USER)

    async def call(index: int) -> str:
        account = ProspectAccountId(f"acc_01J00000000000000000000{index:03d}")
        result = await adapter.find_contacts(TENANT, HYPOTHESIS, account, ())
        return result.candidates[0].email

    results = await asyncio.gather(*(call(index) for index in range(20)))
    assert results == [
        f"private-acc_01J00000000000000000000{index:03d}@example.com"
        for index in range(20)
    ]
