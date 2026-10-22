"""余额不能混淆总额度、短时限流、缺失值和旧凭证状态。"""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from connectors.hunter.transport import (
    HunterErrorCode,
    HunterHttpStatusError,
    HunterNetworkError,
)
from connectors.hunter.usage import HunterUsageConnector, parse_hunter_usage
from domains.organization.data_source_usage import usage_view
from tests.unit.test_hunter_contact_enrichment import _Resolver, _Transport

NOW = datetime.now(UTC)


def observation(**requests):
    return parse_hunter_usage({"data": {"requests": requests, "email": "private-canary@example.test", "plan_name": "private-plan", "reset_date": "2026-11-01"}}, NOW)


def test_remaining_not_total_and_missing_is_unknown():
    item = observation(searches={"used": 4, "available": 100, "remaining": 7})
    assert item.counters[0].remaining == 7
    assert usage_view(item).status == "low"
    assert "private-canary" not in item.model_dump_json()
    assert "private-plan" not in item.model_dump_json()
    unknown = observation(searches={"used": 100, "available": 100})
    assert unknown.counters[0].remaining is None
    assert usage_view(unknown).status == "unknown"


def test_partial_exhaustion_and_unified_credit_pool_are_distinct():
    assert usage_view(observation(searches={"remaining": 0}, verifications={"remaining": 25})).status == "partial_exhausted"
    assert usage_view(observation(searches={"remaining": 0}, verifications={"remaining": 0})).status == "exhausted"
    assert usage_view(observation(credits={"remaining": "0.5", "available": 100}, searches={"remaining": 0})).status == "low"
    assert usage_view(observation(credits={"remaining": 0}, searches={"remaining": 100})).status == "exhausted"
    assert usage_view(observation(credits={"remaining": "12.5", "available": 100})).counters[0].remaining == Decimal("12.5")


@pytest.mark.parametrize("number", [True, -1, "NaN", "Infinity", "99999999999999", {}, None])
def test_bad_values_are_unknown_never_zero(number):
    assert usage_view(observation(searches={"remaining": number})).status == "unknown"


def test_timestamp_and_staleness():
    assert usage_view(None).status == "not_checked"
    view = usage_view(observation(searches={"remaining": 10}), now=NOW + timedelta(minutes=11))
    assert view.stale and str(view.reset_date) == "2026-11-01"


@pytest.mark.parametrize(("error", "status"), [
    (HunterHttpStatusError(401, HunterErrorCode.OTHER), "credential_invalid"),
    (HunterHttpStatusError(402, HunterErrorCode.OTHER), "payment_required"),
    (HunterHttpStatusError(429, HunterErrorCode.OTHER, 300), "rate_limited"),
    (HunterHttpStatusError(503, HunterErrorCode.OTHER), "unavailable"),
    (HunterNetworkError(True), "unavailable"),
])
async def test_free_probe_does_not_retry_or_confuse_rate_limit_and_empty_balance(error, status):
    transport = _Transport([error])
    result = await HunterUsageConnector(transport, _Resolver()).read()
    assert result.outcome == status and result.counters == ()
    assert len(transport.calls) == 1
    assert transport.calls[0][:2] == ("/account", ())


def composed(tmp_path, monkeypatch, *, fail_executing=False, fail_succeeded=False, transport=None):
    from apps.api import platform_data_source_usage as module
    from connectors.hunter.transport import HunterHttpResponse
    from domains.organization.platform_access import PlatformAccessService
    from tests.unit.test_platform_access import CONTROL, Repository
    from tests.unit.test_platform_data_sources import repository, update
    from tests.unit.test_tool_gateway_pipeline import _Ledger, _Uow
    from tool_gateway.fingerprint import HmacFingerprintProvider

    trace, grants = [], Repository()
    ledger = _Ledger(trace)
    monkeypatch.setattr(module, "SqlAlchemyToolGatewayUnitOfWork", lambda *args: _Uow(ledger, fail_executing_commit=fail_executing, fail_succeeded_commit=fail_succeeded))
    credentials = repository(tmp_path)
    state = credentials.save_credentials(CONTROL, "hunter", "actor", update())
    access = PlatformAccessService(CONTROL, grants, {})
    transport = transport or _Transport([HunterHttpResponse(200, {"data": {"requests": {"searches": {"remaining": 0, "available": 50}, "verifications": {"remaining": 3, "available": 100}}}})])
    usage = module.PlatformDataSourceUsage(CONTROL, access, credentials, tmp_path / "data-sources", object(), HmacFingerprintProvider("test-v1", b"x" * 32), transport=transport)
    return usage, credentials, state, grants, access, transport, trace


