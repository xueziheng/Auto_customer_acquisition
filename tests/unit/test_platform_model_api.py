"""模型设置的凭证隔离、版本绑定、协议与 HTTP 授权回归。"""

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError

from domains.organization.data_sources import (
    DataSourceConflict,
    DataSourceInputInvalid,
    DataSourceUnavailable,
)
from domains.organization.model_api import ModelApiCommand, ModelApiUpdate
from infra.model_api import FileModelApiRepository
from tests.unit.test_platform_access import CONTROL, ORIGIN, TRUSTED, build_app, login

SECRET = "synthetic-model-key-only"


def command(revision=0, **kw):
    return ModelApiUpdate(expected_revision=revision, provider="deepseek",
        base_url="https://api.deepseek.com", protocol="responses", model="deepseek-flash",
        api_key=SecretStr(SECRET), **kw)


def test_keys_are_write_only_and_cannot_follow_an_endpoint_change(tmp_path):
    store = FileModelApiRepository(tmp_path / "models", CONTROL)
    one = store.save(CONTROL, "user", command())
    assert SECRET not in one.model_dump_json()
    assert store.directory.stat().st_mode & 0o777 == 0o700
    assert store.key_path(one.configuration_version).stat().st_mode & 0o777 == 0o600
    assert store.view(CONTROL).test_status == "not_run"
    changed = command(1).model_copy(update={"provider": "compatible", "base_url": "https://api.moonshot.cn/v1", "protocol": "chat_completions", "api_key": None})
    with pytest.raises(DataSourceInputInvalid):
        store.save(CONTROL, "user", changed)
    with pytest.raises(DataSourceConflict):
        store.save(CONTROL, "user", command())
    with pytest.raises(DataSourceUnavailable):
        store.view("foreign-tenant")


def test_stale_probe_cannot_validate_replacement_or_activate_untested_config(tmp_path):
    store = FileModelApiRepository(tmp_path / "models", CONTROL)
    first = store.save(CONTROL, "user", command())
    with pytest.raises(DataSourceInputInvalid):
        store.request_apply(CONTROL, "user", ModelApiCommand(expected_revision=1))
    store.record_test(CONTROL, 1, "passed", None)
    store.save(CONTROL, "user", command(1))
    with pytest.raises(DataSourceConflict):
        store.record_test(CONTROL, first.revision, "passed", None)
    assert store.view(CONTROL).test_status == "not_run"
    store.record_test(CONTROL, 2, "passed", None)
    ready = store.request_apply(CONTROL, "user", ModelApiCommand(expected_revision=2))
    assert ready.apply_status == "pending"
    with pytest.raises(DataSourceConflict):
        store.save(CONTROL, "user", command(2))


@pytest.mark.parametrize("url", ["http://api.deepseek.com", "https://127.0.0.1/v1", "https://api.deepseek.com.evil.test", "https://api.deepseek.com/?token=secret", "https://user:pass@api.deepseek.com"])
def test_only_declared_provider_origins_can_receive_keys(url):
    with pytest.raises(ValidationError):
        ModelApiUpdate.model_validate({**command().model_dump(), "base_url": url})


async def test_model_routes_require_platform_auth_csrf_and_never_echo_bad_keys(monkeypatch, tmp_path):
    app, child, repo, _auth, _ = build_app(monkeypatch)
    from apps.api.platform_model_api import PlatformModelApi
    store = FileModelApiRepository(tmp_path / "models", CONTROL)
    child.state.model_api = PlatformModelApi(CONTROL, child.state.platform_access, store)
    async with child.router.lifespan_context(child):  # noqa: SIM117
        async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
            assert (await client.get("/api/platform/model-api")).status_code == 401
            logged = await login(client)
            headers = {**TRUSTED, "X-CSRF-Token": logged.json()["csrf_token"]}
            payload = {**command().model_dump(mode="json"), "api_key": SECRET}
            assert (await client.put("/api/platform/model-api", headers=TRUSTED, json=payload)).status_code == 403
            bad = await client.put("/api/platform/model-api", headers=headers, json={**payload, "api_key": {"value": SECRET}})
            assert bad.status_code == 400 and SECRET not in bad.text
            saved = await client.put("/api/platform/model-api", headers=headers, json=payload)
            assert saved.status_code == 200 and SECRET not in saved.text
            repo.allowed = False
            assert (await client.get("/api/platform/model-api")).status_code in (401, 403)


async def test_probe_accepts_canonical_uppercase_model_versions_without_reading_keys():
    from shared.schemas.identifiers import new_id
    from tool_gateway.fingerprint import HmacFingerprintProvider
    from tool_gateway.handlers.model_connection_probe import (
        MODEL_CONNECTION_MANIFEST,
        ModelConnectionProbe,
    )
    from tool_gateway.pipeline import ToolCallContext
    version = new_id("mcv")
    async def authorize():
        return None
    def provider():
        pytest.fail("prepare 不得创建 Provider")
    handler = ModelConnectionProbe(CONTROL, "actor", version, "probe-once", authorize, provider, "deepseek-flash", HmacFingerprintProvider("test-v1", b"t" * 32))
    prepared = await handler.prepare(ToolCallContext(CONTROL, "actor", MODEL_CONNECTION_MANIFEST.tool_id, {"configuration_version": version}, idempotency_key="probe-once"), None)
    assert version in prepared.audit_projection.values()
