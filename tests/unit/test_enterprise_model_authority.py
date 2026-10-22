"""企业服务不能借老板角色改写平台模型或调用预算。"""
from datetime import UTC, datetime

import pytest

from domains.assistant.schemas import AssistantActor, ModelSettingsUpdate
from domains.assistant.service_impl import ModelConfigurationServiceImpl
from infra.standalone.settings import StandaloneModelSettings
from shared.errors import PermissionDenied
from tests.unit.test_standalone_model_settings import settings


class Repository:
    def __init__(self):
        self.version = 'original'

    async def save(self, actor, update):
        self.version = 'changed'

    async def get(self, tenant):
        return None


class Authority:
    async def require_admin(self, actor):
        if actor.employee_id != 'boss':
            raise PermissionDenied('禁止')


@pytest.mark.parametrize('role', ['boss', 'manager', 'sales'])
async def test_enterprise_roles_cannot_mutate_model_budget_or_export(role):
    repository = Repository()
    service = ModelConfigurationServiceImpl(repository, Authority(), now=lambda: datetime.now(UTC))
    config = StandaloneModelSettings.model_validate(settings())
    actor = AssistantActor(tenant_id='tenant-a', employee_id=role, user_id='user-a')
    update = ModelSettingsUpdate(expected_version='original', model='unauthorized-model',
        limits=config.limits.model_copy(update={'tenant_calls': None}), model_data_export_enabled=True)
    with pytest.raises(PermissionDenied):
        await service.save_nonsecret(actor, update)
    assert repository.version == 'original'


def test_public_summary_uses_current_binding_without_pinning_or_secret_read(tmp_path, monkeypatch):
    from domains.assistant.schemas import ModelSettingsView
    from domains.organization.model_api import ModelApiView
    from infra.model_catalog import FileModelCatalog
    from infra.model_settings_summary import PlatformModelSummary

    config = StandaloneModelSettings.model_validate({**settings(),
        'model_catalog_directory': str(tmp_path), 'api_control_tenant': 'control',
        'api_business_tenant': 'business', 'codex_job_root': str(tmp_path / 'jobs')})
    def bound(self, control, company):
        assert (control, company) == ('control', 'business')
        return ModelApiView(model='current-platform-model', provider='compatible',
            configured=True, configuration_version='platform-v2', test_status='passed')
    monkeypatch.setattr(FileModelCatalog, 'bound_view', bound)
    monkeypatch.setattr(FileModelCatalog, 'for_run', lambda *a, **k: pytest.fail('GET不得pin'))
    view = PlatformModelSummary(config)('business', ModelSettingsView(status='verified', model='stale-sql'))
    assert view.model == 'current-platform-model'
    assert view.platform_connection_version == 'platform-v2'
    assert view.can_edit is False
    assert 'api_key' not in view.model_dump_json()
    wrong = PlatformModelSummary(config)('another', ModelSettingsView(status='verified', model='stale-sql'))
    assert wrong.model is None and not wrong.can_probe

async def test_legacy_http_post_is_403_without_sql_or_repository_mutation():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from apps.api.routers.assistant import _access
    from apps.api.routers.model_settings import _service, router
    repository = Repository()
    service = ModelConfigurationServiceImpl(repository, Authority(), now=lambda:datetime.now(UTC))
    actor = AssistantActor(tenant_id='tenant-a', employee_id='boss', user_id='user-a')
    app = FastAPI()
    from apps.api.middleware import ApiSettings, install_error_handlers
    install_error_handlers(app, ApiSettings(tenant_id="tenant-a", dev_mode=True, retry_after_seconds=1))
    app.include_router(router)
    app.dependency_overrides[_access] = lambda: (actor, None)
    app.dependency_overrides[_service] = lambda: service
    config = StandaloneModelSettings.model_validate(settings())
    async with AsyncClient(transport=ASGITransport(app), base_url='http://test') as client:
        response = await client.post('/settings/model', json={'expected_version': 'original', 'model': 'unauthorized',
            'limits': config.limits.model_dump(mode='json'), 'model_data_export_enabled': True})
    assert response.status_code == 403
    assert repository.version == 'original'
