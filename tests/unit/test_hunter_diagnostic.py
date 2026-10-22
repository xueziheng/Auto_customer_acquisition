"""固定 Hunter 样本诊断的受控 transport、权限与单次执行边界。"""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from connectors.hunter.client import HunterConnector
from connectors.hunter.transport import (
    HunterErrorCode,
    HunterHttpStatusError,
    HunterNetworkError,
)
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import IdempotencyKey, TenantId, UserId
from tests.unit.test_hunter_contact_enrichment import (
    _email,
    _Resolver,
    _response,
    _Transport,
)
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.hunter_diagnostic import (
    HUNTER_DIAGNOSTIC_MANIFEST,
    HunterDiagnosticHandler,
)
from tool_gateway.pipeline import ToolCallContext, ToolCallResult


class Authorization:
    def __init__(self):
        self.allowed = True
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if not self.allowed:
            raise PermissionDenied("平台权限不足")


def setup(response=None):
    transport = _Transport([response or _response([
        _email("ada@hunter.io"), _email("support@hunter.io", first_name=None, last_name=None, kind="generic"),
    ])])
    resolver, authorize = _Resolver(), Authorization()
    handler = HunterDiagnosticHandler(
        tenant_id=TenantId("control"), actor_id="platform-user", configuration_version="test-v1",
        authorize=authorize, connector=HunterConnector(transport), resolver=resolver,
        secret_ref="SYNTHETIC_HUNTER_KEY", fingerprints=HmacFingerprintProvider("test-v1", b"x" * 32),
    )
    return handler, transport, resolver, authorize


def context(**updates):
    return ToolCallContext(**{
        "tenant_id": TenantId("control"), "user_id": UserId("platform-user"),
        "tool_id": HUNTER_DIAGNOSTIC_MANIFEST.tool_id,
        "params": {"configuration_version": "test-v1"},
        "idempotency_key": IdempotencyKey("hunter-sample:test-v1"),
        **updates,
    })


async def test_actual_typed_result_counts_and_manifest_output_are_compatible():
    handler, transport, resolver, authorize = setup()
    prepared = await handler.prepare(context(), None)
    assert not resolver.refs and not transport.calls
    output = await handler.execute(TenantId("control"), prepared)
    HUNTER_DIAGNOSTIC_MANIFEST.validate_output(output)
    result = ToolCallResult(
        HUNTER_DIAGNOSTIC_MANIFEST.tool_id, ToolCallStatus.SUCCEEDED, output,
        tool_call_id="tcl_01K2C5R6J7ABCDEFGHJKMNPQRS",
    )
    assert result.output == {"provider_ref": "hunter-sample:test-v1", "status": "validation_passed"}
    assert handler.summary is not None
    assert (handler.summary.email_count, handler.summary.named_contact_count, handler.summary.source_count) == (2, 1, 2)
    assert transport.calls[0][:2] == ("/domain-search", (("domain", "hunter.io"), ("limit", "10"), ("offset", "0")))
    assert resolver.refs == ["SYNTHETIC_HUNTER_KEY"] and authorize.calls == 3
    for value in (repr(handler), repr(prepared), repr(handler.summary), repr(output)):
        assert "ada@" not in value and "support@" not in value and "hunter-domain-secret-canary" not in value


async def test_no_result_is_success_with_zero_counts_not_a_validated_contact():
    handler, _, _, _ = setup(_response([]))
    prepared = await handler.prepare(context(), None)
    output = await handler.execute(TenantId("control"), prepared)
    HUNTER_DIAGNOSTIC_MANIFEST.validate_output(output)
    assert handler.summary.email_count == handler.summary.source_count == 0
    assert set(output) == {"provider_ref", "status"}


async def test_counts_are_retained_typed_candidates_not_raw_provider_total():
    handler, _, _, _ = setup(_response([_email(f"person{i}@hunter.io") for i in range(10)], results=99))
    prepared = await handler.prepare(context(), None)
    await handler.execute(TenantId("control"), prepared)
    assert handler.summary.email_count == 5  # existing connector retains at most five valid candidates


