"""真实路由验证跨企业选择、CSRF、凭证脱敏和租户拒绝。"""
from httpx import ASGITransport, AsyncClient

from apps.api.platform_model_catalog import PlatformModelCatalog
from infra.model_catalog import FileModelCatalog
from tests.unit.test_platform_access import CONTROL, ORIGIN, TRUSTED, build_app, login


async def test_catalog_mutations_require_admin_csrf_and_known_company(monkeypatch, tmp_path):
    app, child, repo, _auth, _ = build_app(monkeypatch)
    store = FileModelCatalog(tmp_path/'models', CONTROL)
    child.state.model_catalog = PlatformModelCatalog(CONTROL, child.state.platform_access, store, None, None)
    async with child.router.lifespan_context(child):  # noqa: SIM117 -- 测试显式保留应用与客户端两层生命周期。
        async with AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client:
            assert (await client.get('/api/platform/model-catalog')).status_code == 401
            session = await login(client)
            headers = {**TRUSTED,'X-CSRF-Token':session.json()['csrf_token']}
            payload = {'name':'A','expected_revision':0,'provider':'deepseek','base_url':'https://api.deepseek.com','protocol':'responses','model':'test-model','api_key':'synthetic-only-secret'}
            assert (await client.post('/api/platform/model-catalog',headers=TRUSTED,json=payload)).status_code == 403
            bad = await client.post('/api/platform/model-catalog',headers=headers,json={**payload,'api_key':{'bad':'synthetic-only-secret'}})
            assert bad.status_code == 400 and 'synthetic-only-secret' not in bad.text
            created = await client.post('/api/platform/model-catalog',headers=headers,json=payload)
            assert created.status_code == 200 and 'synthetic-only-secret' not in created.text
            entry = created.json()
            result = await client.put('/api/platform/company-models/foreign-company',headers=headers,json={'expected_revision':0,'model_id':entry['id'],'model_revision':1})
            assert result.status_code == 503
            assert not store.bindings(CONTROL)
            deleted = await client.post(f"/api/platform/model-catalog/{entry['id']}/delete",headers=headers,json={'expected_revision':1})
            assert deleted.status_code == 200
            repo.allowed = False
            assert (await client.get('/api/platform/model-catalog')).status_code in {401,403}
