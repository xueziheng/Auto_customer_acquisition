"""来源备注独立持久化、权限、并发与真实 HTTP 边界。"""
from __future__ import annotations

import json

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
from domains.organization.schemas import DataSourceNoteUpdate
from infra.data_source_notes import FileDataSourceNotesRepository
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
from tests.unit.test_platform_data_sources import repository, update


def stores(tmp_path):
    credentials = repository(tmp_path)
    return credentials, FileDataSourceNotesRepository(tmp_path / "data-sources", CONTROL)


def test_notes_survive_restart_clear_and_do_not_modify_credentials_or_tests(tmp_path):
    credentials, notes = stores(tmp_path)
    credentials.save_credentials(CONTROL, "hunter", "actor", update())
    credentials.record_test_result(CONTROL, "hunter", revision=1, actor_id="actor", status="passed", scope="account")
    credential_bytes = (tmp_path / "data-sources/configuration.json").read_bytes()
    first = notes.save_note(CONTROL, "hunter", "actor", DataSourceNoteUpdate(expected_revision=0, text="  待统一测试\n参考：9 美元  "))
    assert first.text == "待统一测试\n参考：9 美元" and first.revision == 1
    reopened = FileDataSourceNotesRepository(tmp_path / "data-sources", CONTROL)
    assert reopened.list_notes(CONTROL)["hunter"] == first
    assert first.updated_by == "actor" and first.updated_at is not None
    with pytest.raises(DataSourceConflict):
        notes.save_note(CONTROL, "hunter", "actor", DataSourceNoteUpdate(expected_revision=0, text="过期"))
    cleared = notes.save_note(CONTROL, "hunter", "actor", DataSourceNoteUpdate(expected_revision=1, text=" \n "))
    assert cleared.text == "" and cleared.revision == 2
    assert (tmp_path / "data-sources/configuration.json").read_bytes() == credential_bytes
    file = tmp_path / "data-sources/notes.json"
    assert file.stat().st_mode & 0o777 == 0o600
    payload = json.loads(file.read_text())
    assert all(row["tenant_id"] == CONTROL for row in payload["audit"])
    assert len(payload["audit"]) == 2
    assert "待统一测试" not in repr(first)


@pytest.mark.parametrize("fault", ["tenant", "symlink", "mode", "corrupt", "record"])
def test_notes_fail_closed_and_reject_foreign_tenants(tmp_path, fault):
    _, notes = stores(tmp_path)
    notes.save_note(CONTROL, "ungm", "actor", DataSourceNoteUpdate(expected_revision=0, text="PRIVATE-NOTE-CANARY"))
    file = tmp_path / "data-sources/notes.json"
    tenant = CONTROL
    if fault == "tenant":
        tenant = TenantId("foreign")
    elif fault == "symlink":
        file.rename(file.with_suffix(".other"))
        file.symlink_to(file.with_suffix(".other"))
    elif fault == "mode":
        file.chmod(0o644)
    elif fault == "record":
        data = json.loads(file.read_text())
        data["records"]["../unknown"] = data["records"].pop("ungm")
        file.write_text(json.dumps(data))
    else:
        file.write_text("PRIVATE-NOTE-CANARY")
    with pytest.raises(DataSourceUnavailable) as caught:
        notes.list_notes(tenant)
    assert "PRIVATE-NOTE-CANARY" not in str(caught.value)


@pytest.mark.parametrize("body", [
    {"expected_revision": True, "text": "x"}, {"expected_revision": 0, "text": "x" * 2001},
    {"expected_revision": 0, "text": "private\x00note"}, {"expected_revision": 0, "text": 123},
    {"expected_revision": 0, "text": "x", "tenant_id": "foreign"},
])
def test_note_input_is_bounded_plain_text_without_echoed_errors(body):
    with pytest.raises(ValidationError) as caught:
        DataSourceNoteUpdate.model_validate(body)
    assert "input_value" not in str(caught.value)


