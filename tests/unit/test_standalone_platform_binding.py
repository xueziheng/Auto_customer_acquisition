"""平台连接跟随存储当前端口，但不能借用企业数据库身份。"""

import secrets
from pathlib import Path

import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from apps.api import standalone
from infra.db.platform_access import EnterpriseReaderBinding
from infra.db.session import create_engine_from
from infra.db.tenant_security import tenant_database_role
from infra.standalone.platform_settings import PlatformSettings
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.identifiers import TenantId, new_id
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
    platform_engines: list[AsyncEngine] = []
    bindings: list[tuple[TenantId, tuple[EnterpriseReaderBinding, ...]]] = []

    def platform_factory(
        *, control_tenant: TenantId, engine: AsyncEngine, origin: str,
        readers: tuple[EnterpriseReaderBinding, ...],
    ) -> FastAPI:
        platform_engines.append(engine)
        bindings.append((control_tenant, readers))
        return FastAPI()

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
