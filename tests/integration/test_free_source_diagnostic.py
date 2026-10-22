"""实际Gateway诊断账本准确失败；一次性journal阻止新run重试。"""
import json
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.tables import ToolCallRow
from scripts.diagnose_free_sources import main
from shared.schemas.identifiers import new_id


@pytest.mark.parametrize("paid,status",[(False,200),(False,429),(False,500),(True,200)])
async def test_source_diagnostic_bounded_fixed_http_error_ledger(integration_engine,tmp_path,monkeypatch,capsys,status,paid):
    tenant=new_id("tn")
    calls=[]
    class Profile:
        tenant_id=tenant
        database_url=SecretStr(integration_engine.url.render_as_string(False))
        def runtime_environment(self):
            return {"TOOL_CALL_FINGERPRINT_KEY_VERSION":"v1","TOOL_CALL_FINGERPRINT_KEY_REF":"SYNTHETIC"}
        def resolve(self,ref):
            assert ref=="SYNTHETIC"
            return "f"*32
    monkeypatch.setattr("infra.pilot.config.PilotConfig.read",lambda _:Profile())
    monkeypatch.setattr("infra.standalone.settings.load_model_settings",lambda _:SimpleNamespace(research=SimpleNamespace(exclusive_account_confirmed=True,secret_ref="SYNTHETIC") if paid else None,model_catalog_directory=None,api_control_tenant=None))
    class Response:
        path=""
        def read(self,maximum):
            assert maximum==1048577
            if self.path == "/usage":
                return b'{"account":{"current_plan":"Project","plan_usage":0,"plan_limit":1000,"paygo_usage":0,"paygo_limit":0}}'
            return b'{"notices":[{"links":{"html":"https://ted.europa.eu/en/notice/1-2026"}}]}'
    Response.status=status
    class Connection:
        def __init__(self,host,port,**kwargs):
            assert port == 443 and host in {"api.ted.europa.eu","api.tavily.com"}
        def request(self,method,path,body,headers):
            self.path=path
            if path == "/usage":
                assert method == "GET"
                calls.append(1)
                return
            assert method=="POST" and path=="/v3/notices/search"
            assert "Authorization" not in headers
            assert json.loads(body)["limit"]==1
            calls.append(1)
        def getresponse(self):
            response=Response()
            response.path=self.path
            if self.path=="/usage": response.status=200
            return response
        def close(self): pass
    monkeypatch.setattr("http.client.HTTPSConnection",Connection)
    journal=tmp_path/"once.json"
    await main(tmp_path/"profile",tmp_path/"model",journal_path=journal)
    output=json.loads(capsys.readouterr().out)
    assert output["supplier_http_total"]==(2 if paid else 1) and output["http_cap"]==6
    assert output["model_requests"]==output["page_requests"]==output["business_writes"]==0
    assert output["results"]["deepseek"]["status"]=="not_run"
    assert output["results"]["ted"]["status"]==("sample" if status==200 else "unknown")
    sessions=async_sessionmaker(integration_engine)
    async with sessions() as db:
        saved=(await db.scalars(select(ToolCallRow).where(ToolCallRow.tenant_id==tenant,ToolCallRow.tool_id=="diagnostic.source.ted"))).one()
        assert saved.status==("succeeded" if status==200 else "failed_transient")
    with pytest.raises(FileExistsError):
        await main(tmp_path/"profile",tmp_path/"model",journal_path=journal)
    assert len(calls)==(2 if paid else 1)
    if paid:
        assert output["results"]["tavily"]["status"]=="not_run"
        assert output["results"]["tavily"]["attempted"]=={"usage":1,"search":0}