async def test_notes_require_current_grant_and_support_public_sources(tmp_path):
    credentials, notes = stores(tmp_path)
    grants = Repository()
    service = DataSourceAdministrationService(CONTROL, PlatformAccessService(CONTROL, grants, {}), credentials, notes=notes)
    assert all(row.can_edit_note for row in (await service.list_sources(PRINCIPAL)).sources)
    await service.save_note(PRINCIPAL, "ungm", DataSourceNoteUpdate(expected_revision=0, text="公开网页，待接入"))
    row = next(row for row in (await service.list_sources(PRINCIPAL)).sources if row.definition.source_id == "ungm")
    assert row.note.text == "公开网页，待接入" and not row.configured and not row.can_edit
    with pytest.raises(DataSourceInputInvalid):
        await service.save_note(PRINCIPAL, "unknown", DataSourceNoteUpdate(expected_revision=0, text="x"))
    grants.allowed = False
    with pytest.raises(PermissionDenied):
        await service.save_note(PRINCIPAL, "ungm", DataSourceNoteUpdate(expected_revision=1, text="blocked"))
    assert notes.list_notes(CONTROL)["ungm"].revision == 1


async def test_notes_http_requires_auth_csrf_and_rejects_stale_or_oversized_writes(tmp_path, monkeypatch):
    app, child, _, _, _ = build_app(monkeypatch)
    credentials, notes = stores(tmp_path)
    child.state.data_sources = DataSourceAdministrationService(CONTROL, child.state.platform_access, credentials, notes=notes)
    async with child.router.lifespan_context(child), AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
        path = "/api/platform/data-sources/importyeti/note"
        body = {"expected_revision": 0, "text": "已注册，待开通"}
        assert (await client.put(path, json=body, headers=TRUSTED)).status_code == 401
        session = await login(client)
        headers = {**TRUSTED, "X-CSRF-Token": session.json()["csrf_token"]}
        assert (await client.put(path, json=body, headers=TRUSTED)).status_code == 403
        response = await client.put(path, json=body, headers=headers)
        assert response.status_code == 200 and response.json()["revision"] == 1
        assert response.headers["cache-control"] == "no-store"
        assert (await client.put(path, json=body, headers=headers)).status_code == 409
        assert (await client.put(path + "?tenant_id=other", json=body, headers=headers)).status_code == 400
        assert (await client.put(path.replace("importyeti", "unknown"), json=body, headers=headers)).status_code == 400
        invalid = await client.put(path, json={"expected_revision": 1, "text": "PRIVATE-NOTE-CANARY" * 200}, headers=headers)
        assert invalid.status_code == 400 and "PRIVATE-NOTE-CANARY" not in invalid.text
        assert (await client.put(path, json={"expected_revision": 1, "text": "x" * 17000}, headers=headers)).status_code == 413
        cleared = await client.put(path, json={"expected_revision": 1, "text": ""}, headers=headers)
        assert cleared.status_code == 200 and cleared.json()["revision"] == 2
        rows = (await client.get("/api/platform/data-sources")).json()["sources"]
        row = next(row for row in rows if row["definition"]["source_id"] == "importyeti")
        assert row["note"]["text"] == "" and row["note"]["revision"] == 2 and not row["configured"]


def test_note_lock_conflicts_cannot_lose_updates(tmp_path):
    from infra.pilot.config import exclusive_profile_lock

    _, notes = stores(tmp_path)
    notes.save_note(CONTROL, "importyeti", "actor", DataSourceNoteUpdate(expected_revision=0, text="original"))
    with exclusive_profile_lock(tmp_path / "data-sources"), pytest.raises(DataSourceUnavailable):
        notes.save_note(CONTROL, "importyeti", "actor", DataSourceNoteUpdate(expected_revision=1, text="blocked"))
    assert notes.list_notes(CONTROL)["importyeti"].text == "original"