async def test_composed_gateway_persists_snapshot_and_caches_without_touching_test_result(tmp_path, monkeypatch):
    from domains.organization.data_sources import DataSourceAdministrationService
    from domains.organization.schemas import DataSourceUsageCheck
    from tests.unit.test_platform_access import CONTROL, PRINCIPAL
    from tests.unit.test_platform_data_sources import SECRET

    usage, credentials, _, _, access, transport, trace = composed(tmp_path, monkeypatch)
    credentials.record_test_result(CONTROL, "hunter", revision=1, actor_id="actor", scope="account", status="passed")
    service = DataSourceAdministrationService(CONTROL, access, credentials, usage)
    view = await service.check_usage(PRINCIPAL, "hunter", DataSourceUsageCheck(expected_revision=1))
    assert view.usage.status == "partial_exhausted" and view.test_status == "passed" and view.last_test_scope == "account"
    assert len(transport.calls) == 1 and "ledger.executing" in trace and "ledger.succeeded" in trace
    assert SECRET not in view.model_dump_json()
    assert (tmp_path / "data-sources/usage-hunter.json").stat().st_mode & 0o777 == 0o600
    await service.check_usage(PRINCIPAL, "hunter", DataSourceUsageCheck(expected_revision=1))
    assert len(transport.calls) == 1


@pytest.mark.parametrize("failure", ["executing", "succeeded", "revoked"])
async def test_denied_or_uncommitted_probe_cannot_publish_usage(tmp_path, monkeypatch, failure):
    from domains.organization.data_sources import DataSourceUnavailable
    from shared.errors import PermissionDenied
    from tests.unit.test_platform_access import CONTROL, PRINCIPAL

    usage, _, state, grants, _, transport, _ = composed(tmp_path, monkeypatch, fail_executing=failure == "executing", fail_succeeded=failure == "succeeded")
    grants.allowed = failure != "revoked"
    with pytest.raises((DataSourceUnavailable, PermissionDenied)):
        await usage.check(PRINCIPAL, "hunter", state)
    assert usage.snapshot(CONTROL, "hunter", 1) is None
    assert len(transport.calls) == (1 if failure == "succeeded" else 0)


async def test_rotation_deletion_and_foreign_tenant_never_reuse_usage(tmp_path, monkeypatch):
    from domains.organization.data_sources import (
        DataSourceConflict,
        DataSourceUnavailable,
    )
    from domains.organization.schemas import DataSourceCredentialsDelete
    from shared.schemas.identifiers import TenantId
    from tests.unit.test_platform_access import CONTROL, PRINCIPAL
    from tests.unit.test_platform_data_sources import update

    usage, credentials, state, _, _, _, _ = composed(tmp_path, monkeypatch)
    await usage.check(PRINCIPAL, "hunter", state)
    credentials.save_credentials(CONTROL, "hunter", "actor", update(1, "replacement-test-only"))
    assert usage.snapshot(CONTROL, "hunter", 2) is None
    with pytest.raises(DataSourceConflict):
        await usage.check(PRINCIPAL, "hunter", state)
    with pytest.raises(DataSourceUnavailable):
        usage.snapshot(TenantId("foreign"), "hunter", 1)
    credentials.delete_credentials(CONTROL, "hunter", "actor", DataSourceCredentialsDelete(expected_revision=2))
    assert usage.snapshot(CONTROL, "hunter", 3) is None


async def test_usage_http_requires_csrf_and_exact_revision(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from domains.organization.data_sources import DataSourceAdministrationService
    from tests.unit.test_platform_access import (
        CONTROL,
        ORIGIN,
        TRUSTED,
        build_app,
        login,
    )

    app, child, _, _, _ = build_app(monkeypatch)
    usage, credentials, _, _, _, transport, _ = composed(tmp_path, monkeypatch)
    child.state.data_sources = DataSourceAdministrationService(CONTROL, child.state.platform_access, credentials, usage)
    async with child.router.lifespan_context(child), AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        path = "/api/platform/data-sources/hunter/usage-check"
        assert (await client.post(path, json={"expected_revision": 1}, headers=TRUSTED)).status_code == 401
        session = await login(client)
        headers = {**TRUSTED, "X-CSRF-Token": session.json()["csrf_token"]}
        assert (await client.post(path, json={"expected_revision": 1}, headers=TRUSTED)).status_code == 403
        assert (await client.post(path, json={"expected_revision": 2}, headers=headers)).status_code == 409
        assert (await client.post(path, json={"expected_revision": 1, "url": "https://bad.test"}, headers=headers)).status_code == 400
        response = await client.post(path, json={"expected_revision": 1}, headers=headers)
        assert response.status_code == 200 and response.json()["usage"]["status"] == "partial_exhausted"
        assert response.headers["cache-control"] == "no-store"
        assert len(transport.calls) == 1
