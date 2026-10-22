"""正式普通对话的真实配置工厂/CLI JSON边界；仅供应商协议替身，无网络或凭证。"""
import json
import os
from pathlib import Path

import pytest

from shared.schemas.model_invocation import InvocationIdentity, ModelRequest
from tests.unit.test_codex_provider import FakeDelegate
from tests.unit.test_codex_provider import short_jobs as short_jobs  # noqa: PLC0414
from tests.unit.test_dialogue_send_switch import settings


@pytest.mark.skipif(not os.environ.get('TRADEOS_TEST_CODEX_BINARY'), reason='须显式选择Linux原生Codex')
async def test_configured_ordinary_profile_native_cli_keeps_context_and_json_boundary(short_jobs, monkeypatch):
    from apps.composition_support.model import configured_codex_provider
    calls = []
    class Budget:
        async def run(self, body, dispatch, **kwargs):
            calls.append(body)
            assert body['model']=='synthetic'
            return await dispatch(body)
    budget=Budget()
    class Transport(FakeDelegate):
        def __init__(self,*args,**kwargs):
            super().__init__()
            self.budget=kwargs['round_budget']
        async def round(self, body):
            async def dispatch(payload):
                assert all(tool['name']=='mcp__tradeos__read_task_context' for tool in payload.get('tools', []))
                assert 'bounded reply-classification' in payload['instructions']
                if len(calls)==1:
                    output=[{'type':'function_call','id':'f1','call_id':'c1','name':'mcp__tradeos__read_task_context','arguments':'{}','status':'completed'}]
                else:
                    assert len(calls)==2
                    assert 'customer_visible_products' in json.dumps(payload['input'])
                    output=[{'type':'message','id':'m2','role':'assistant','status':'completed','phase':'final_answer',
                             'content':[{'type':'output_text','text':'{"kind":"ordinary","question_topics":["quantity"]}'}]}]
                return {'id':f'r{len(calls)}','status':'completed','model':payload['model'],'output':output,
                        'usage':{'input_tokens':10,'output_tokens':5}}
            return await self.budget.run(body,dispatch)
    monkeypatch.setattr('connectors.codex_agent.client.AgentTransport',Transport)
    configured=settings(True).model_copy(update={'codex_binary':Path(os.environ['TRADEOS_TEST_CODEX_BINARY']),
        'codex_job_root':short_jobs,'api_key_file':short_jobs/'never-read-key','api_control_tenant':'platform'})
    identity=InvocationIdentity(tenant_id='tenant',employee_id='employee',user_id='user',run_id='run',
        capability='ordinary_dialogue',configuration_version=configured.configuration_version,sequence=0)
    provider=configured_codex_provider(configured,identity,round_budget=budget)
    try:
        response=await provider.generate(ModelRequest(model='synthetic',system_prompt='Read task context and return the bounded ordinary dialogue plan as JSON.',
            payload={'customer_visible_products':[],'allowed_question_topics':['quantity'],'customer':{'text':'We need hinges.'}},max_output_tokens=128))
    finally:
        await provider.aclose()
    assert json.loads(response.text)=={'kind':'ordinary','question_topics':['quantity']}
    assert len(calls)==2 and response.usage.output_tokens==10
    assert not list(short_jobs.iterdir())
