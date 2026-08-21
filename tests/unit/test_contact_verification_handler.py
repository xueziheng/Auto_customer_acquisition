"""contact.verify handler 的缓存、typed 隐私事实与清槽合同。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from connectors.email_verification.client import (
    EmailVerificationOutcome,
    EmailVerificationResult,
    VerificationCostNote,
)
from connectors.hunter.client import HunterUncertainError
from domains.prospecting.schemas import (
    ContactPointKind,
    ContactPointView,
    VerificationStatus,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    ContactPointId,
    ProspectAccountId,
    ProspectContactId,
    TenantId,
    UserId,
)
from tool_gateway.checks.contact_provider import ContactVerificationPreflight
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.contact_verification import (
    MANIFEST,
    ContactVerificationHandler,
    ToolGatewayContactVerifier,
)
from tool_gateway.handlers.single_result_slot import ContextLocalSingleResultSlot
from tool_gateway.manifest import CostClass, IdempotencyRequirement, RiskLevel
from tool_gateway.pipeline import ToolCallContext, ToolCallResult

TENANT = TenantId("ten_01J00000000000000000000000")
USER = UserId("usr_01J00000000000000000000000")
POINT = ContactPointId("cp_01J00000000000000000000000")
NOW = datetime(2026, 8, 21, 15, tzinfo=UTC)
EMAIL = "private-verify-canary@example.com"


def _point(
    status: VerificationStatus,
    checked: datetime | None,
    *,
    tenant_id: TenantId = TENANT,
) -> ContactPointView:
    observed = checked is not None
    return ContactPointView(
        POINT,
        tenant_id,
        ProspectContactId("con_01J00000000000000000000000"),
        ProspectAccountId("acc_01J00000000000000000000000"),
        ContactPointKind.EMAIL,
        EMAIL,
        status,
        NOW - timedelta(days=90),
        verified_at=(checked if status is VerificationStatus.VERIFIED else None),
        verification_provider=("hunter" if observed else None),
        verification_checked_at=checked,
        verification_cost_note=("hunter.email_verifier.counted" if observed else None),
    )


class _Reader:
    def __init__(self, result: EmailVerificationResult | BaseException) -> None:
        self.result = result
        self.calls = 0

    async def verify(self, tenant_id: TenantId, email: str) -> EmailVerificationResult:
        assert tenant_id == TENANT and email == EMAIL
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def _slot() -> ContextLocalSingleResultSlot[EmailVerificationResult]:
    return ContextLocalSingleResultSlot(
        "veb", lambda _p: "veb_01J00000000000000000000000"
    )


def _handler(reader: _Reader, slot=None) -> ContactVerificationHandler:
    return ContactVerificationHandler(
        reader,
        slot or _slot(),
        HmacFingerprintProvider("verify-v1", b"v" * 32),
        now=lambda: NOW,
    )


def _ctx() -> ToolCallContext:
    return ToolCallContext(
        TENANT, USER, "contact.verify", {"contact_point_id": str(POINT)}
    )


def test_manifest_is_exact() -> None:
    assert MANIFEST.tool_id == "contact.verify"
    assert MANIFEST.risk_level is RiskLevel.MEDIUM
    assert MANIFEST.cost_class is CostClass.LOW
    assert MANIFEST.idempotency is IdempotencyRequirement.NONE
    assert MANIFEST.required_permissions == ("contact:verify",)
    assert MANIFEST.checks == ("tenant", "permission", "suppression", "rate_limit")


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (VerificationStatus.VERIFIED, EmailVerificationOutcome.VERIFIED),
        (VerificationStatus.INVALID, EmailVerificationOutcome.INVALID),
        (VerificationStatus.RISKY, EmailVerificationOutcome.RISKY),
        (VerificationStatus.UNVERIFIED, EmailVerificationOutcome.UNVERIFIED),
    ],
)
@pytest.mark.asyncio
async def test_all_four_fresh_cached_outcomes_skip_provider(status, outcome) -> None:
    reader = _Reader(
        EmailVerificationResult(outcome, "hunter", NOW, VerificationCostNote.COUNTED)
    )
    slot = _slot()
    handler = _handler(reader, slot)
    preflight = ContactVerificationPreflight(
        TENANT, _point(status, NOW - timedelta(days=29)), True
    )
    prepared = await handler.prepare(_ctx(), preflight)
    assert EMAIL not in repr(prepared)
    assert prepared.payload.email is None  # type: ignore[attr-defined]
    assert dict(prepared.audit_projection) == {
        "contact_point_id": str(POINT),
        "cache_hit": True,
    }
    output = await handler.execute(TENANT, prepared)
    result = slot.take(output["provider_ref"])
    assert result.outcome is outcome
    assert result.cost_note is VerificationCostNote.CACHE_HIT
    assert reader.calls == 0


@pytest.mark.asyncio
async def test_exact_30_day_expiry_is_live_and_email_not_in_fingerprint_parts() -> None:
    live = EmailVerificationResult(
        EmailVerificationOutcome.INVALID, "hunter", NOW, VerificationCostNote.COUNTED
    )
    reader = _Reader(live)
    slot = _slot()
    handler = _handler(reader, slot)
    preflight = ContactVerificationPreflight(
        TENANT, _point(VerificationStatus.INVALID, NOW - timedelta(days=30)), False
    )
    prepared = await handler.prepare(_ctx(), preflight)
    expected, _ = HmacFingerprintProvider("verify-v1", b"v" * 32).fingerprint(
        (str(TENANT).encode(), str(POINT).encode())
    )
    assert prepared.request_fingerprint == expected
    assert EMAIL not in repr(prepared)
    output = await handler.execute(TENANT, prepared)
    assert slot.take(output["provider_ref"]) == live
    assert reader.calls == 1


@pytest.mark.asyncio
async def test_stale_cache_preflight_fails_closed() -> None:
    live = EmailVerificationResult(
        EmailVerificationOutcome.INVALID,
        "hunter",
        NOW,
        VerificationCostNote.COUNTED,
    )
    reader = _Reader(live)
    preflight = ContactVerificationPreflight(
        TENANT,
        _point(VerificationStatus.INVALID, NOW - timedelta(days=30)),
        True,
    )
    with pytest.raises(ValidationError, match="contact verification cache 无效"):
        await _handler(reader).prepare(_ctx(), preflight)
    assert reader.calls == 0


@pytest.mark.asyncio
async def test_strict_params_and_tenant_kind_binding() -> None:
    reader = _Reader(
        EmailVerificationResult(
            EmailVerificationOutcome.INVALID,
            "hunter",
            NOW,
            VerificationCostNote.COUNTED,
        )
    )
    for params in (
        {},
        {"contact_point_id": str(POINT), "email": EMAIL},
        {"contact_point_id": "bad"},
    ):
        with pytest.raises(ValidationError):
            await _handler(reader).prepare(
                ToolCallContext(TENANT, USER, "contact.verify", params), None
            )
    wrong_kind = ContactVerificationPreflight(
        TENANT,
        replace(
            _point(VerificationStatus.UNVERIFIED, None),
            kind=ContactPointKind.PHONE,
        ),
        False,
    )
    with pytest.raises(ValidationError):
        await _handler(reader).prepare(_ctx(), wrong_kind)

    other_tenant = TenantId("ten_01J00000000000000000000001")
    wrong_tenant = ContactVerificationPreflight(
        other_tenant,
        _point(
            VerificationStatus.UNVERIFIED,
            None,
            tenant_id=other_tenant,
        ),
        False,
    )
    with pytest.raises(ValidationError):
        await _handler(reader).prepare(_ctx(), wrong_tenant)

    wrong_status = ContactVerificationPreflight(
        TENANT,
        replace(
            _point(VerificationStatus.UNVERIFIED, None),
            verification="verified",  # type: ignore[arg-type]
        ),
        False,
    )
    with pytest.raises(ValidationError):
        await _handler(reader).prepare(_ctx(), wrong_status)


@pytest.mark.asyncio
async def test_privacy_claim_stays_typed_and_no_business_writer_dependency() -> None:
    privacy = EmailVerificationResult(
        EmailVerificationOutcome.UNVERIFIED,
        "hunter",
        NOW,
        VerificationCostNote.PRIVACY_REFUSED,
        True,
    )
    reader = _Reader(privacy)
    slot = _slot()
    handler = _handler(reader, slot)
    assert not hasattr(handler, "record_verification")
    assert not hasattr(handler, "handle_erasure_request")
    prepared = await handler.prepare(
        _ctx(),
        ContactVerificationPreflight(
            TENANT, _point(VerificationStatus.UNVERIFIED, None), False
        ),
    )
    output = await handler.execute(TENANT, prepared)
    assert slot.take(output["provider_ref"]).privacy_claimed is True


@pytest.mark.asyncio
async def test_uncertain_paid_call_is_not_retried_and_clears_slot() -> None:
    reader = _Reader(HunterUncertainError())
    slot = _slot()
    handler = _handler(reader, slot)
    prepared = await handler.prepare(
        _ctx(),
        ContactVerificationPreflight(
            TENANT, _point(VerificationStatus.UNVERIFIED, None), False
        ),
    )
    with pytest.raises(ToolGatewayError) as captured:
        await handler.execute(TENANT, prepared)
    assert captured.value.category is ToolErrorCategory.RECONCILIATION_REQUIRED
    assert reader.calls == 1
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
async def test_trusted_adapter_takes_once() -> None:
    slot = _slot()
    handle = slot.put(
        EmailVerificationResult(
            EmailVerificationOutcome.VERIFIED,
            "hunter",
            NOW,
            VerificationCostNote.COUNTED,
        )
    )
    gateway = _Gateway(
        ToolCallResult(
            "contact.verify",
            ToolCallStatus.SUCCEEDED,
            output={"provider_ref": handle},
            tool_call_id="tcl_01J00000000000000000000000",
        )
    )
    result = await ToolGatewayContactVerifier(gateway, slot, USER).verify(TENANT, POINT)
    assert result.outcome is EmailVerificationOutcome.VERIFIED
    assert slot.is_empty and gateway.calls == 1


@pytest.mark.asyncio
async def test_trusted_adapter_clears_slot_on_all_gateway_failures() -> None:
    for gateway_result, expected_error in (
        (
            ToolCallResult(
                "contact.verify",
                ToolCallStatus.FAILED_TRANSIENT,
                error_category=ToolErrorCategory.PROVIDER_TRANSIENT,
                tool_call_id="tcl_01J00000000000000000000001",
            ),
            Exception,
        ),
        (RuntimeError("private gateway failure"), RuntimeError),
        (asyncio.CancelledError(), asyncio.CancelledError),
    ):
        slot = _slot()
        slot.put(
            EmailVerificationResult(
                EmailVerificationOutcome.INVALID,
                "hunter",
                NOW,
                VerificationCostNote.COUNTED,
            )
        )
        gateway = _Gateway(gateway_result)
        with pytest.raises(expected_error):
            await ToolGatewayContactVerifier(gateway, slot, USER).verify(TENANT, POINT)
        assert slot.is_empty
        assert gateway.calls == 1
