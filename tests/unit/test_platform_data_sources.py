"""平台数据源凭证：真实私有文件、授权、CSRF、轮换和删除。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from domains.organization.data_sources import (
    DataSourceAdministrationService,
    DataSourceConflict,
    DataSourceInputInvalid,
    DataSourceUnavailable,
)
from domains.organization.platform_access import PlatformAccessService
from domains.organization.schemas import (
    DataSourceCredentialsDelete,
    DataSourceCredentialsUpdate,
)
from infra.data_source_credentials import FileDataSourceConfigurationRepository
from shared.errors import PermissionDenied
from shared.schemas.identifiers import TenantId
from tests.unit.test_platform_access import (
    CONTROL,
    ORIGIN,
    PRINCIPAL,
    TRUSTED,
    Repository,
    build_app,
    login,
)

SECRET = "synthetic-hunter-secret-for-test-only"


def repository(tmp_path: Path) -> FileDataSourceConfigurationRepository:
    tmp_path.chmod(0o700)
    return FileDataSourceConfigurationRepository(tmp_path / "data-sources", CONTROL)


def update(revision: int = 0, secret: str = SECRET) -> DataSourceCredentialsUpdate:
    return DataSourceCredentialsUpdate(expected_revision=revision, credentials={"api_key": secret})


def test_private_atomic_save_rotation_clear_and_stale_results(tmp_path):
    store = repository(tmp_path)
    assert store.list_configurations(CONTROL) == {}
    first = store.save_credentials(CONTROL, "hunter", "actor", update())
    assert first.configured and first.revision == 1 and first.test_status == "not_run"
    root = tmp_path / "data-sources"
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / "configuration.json").stat().st_mode & 0o777 == 0o600
    assert SECRET not in first.model_dump_json() and "credentials" not in first.model_dump()
    assert store.resolve_credential(CONTROL, "hunter", "api_key", revision=1).get_secret_value() == SECRET
    tested = store.record_test_result(CONTROL, "hunter", revision=1, actor_id="actor", status="passed", scope="account")
    assert tested.test_status == "passed" and tested.tested_at is not None
    assert tested.last_test_scope == "account"
    second = store.save_credentials(CONTROL, "hunter", "actor", update(1, "replacement-test-only"))
    assert second.test_status == "not_run" and second.tested_at is None
    assert second.last_test_scope is None
    assert first.configuration_version != second.configuration_version
    with pytest.raises(DataSourceConflict):
        store.save_credentials(CONTROL, "hunter", "actor", update(1))
    with pytest.raises(DataSourceConflict):
        store.record_test_result(CONTROL, "hunter", revision=1, actor_id="actor", status="passed", scope="account")
    deleted = store.delete_credentials(CONTROL, "hunter", "actor", DataSourceCredentialsDelete(expected_revision=2))
    assert not deleted.configured and deleted.revision == 3 and deleted.test_status == "not_run"
    assert deleted.last_test_scope is None
    private_payload = (root / "configuration.json").read_text()
    assert SECRET not in private_payload and "replacement-test-only" not in private_payload
    assert len(json.loads(private_payload)["audit"]) == 4
    assert all(row["tenant_id"] == CONTROL for row in json.loads(private_payload)["audit"])
    with pytest.raises(DataSourceUnavailable):
        store.resolve_credential(CONTROL, "hunter", "api_key", revision=3)
    with pytest.raises(DataSourceConflict):
        store.record_test_result(CONTROL, "hunter", revision=3, actor_id="actor", status="passed", scope="account")
    assert store.list_configurations(CONTROL)["hunter"] == deleted


@pytest.mark.parametrize("fault", ["tenant", "symlink", "mode", "corrupt"])
def test_storage_fails_closed_without_exposing_private_content(tmp_path, fault):
    store = repository(tmp_path)
    store.save_credentials(CONTROL, "hunter", "actor", update())
    private_file = tmp_path / "data-sources/configuration.json"
    tenant = CONTROL
    if fault == "tenant":
        tenant = TenantId("other")
    elif fault == "symlink":
        private_file.rename(private_file.with_suffix(".private"))
        private_file.symlink_to(private_file.with_suffix(".private"))
    elif fault == "mode":
        private_file.chmod(0o644)
    else:
        private_file.write_text(SECRET)
    with pytest.raises(DataSourceUnavailable) as caught:
        store.list_configurations(tenant)
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("value", ["", " hidden ", "key\nsecret", "x" * 8193, 123])
def test_credentials_input_never_exposes_value_in_validation_error(value):
    with pytest.raises(ValidationError) as caught:
        DataSourceCredentialsUpdate(expected_revision=0, credentials={"api_key": value})
    assert "input_value" not in str(caught.value)


async def test_service_rechecks_grant_and_requires_complete_snov_credentials(tmp_path):
    grants = Repository()
    store = repository(tmp_path)
    access = PlatformAccessService(CONTROL, grants, {})
    service = DataSourceAdministrationService(CONTROL, access, store)
    with pytest.raises(DataSourceInputInvalid):
        await service.save_credentials(PRINCIPAL, "snov", update())
    grants.allowed = False
    with pytest.raises(PermissionDenied):
        await service.save_credentials(PRINCIPAL, "hunter", update())
    assert not (tmp_path / "data-sources").exists()


async def test_platform_http_credential_contract_csrf_and_safe_errors(tmp_path, monkeypatch):
    app, child, grants, _, _ = build_app(monkeypatch)
    store = repository(tmp_path)
    child.state.data_sources = DataSourceAdministrationService(CONTROL, child.state.platform_access, store)
    async with child.router.lifespan_context(child), AsyncClient(
        transport=ASGITransport(app), base_url=ORIGIN,
    ) as client:
        path = "/api/platform/data-sources/hunter/credentials"
        assert (await client.get("/api/platform/data-sources")).status_code == 401
        session = await login(client)
        headers = {**TRUSTED, "X-CSRF-Token": session.json()["csrf_token"]}
        body = {"expected_revision": 0, "credentials": {"api_key": SECRET}}
        assert (await client.put(path, json=body, headers=TRUSTED)).status_code == 403
        response = await client.put(path, json=body, headers=headers)
        assert response.status_code == 200 and response.json()["configured"]
        assert SECRET not in response.text and "api_key\":\"" not in response.text
        assert response.headers["cache-control"] == "no-store"
        assert (await client.put(path, json=body, headers=headers)).status_code == 409
        listed = await client.get("/api/platform/data-sources")
        assert listed.status_code == 200 and SECRET not in listed.text
        invalid = await client.put(path, json={**body, "extra": SECRET}, headers=headers)
        assert invalid.status_code == 400 and SECRET not in invalid.text
        huge = await client.put(path, json={**body, "extra": SECRET * 2000}, headers=headers)
        assert huge.status_code == 413 and SECRET not in huge.text
        foreign = await client.put(path + "?tenant_id=other", json=body, headers=headers)
        assert foreign.status_code == 400
        invalid_source = await client.put(path.replace("hunter", "unknown"), json=body, headers=headers)
        assert invalid_source.status_code == 400
        removed = await client.request("DELETE", path, json={"expected_revision": 1}, headers=headers)
        assert removed.status_code == 200 and not removed.json()["configured"]
        assert removed.json()["revision"] == 2
        assert (await client.request("DELETE", path, json={"expected_revision": 1}, headers=headers)).status_code == 409
        grants.allowed = False
        assert (await client.get("/api/platform/data-sources")).status_code == 401


def test_lock_conflict_cannot_overwrite_credentials(tmp_path):
    from infra.pilot.config import exclusive_profile_lock

    store = repository(tmp_path)
    store.save_credentials(CONTROL, "hunter", "actor", update())
    with exclusive_profile_lock(tmp_path / "data-sources"), pytest.raises(DataSourceUnavailable):
        store.save_credentials(CONTROL, "hunter", "actor", update(1))
    assert store.list_configurations(CONTROL)["hunter"].revision == 1
