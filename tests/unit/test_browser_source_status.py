"""UNGM 登录状态独立于额度，真实 Gateway 成功后才交付检查结果。"""
from datetime import UTC, datetime, timedelta

import pytest

from domains.organization.schemas import DataSourceView
from shared.schemas.discovery_sources import get_discovery_source


def test_source_contract_includes_independent_browser_status():
    source = DataSourceView(definition=get_discovery_source("ungm"), can_edit=False)
    assert "browser" in source.model_dump()
    assert source.browser is None


def test_old_success_is_stale_not_expired():
    from domains.organization.data_source_browser import browser_view
    from shared.schemas.browser_probe import BrowserProbeObservation

    now = datetime.now(UTC)
    passed = BrowserProbeObservation(outcome="available", checked_at=now)
    assert browser_view(passed, now=now).status == "available"
    assert browser_view(passed, now=now + timedelta(days=2)).status == "stale"
    failed = BrowserProbeObservation(outcome="login_required", checked_at=now)
    assert browser_view(failed, now=now).status == "login_required"


def composed(tmp_path, monkeypatch, *, fail_executing=False, fail_succeeded=False):
    from apps.api import platform_browser_probe as module
    from domains.organization.platform_access import PlatformAccessService
    from shared.schemas.browser_probe import BrowserProbeObservation
    from tests.unit.test_platform_access import CONTROL, Repository
    from tests.unit.test_platform_data_sources import repository
    from tests.unit.test_tool_gateway_pipeline import _Ledger, _Uow
    from tool_gateway.fingerprint import HmacFingerprintProvider

    trace, grants = [], Repository()
    ledger = _Ledger(trace)
    monkeypatch.setattr(module, "SqlAlchemyToolGatewayUnitOfWork", lambda *args: _Uow(ledger, fail_executing_commit=fail_executing, fail_succeeded_commit=fail_succeeded))
    access = PlatformAccessService(CONTROL, grants, {})
    class Connector:
        calls = 0
        async def check(self):
            self.calls += 1
            assert "ledger.executing" in trace
            return BrowserProbeObservation(outcome="available", checked_at=datetime.now(UTC))
    connector = Connector()
    probe = module.PlatformBrowserProbe(CONTROL, access, tmp_path / "data-sources", object(), HmacFingerprintProvider("test-v1", b"x" * 32), connector=connector)
    return probe, access, repository(tmp_path), grants, connector, trace


async def test_page_read_is_local_and_manual_check_persists_and_caches(tmp_path, monkeypatch):
    from domains.organization.data_sources import DataSourceAdministrationService
    from tests.unit.test_platform_access import CONTROL, PRINCIPAL

    probe, access, store, _, connector, trace = composed(tmp_path, monkeypatch)
    service = DataSourceAdministrationService(CONTROL, access, store, browser=probe)
    view = await service.list_sources(PRINCIPAL)
    ungm = next(s for s in view.sources if s.definition.source_id == "ungm")
    assert ungm.status_kind == "session" and ungm.browser.status == "not_checked"
    assert connector.calls == 0
    checked = await service.check_browser(PRINCIPAL, "ungm")
    assert checked.browser.status == "available" and checked.browser.checked_at
    assert checked.test_status == "not_run"  # 浏览器验证不冒充 API 验证
    assert "ledger.succeeded" in trace
    await service.check_browser(PRINCIPAL, "ungm")
    assert connector.calls == 1
    assert (tmp_path / "data-sources/browser-ungm.json").stat().st_mode & 0o777 == 0o600
    assert probe.snapshot(CONTROL).outcome == "available"


@pytest.mark.parametrize("failure", ["executing", "succeeded", "revoked"])
async def test_denied_or_uncommitted_check_cannot_publish_success(tmp_path, monkeypatch, failure):
    from domains.organization.data_sources import DataSourceAdministrationService
    from shared.errors import PermissionDenied
    from tests.unit.test_platform_access import CONTROL, PRINCIPAL

    probe, access, store, grants, connector, _ = composed(tmp_path, monkeypatch, fail_executing=failure == "executing", fail_succeeded=failure == "succeeded")
    service = DataSourceAdministrationService(CONTROL, access, store, browser=probe)
    if failure == "revoked":
        grants.allowed = False
        with pytest.raises(PermissionDenied):
            await service.check_browser(PRINCIPAL, "ungm")
        assert connector.calls == 0
    else:
        view = await service.check_browser(PRINCIPAL, "ungm")
        assert view.browser.status == "unavailable"
        assert connector.calls == (0 if failure == "executing" else 1)
    assert probe.snapshot(CONTROL) is None or probe.snapshot(CONTROL).outcome != "available"


