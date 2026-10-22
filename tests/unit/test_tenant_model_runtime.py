"""公司运行时使用固定版本且不把未知租户回退到其他公司。"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.composition_support.model import TenantModelGenerator
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelGenerationError,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from tests.unit.test_model_catalog import catalog, create, verify
from tests.unit.test_standalone_model_settings import settings


async def test_two_company_generators_resolve_independently_and_preserve_started_run(tmp_path):
    store = catalog(tmp_path)
    a,b = verify(store, create(store)), verify(store, create(store, 'Model B', 'beta'))
    store.bind('platform','admin','company-a',a.id,1,0)
    store.bind('platform','admin','company-b',b.id,1,0)
    base = {**settings(), 'api_control_tenant':'platform', 'model_catalog_directory':store.directory,'codex_job_root':tmp_path/'jobs'}
    seen=[]
    def factory(selected, identity):
        async def generate(received, request):
            seen.append((received.tenant_id, request.model, selected.api_connection_version))
            return ModelResponse(text='{"ok":true}',model=request.model,usage=ModelUsage(input_tokens=1,output_tokens=1,cached_input_tokens=0))
        return SimpleNamespace(generate=generate)
    authority=SimpleNamespace(check=AsyncMock())
    ga=TenantModelGenerator(StandaloneModelSettings.model_validate({**base,'api_business_tenant':'company-a'}),authority,factory)
    gb=TenantModelGenerator(StandaloneModelSettings.model_validate({**base,'api_business_tenant':'company-b'}),authority,factory)
    def identity(company,run='r1'):
        return InvocationIdentity(tenant_id=company,user_id='u1',employee_id='e1',run_id=run,configuration_version='explicit-v1',capability='business_read',sequence=0)
    request=ModelRequest(model='untrusted-request-model',system_prompt='JSON',payload={},max_output_tokens=128)
    ra,rb=await asyncio.gather(ga.generate(identity('company-a'),request),gb.generate(identity('company-b'),request))
    assert (ra.model,rb.model)==('alpha','beta')
    store.bind('platform','admin','company-a',b.id,1,1)
    assert (await ga.generate(identity('company-a'),request)).model=='alpha'
    assert (await ga.generate(identity('company-a','r2'),request)).model=='beta'
    with pytest.raises(ModelGenerationError):
        await ga.generate(identity('company-b'),request)
    before=len(seen)
    authority.check.side_effect=ModelGenerationError('permission')
    with pytest.raises(ModelGenerationError):
        await ga.generate(identity('company-a','r3'),request)
    assert len(seen)==before
