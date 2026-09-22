"""登录→多轮→精确确认→持锁后台→网关→来源与模型→持久研究证据。"""
import asyncio
import json
import secrets
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.standalone import create_standalone_app
from apps.scheduler_worker.bootstrap import ResearchRuntimePorts
from apps.scheduler_worker.main import run_scheduler_worker
from apps.scheduler_worker.standalone import (
    UnboundResearchClient,
    create_standalone_factory,
)
from connectors.deepseek.client import DeepSeekFailure
from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.s3 import S3ObjectBlobTransport
from connectors.web_search.transport import PublicPageResponse
from infra.authentication.service import PostgresAuthentication
from infra.db.session import create_engine_from
from infra.db.tables import (
    DemandSignalRow,
    EmployeeRow,
    ModelInvocationRow,
    NeedHypothesisRow,
    ToolCallRow,
    ValidatedNeedRow,
)
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.identifiers import EmployeeId, TenantId, UserId
from shared.schemas.model_invocation import ModelResponse, ModelUsage
from tests.e2e.conftest import (
    _seed_controlled_public_research_policy,
    _seed_controlled_research_playbook,
)
from tests.integration.test_pilot_persistence import initialized
from tests.integration.test_pilot_persistence import (
    owned_profiles as owned_profiles,  # noqa: PLC0414
)
from tests.integration.test_search_quota import Pages, SearchTransport, Secrets
from tests.unit.test_standalone_model_settings import settings

FIELDS={'objective':'研究铰链需求','target_countries':'US','target_categories':'hinges','excluded_countries':'无','excluded_categories':'无','max_search_queries':'3','max_pages_read':'3','max_signals':'3','max_hypotheses':'3','minimum_confidence_tier':'low','strategy_group':'demand_first','query_limit':'1'}
PAGE='We are Acme Tools, an importer and distributor with an online store. We are based in US.'


class Provider:
    def __init__(self, unknown=False): self.calls=[]; self.unknown=unknown
    async def generate(self, request):
        self.calls.append(request)
        if request.payload.get('probe'): value={'ok':True}
        elif 'decision_schema' in request.payload:
            history=request.payload['untrusted_history']
            if len(history)==1:
                value={'kind':'clarify','questions':['请补全国家与预算。'],'missing_fields':list(FIELDS)}
            else:
                value={'kind':'research'}
        else:
            if self.unknown: raise DeepSeekFailure('unknown')
            value={'signals':[{'signal_type':'marketplace_seller_activity','source_page_index':0,'source_excerpt':PAGE,'possible_need':'hinges','evidence_level':'agent_industry_inference'}],
                   'hypotheses':[{'account_name_signal_index':0,'country_signal_index':0,'signal_indexes':[0],'country':'US','category':'hinges','reasoning':'经营相关商品，可能需要铰链，值得验证'}]}
        return ModelResponse(text=json.dumps(value),model=request.model,usage=ModelUsage(input_tokens=20,cached_input_tokens=0,output_tokens=30))
    async def aclose(self): pass


class PageTransport(Pages):
    async def fetch(self,url):return PublicPageResponse(url,('<html><body>'+PAGE+'</body></html>').encode())