async def test_browser_route_auth_csrf_and_fixed_source(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    from domains.organization.data_sources import DataSourceAdministrationService
    from tests.unit.test_platform_access import (
        CONTROL,
        ORIGIN,
        TRUSTED,
        build_app,
        login,
    )

    probe, _, store, _, connector, _ = composed(tmp_path, monkeypatch)
    app, child, _, _, _ = build_app(monkeypatch)
    child.state.data_sources = DataSourceAdministrationService(CONTROL, child.state.platform_access, store, browser=probe)
    path = "/api/platform/data-sources/ungm/browser-check"
    async with child.router.lifespan_context(child), AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        assert (await client.post(path, json={}, headers=TRUSTED)).status_code == 401
        session = await login(client)
        headers = {**TRUSTED, "X-CSRF-Token": session.json()["csrf_token"]}
        assert (await client.post(path, json={}, headers=TRUSTED)).status_code == 403
        assert (await client.post(path, json={"url": "http://private.invalid"}, headers=headers)).status_code == 400
        assert (await client.post(path.replace("ungm", "hunter"), json={}, headers=headers)).status_code == 400
        result = await client.post(path, json={}, headers=headers)
        assert result.status_code == 200 and result.json()["browser"]["status"] == "available"
        assert connector.calls == 1

@pytest.mark.parametrize(("patch", "outcome"), [
    ({}, "available"),
    ({"http_status": 403}, "blocked"),
    ({"http_status": 429}, "rate_limited"),
    ({"authenticated": False, "login_required": True}, "login_required"),
    ({"authenticated": False}, "unavailable"),
    ({"error_kind": "TimeoutError", "login_required": True}, "unavailable"),
    ({"http_status": None, "login_required": True}, "unavailable"),
    ({"authenticated": "true"}, "unavailable"),
    ({"restored": False}, "unavailable"),
])
def test_only_proven_login_redirect_means_login_required(patch, outcome):
    from connectors.playwright.login_probe import parse_login_probe
    payload = {"environment": "aliyun-server", "http_status": 200, "authenticated": True, "restored": True, "saved": True, **patch}
    assert parse_login_probe(payload) == outcome


def test_persisted_status_rejects_other_tenant_and_corruption(tmp_path):
    from domains.organization.data_sources import DataSourceUnavailable
    from infra.browser_probe import FileBrowserProbeRepository
    from shared.schemas.browser_probe import BrowserProbeObservation
    from shared.schemas.identifiers import TenantId
    from tests.unit.test_platform_access import CONTROL
    store = FileBrowserProbeRepository(tmp_path / "data-sources", CONTROL)
    store.record(CONTROL, "actor", "test-call", BrowserProbeObservation(outcome="available", checked_at=datetime.now(UTC)))
    with pytest.raises(DataSourceUnavailable):
        store.read(TenantId("other"))
    (tmp_path / "data-sources/browser-ungm.json").write_text("private-corruption-canary")
    with pytest.raises(DataSourceUnavailable) as caught:
        store.read(CONTROL)
    assert "canary" not in str(caught.value)


def test_enablement_requires_private_matching_deployment_binding(tmp_path):
    import json

    from infra.browser_probe import ungm_probe_enabled
    from tests.unit.test_platform_access import CONTROL
    tmp_path.chmod(0o700)
    assert not ungm_probe_enabled(tmp_path, CONTROL)
    path = tmp_path / "ungm-browser-binding.json"
    path.write_text(json.dumps({"tenant_id": CONTROL, "profile": "ungm-1287509", "enabled": True}))
    path.chmod(0o600)
    assert ungm_probe_enabled(tmp_path, CONTROL)
    path.chmod(0o644)
    assert not ungm_probe_enabled(tmp_path, CONTROL)


async def test_api_without_key_is_still_api_not_a_public_website(tmp_path):
    from domains.organization.data_sources import DataSourceAdministrationService
    from domains.organization.platform_access import PlatformAccessService
    from tests.unit.test_platform_access import CONTROL, PRINCIPAL, Repository
    from tests.unit.test_platform_data_sources import repository
    service = DataSourceAdministrationService(CONTROL, PlatformAccessService(CONTROL, Repository(), {}), repository(tmp_path))
    sources = {s.definition.source_id: s for s in (await service.list_sources(PRINCIPAL)).sources}
    assert sources["ted"].status_kind == "api"
    assert sources["volza"].status_kind == "api"
    assert sources["ungm"].status_kind == "session"
