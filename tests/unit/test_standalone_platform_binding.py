"""平台连接跟随存储当前端口，但不能借用企业数据库身份。"""

import secrets
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from apps.api import standalone
from apps.api.composition.runtime import build_phase1_dependencies
from apps.api.runtime_config import Phase1RuntimeSettings
from infra.db.platform_access import EnterpriseReaderBinding
from infra.db.session import create_engine_from
from infra.db.tenant_security import tenant_database_role
from infra.secrets import EnvironmentSecretResolver
from infra.standalone.platform_settings import PlatformSettings
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.identifiers import TenantId, new_id
from tests.integration.test_api_runtime import _runtime_env
from tests.unit.test_pilot_profile import make_config
from tests.unit.test_standalone_model_settings import settings


@pytest.mark.parametrize("stored_port,current_port", [(44551, 44552), (44552, 44552)])
async def test_platform_uses_current_storage_port_with_independent_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stored_port: int, current_port: int,
) -> None:
    base = make_config(tmp_path)
    profile = base.model_copy(update={
        "database_port": current_port,
        "object_port": 44553,
        "database_username": tenant_database_role(base.tenant_id),
        "database_runtime_password": SecretStr(secrets.token_urlsafe(32)),
    })
    control_tenant = TenantId(new_id("tn"))
    configured = PlatformSettings(
        control_tenant_id=control_tenant,
        database_port=stored_port,
        database_username=tenant_database_role(control_tenant),
        database_password=SecretStr(secrets.token_urlsafe(32)),
    )
    business = FastAPI()
    business_engine = create_engine_from(profile.database_url.get_secret_value())
    business.state.runtime_engine = business_engine
    runtime_env = _runtime_env(profile.database_url.get_secret_value())
    runtime_env["TRADEOS_TENANT_ID"] = str(profile.tenant_id)
    business.state.dependencies = build_phase1_dependencies(
        Phase1RuntimeSettings.from_environ(runtime_env),
        async_sessionmaker(business_engine, expire_on_commit=False), now=lambda: datetime.now(UTC), secret_resolver=EnvironmentSecretResolver(runtime_env),
    )
    platform_engines: list[AsyncEngine] = []
    bindings: list[tuple[TenantId, tuple[EnterpriseReaderBinding, ...]]] = []

    def platform_factory(
        *, control_tenant: TenantId, engine: AsyncEngine, origin: str,
        readers: tuple[EnterpriseReaderBinding, ...],
        data_sources_directory: Path | None = None,
        model_api_directory: Path | None = None,
        model_catalog_settings: StandaloneModelSettings | None = None,
        data_sources_fingerprints: object | None = None,
        ungm_browser_probe: bool = False,
    ) -> FastAPI:
        platform_engines.append(engine)
        bindings.append((control_tenant, readers))
        result = FastAPI()
        result.state.runtime_engine = engine
        return result

    monkeypatch.setattr(
        standalone, "create_runtime_app_from_settings", lambda *args, **kwargs: business,
    )
    monkeypatch.setattr(standalone, "create_platform_app", platform_factory)
    (tmp_path / "index.html").write_text("<html>TradeOS</html>")
    try:
        standalone.create_standalone_app(
            profile, StandaloneModelSettings.model_validate(settings()), tmp_path,
            platform_settings=configured,
        )
        assert len(platform_engines) == 1
        connection = platform_engines[0].url
        assert connection.host == "127.0.0.1"
        assert connection.port == current_port
        assert connection.database == "pilot"
        assert connection.username == configured.database_username
        assert connection.username != profile.database_username
        assert connection.password == configured.database_password.get_secret_value()
        assert connection.password != business_engine.url.password
        assert bindings == [
            (control_tenant, (EnterpriseReaderBinding(
                tenant_id=TenantId(profile.tenant_id), engine=business_engine,
            ),)),
        ]
        assert configured.database_port == stored_port
    finally:
        for engine in (*platform_engines, business_engine):
            await engine.dispose()

@pytest.mark.parametrize("reply_enabled,send_enabled", [(False, False), (True, False), (True, True)])
def test_api_and_scheduler_agree_on_explicit_automatic_followup_binding(tmp_path, monkeypatch, reply_enabled, send_enabled):
    from apps.scheduler_worker.standalone import _reply_ports
    from infra.pilot.config import PilotGmailConfig
    base = make_config(tmp_path).model_copy(update={"object_port": 44553})
    profile = base.model_copy(update={"gmail": PilotGmailConfig(address="controlled@example.test", credentials_file=tmp_path / "unread-test-credentials.json", employee_id=new_id("emp"))})
    configured = settings()
    configured["reply_enabled"] = reply_enabled
    configured["automatic_send_enabled"] = send_enabled
    configured["limits"]["max_output_tokens"] = 128
    model = StandaloneModelSettings.model_validate(configured)
    business = FastAPI()
    business.state.runtime_engine = object()
    captured = {}
    def runtime_factory(*args, **kwargs):
        captured.update(kwargs)
        return business
    monkeypatch.setattr(standalone, "create_runtime_app_from_settings", runtime_factory)
    monkeypatch.setattr(standalone, "mount_web", lambda business, *args, **kwargs: business)
    standalone.create_standalone_app(profile, model, tmp_path)
    ports = _reply_ports(profile, model, None)
    assert captured["automatic_followup_enabled"] is send_enabled
    assert (ports is not None) is reply_enabled
    assert captured["gmail_transport"] is not None  # 手动发件组存在不能代替自动配置。


def test_enabled_reply_without_gmail_binding_fails_both_standalone_compositions(tmp_path, monkeypatch):
    from apps.scheduler_worker.standalone import _reply_ports
    base = make_config(tmp_path).model_copy(update={"object_port": 44553})
    configured = settings()
    configured["reply_enabled"] = True
    configured["limits"]["max_output_tokens"] = 128
    model = StandaloneModelSettings.model_validate(configured)
    business = FastAPI()
    business.state.runtime_engine = object()
    monkeypatch.setattr(standalone, "create_runtime_app_from_settings", lambda *args, **kwargs: business)
    monkeypatch.setattr(standalone, "mount_web", lambda business, *args, **kwargs: business)
    with pytest.raises(ValueError, match="Gmail.*员工绑定"):
        standalone.create_standalone_app(base, model, tmp_path)
    with pytest.raises(ValueError, match="Gmail.*员工绑定"):
        _reply_ports(base, model, None)