@pytest.mark.parametrize("updates", [
    {"tenant_id": TenantId("other")}, {"user_id": UserId("other")},
    {"tool_id": "contact.enrich"},
    {"params": {"configuration_version": "test-v1", "domain": "other.example"}},
    {"params": {"configuration_version": "test-v1", "limit": 100}},
    {"params": {"configuration_version": "test-v2"}},
    {"idempotency_key": IdempotencyKey("hunter-sample:another")},
])
async def test_wrong_scope_cannot_resolve_credentials_or_request(updates):
    handler, transport, resolver, authorize = setup()
    with pytest.raises(ValidationError):
        await handler.prepare(context(**updates), None)
    assert not transport.calls and not resolver.refs and authorize.calls == 0


async def test_revocation_between_prepare_and_execute_never_resolves_key():
    handler, transport, resolver, authorize = setup()
    prepared = await handler.prepare(context(), None)
    authorize.allowed = False
    with pytest.raises(PermissionDenied):
        await handler.execute(TenantId("control"), prepared)
    authorize.allowed = True
    with pytest.raises(ValidationError):
        await handler.execute(TenantId("control"), prepared)
    assert not resolver.refs and not transport.calls and handler.summary is None


async def test_forged_prepared_or_foreign_execution_is_rejected():
    handler, transport, resolver, _ = setup()
    prepared = await handler.prepare(context(), None)
    with pytest.raises(ValidationError):
        await handler.execute(TenantId("other"), prepared)
    with pytest.raises(ValidationError):
        await handler.execute(TenantId("control"), replace(prepared))
    assert not resolver.refs and not transport.calls
    await handler.execute(TenantId("control"), prepared)
    with pytest.raises(ValidationError):
        await handler.execute(TenantId("control"), prepared)
    with pytest.raises(ValidationError):
        await handler.prepare(context(), None)
    assert len(transport.calls) == 1


async def test_concurrent_execute_has_only_one_provider_request():
    handler, transport, _, authorize = setup()
    prepared = await handler.prepare(context(), None)
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed_authorization():
        await authorize()
        entered.set()
        await release.wait()

    handler._authorize = delayed_authorization
    task = asyncio.create_task(handler.execute(TenantId("control"), prepared))
    await entered.wait()
    with pytest.raises(ValidationError):
        await handler.execute(TenantId("control"), prepared)
    release.set()
    await task
    assert len(transport.calls) == 1


@pytest.mark.parametrize(("response", "category", "retry_after"), [
    (HunterHttpStatusError(401, HunterErrorCode.OTHER), ToolErrorCategory.PROVIDER_AUTH_REQUIRED, None),
    (HunterHttpStatusError(429, HunterErrorCode.OTHER, 30), ToolErrorCategory.RATE_LIMITED, 30),
    (HunterHttpStatusError(404, HunterErrorCode.OTHER), ToolErrorCategory.PROVIDER_PERMANENT, None),
    (HunterHttpStatusError(503, HunterErrorCode.OTHER), ToolErrorCategory.PROVIDER_TRANSIENT, None),
    (HunterNetworkError(False), ToolErrorCategory.PROVIDER_TRANSIENT, None),
    (HunterNetworkError(True), ToolErrorCategory.RECONCILIATION_REQUIRED, None),
])
async def test_provider_failures_are_fixed_categories_without_retry(response, category, retry_after):
    handler, transport, _, _ = setup(response)
    prepared = await handler.prepare(context(), None)
    with pytest.raises(ToolGatewayError) as caught:
        await handler.execute(TenantId("control"), prepared)
    assert caught.value.category is category and caught.value.retry_after_seconds == retry_after
    assert handler.summary is None and len(transport.calls) == 1
    with pytest.raises(ValidationError):
        await handler.execute(TenantId("control"), prepared)
    assert len(transport.calls) == 1
