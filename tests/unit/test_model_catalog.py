from pathlib import Path

import pytest

from domains.organization.data_sources import DataSourceConflict, DataSourceUnavailable
from domains.organization.model_catalog import ModelCatalogSave
from infra.model_catalog import FileModelCatalog
from shared.schemas.identifiers import TenantId


def catalog(tmp_path: Path):
    return FileModelCatalog(tmp_path / "models", TenantId("platform"))


def create(store, name="Model A", model="alpha"):
    return store.save(TenantId("platform"), "admin", None, ModelCatalogSave(
        expected_revision=0, name=name, provider="compatible",
        base_url="https://api.moonshot.cn/v1", protocol="chat_completions",
        model=model, api_key="synthetic-private-credential",
    ))


def verify(store, item):
    store.model_store(item.id).record_test(TenantId("platform"), item.revision, "passed", None)
    store.record_agent_test(TenantId("platform"), item.id, item.revision, "passed", None)
    return store.get(TenantId("platform"), item.id)


def test_switch_pins_old_run_and_isolates_companies(tmp_path):
    store = catalog(tmp_path)
    a, b = verify(store, create(store)), verify(store, create(store, "Model B", "beta"))
    store.bind(TenantId("platform"), "admin", "company-a", a.id, a.revision, 0)
    store.bind(TenantId("platform"), "admin", "company-b", b.id, b.revision, 0)
    old = store.for_run("company-a", "run-1", "employee-a", "policy-1")
    store.bind(TenantId("platform"), "admin", "company-a", b.id, b.revision, 1)
    reopened = catalog(tmp_path)
    assert reopened.for_run("company-a", "run-1", "employee-a", "policy-1") == old
    assert reopened.for_run("company-a", "run-2", "employee-a", "policy-1").model == "beta"
    assert reopened.for_run("company-b", "run-1", "employee-b", "policy-1").model == "beta"
    with pytest.raises(DataSourceUnavailable):
        reopened.for_run("unknown-company", "run-1", "employee-a", "policy-1")
    with pytest.raises(DataSourceUnavailable):
        reopened.for_run("company-a", "run-1", "employee-b", "policy-1")


def test_revision_and_delete_protection_and_secret_projection(tmp_path):
    store = catalog(tmp_path)
    a = verify(store, create(store))
    store.bind(TenantId("platform"), "admin", "company-a", a.id, a.revision, 0)
    with pytest.raises(DataSourceConflict):
        store.bind(TenantId("platform"), "admin", "company-a", a.id, a.revision, 0)
    with pytest.raises(DataSourceConflict):
        store.delete(TenantId("platform"), "admin", a.id, a.revision)
    public = store.get(TenantId("platform"), a.id).model_dump_json()
    assert "synthetic-private-credential" not in public
    assert "key.json" not in public
    with pytest.raises(DataSourceUnavailable):
        store.get(TenantId("wrong-platform"), a.id)


def test_edit_does_not_replace_running_binding_or_inherit_agent_test(tmp_path):
    store = catalog(tmp_path)
    a = verify(store, create(store))
    store.bind(TenantId("platform"), "admin", "company-a", a.id, a.revision, 0)
    changed = store.save(TenantId("platform"), "admin", a.id, ModelCatalogSave(
        expected_revision=a.revision, name="Renamed", provider="compatible",
        base_url=a.base_url, protocol=a.protocol, model="alpha-2",
    ))
    assert changed.agent_test_status == "not_run"
    assert store.for_run("company-a", "run-1", "actor", "policy").model == "alpha"
    with pytest.raises(DataSourceConflict):
        store.bind(TenantId("platform"), "admin", "company-b", a.id, changed.revision, 0)