@pytest.mark.parametrize("unknown", [False, True])
def test_product_confirmed_research_persists_sourced_signals_without_outbound(owned_profiles, unknown):
    directory, profiles=owned_profiles
    profile=initialized(directory,profiles)
    config=StandaloneModelSettings.model_validate({**settings(),'limits':{**settings()['limits'],'tenant_calls':20,'employee_calls':20,'max_input_bytes':65536,'max_output_tokens':3000},'research':{'secret_ref':'TAVILY_API_KEY_REF','exclusive_account_confirmed':True,'playbook_reader_user_id':'usr_admin','maximum_artifact_bytes':100000,'search_timeout_seconds':5,'page_timeout_seconds':5}})
    provider,search=Provider(unknown),SearchTransport(limit=100)
    async def exercise():
        engine=create_engine_from(profile.config.database_url.get_secret_value());sessions=async_sessionmaker(engine,expire_on_commit=False)
        tenant=TenantId(profile.config.tenant_id); boss=EmployeeId('emp_admin'); approver=EmployeeId('emp_approver')
        auth=PostgresAuthentication(sessions,tenant); password=SecretStr(secrets.token_urlsafe(24))
        async with sessions.begin() as db:
            for employee,user,role in [(boss,'usr_admin','boss'),(approver,'usr_approver','boss'),('emp_sales','usr_sales','sales')]:
                db.add(EmployeeRow(tenant_id=tenant,employee_id=employee,user_id=user,name='受控测试',role=role,is_active=True))
        await auth.create_account('synthetic',password,boss)
        await _seed_controlled_public_research_policy(sessions,tenant,boss,approver,country='US')
        await _seed_controlled_research_playbook(sessions,tenant,boss,approver)
        owned=S3ObjectBlobTransport(S3ObjectStoreSettings.from_pilot_environ(profile.config.runtime_environment()),profile.config)
        ports=ResearchRuntimePorts(UnboundResearchClient(),config.model,UserId('usr_admin'),search,PageTransport(),owned,100000,'TAVILY_API_KEY_REF',Secrets(),True)
        app=create_standalone_app(profile.config,config,Path('apps/web/dist').resolve())
        factory=create_standalone_factory(profile.config,config,Secrets(),provider_factory=lambda:provider,research_ports=ports)
        origin=f'http://127.0.0.1:{profile.config.api_port}'
        try:
            async with app.router.lifespan_context(app), factory() as runtime, httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url=origin) as client:
                trusted={'Origin':origin,'X-TradeOS-Request':'1'}
                login=await client.post('/api/auth/login',headers=trusted,json={'username':'synthetic','password':password.get_secret_value()})
                assert login.status_code==200,'LOGIN_FAILED'
                headers={**trusted,'X-CSRF-Token':login.json()['csrf_token']}
                stop=asyncio.Event();stage=0;path='';proposal_id='';research_run=''
                async def tick(interval,event):
                    nonlocal stage,path,proposal_id,research_run
                    if stage==0:
                        response=await client.post('/api/settings/model/probe',headers=headers,json={'idempotency_key':'probe'})
                        assert response.status_code==202
                    if stage==7:
                        assert (await client.get('/api/settings/model')).json()['status']=='verified'
                        created=await client.post('/api/agent/sessions',headers=headers,json={});assert created.status_code==201
                        path=f"/api/agent/sessions/{created.json()['session_id']}/turns"
                        assert (await client.post(path,headers=headers,json={'text':'研究铰链市场','idempotency_key':'first'})).status_code==202
                    if stage==14:
                        assert (await client.get(path)).json()[0]['state']=='awaiting_input'
                        response=await client.post(path,headers=headers,json={'text':'；'.join(f'{k}={v}' for k,v in FIELDS.items()),'idempotency_key':'scope'})
                        assert response.status_code==202
                    if stage==21:
                        turns=(await client.get(path)).json();assert turns[-1]['state']=='proposal_ready',turns[-1]
                        proposal_id=turns[-1]['proposal_id']
                        url=f'/api/commands/discovery-proposals/{proposal_id}/confirm'
                        response=await client.post(url,headers=headers,json={});assert response.status_code==200,response.text
                        research_run=response.json()['run_id']
                        repeated=await client.post(url,headers=headers,json={});assert repeated.status_code==200 and repeated.json()['run_id']==research_run
                    if stage==29:
                        run=await runtime.workflow.get_run(tenant,research_run)
                        assert run.status.value==('failed' if unknown else 'completed'),(run.status.value,run.last_error,run.context)
                        detail=await client.get('/api/runs/'+research_run)
                        assert detail.status_code==200, 'RESEARCH_RUN_DETAIL_UNAVAILABLE'
                        summary=detail.json()['summary']
                        assert summary['status']==('failed' if unknown else 'completed')
                        if unknown:
                            assert summary['research']['completion_reason']=='model_unknown'
                            assert summary['research']['stop_reason']=='model_unknown'
                        listed=await client.get('/api/runs',params={'workflow_type':'demand_discovery'})
                        assert listed.status_code==200, 'RESEARCH_RUN_LIST_UNAVAILABLE'
                        assert any(item['run_id']==research_run for item in listed.json())
                        async with sessions() as db:
                            signals=(await db.scalars(select(DemandSignalRow).where(DemandSignalRow.tenant_id==tenant))).all()
                            hypotheses=(await db.scalars(select(NeedHypothesisRow).where(NeedHypothesisRow.tenant_id==tenant))).all()
                            assert (not signals and not hypotheses) if unknown else (len(signals)==3 and hypotheses), (run.context.get("completion_reason"), search.calls, len(provider.calls))
                            assert all(s.research_evidence and s.snapshot_artifact_ref for s in signals)
                            assert await db.scalar(select(func.count()).select_from(ValidatedNeedRow).where(ValidatedNeedRow.tenant_id==tenant))==0
                            invocations=(await db.scalars(select(ModelInvocationRow).where(ModelInvocationRow.tenant_id==tenant))).all()
                            assert len(invocations)==4 and all(i.employee_id==boss and i.user_id=='usr_admin' for i in invocations)
                            assert [i.run_id for i in invocations if i.capability=='research']==[research_run]
                            if unknown:
                                invocation=next(i for i in invocations if i.capability=='research')
                                assert invocation.state=='unknown' and not invocation.slot_released
                            tools=set((await db.scalars(select(ToolCallRow.tool_id).where(ToolCallRow.tenant_id==tenant))).all())
                            assert 'model.generate' in tools and not any(t.startswith(('email.','contact.','quotation.')) for t in tools)
                        assert search.calls==3
                    if stage==31:
                        assert len(provider.calls)==4 and search.calls==3
                        stop.set()
                    stage+=1
                await run_scheduler_worker(runtime,stop_event=stop,wait=tick,install_signal_handlers=False)
        finally:
            await owned.aclose();await engine.dispose()
    asyncio.run(exercise())
