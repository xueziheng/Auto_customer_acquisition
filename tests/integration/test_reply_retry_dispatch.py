"""恢复必须能被原持久引擎实际领取，不能只测试分类器直调。"""
import pytest
from sqlalchemy import select

from infra.db.tables import WorkflowRunRow, WorkflowStepRow
from infra.db.workflow_engine import PostgresWorkflowEngine
from tests.integration.test_reply_retry import seed
from workflows.engine.runner import StepDefinition, WorkflowDefinition


@pytest.mark.parametrize('legacy_pending',[False,True])
async def test_recovery_is_claimed_once_by_actual_engine(integration_engine,legacy_pending):
    sessions,tenant,run,message,actor,_,repo=await seed(integration_engine)
    await repo.request(tenant,message,actor,'same-approved-request')
    if legacy_pending:
        async with sessions.begin() as db:
            row=await db.scalar(select(WorkflowRunRow).where(WorkflowRunRow.tenant_id==tenant,WorkflowRunRow.run_id==run))
            row.status='pending'
        assert (await repo.read(tenant,message,actor)).can_retry
        await repo.request(tenant,message,actor,'same-approved-request')
    calls=[]
    class Handler:
        async def execute(self,claimed):
            calls.append(claimed.run_id)
            return 'complete',None,{}
    engine=PostgresWorkflowEngine(sessions,{'classify':Handler()},now=repo.now)
    engine.register(WorkflowDefinition(workflow_type='reply_qualification',version=1,steps=(StepDefinition(step_name='classify',handler_ref='classify'),),transitions={}))
    assert await engine.poll_due(tenant,10)==1
    assert await engine.poll_due(tenant,10)==0
    assert calls==[run]
    async with sessions() as db:
        rows=(await db.scalars(select(WorkflowStepRow).where(WorkflowStepRow.tenant_id==tenant,WorkflowStepRow.run_id==run))).all()
        assert len(rows)==2 and any(r.status=='failed' and r.error=='ModelGenerationError' for r in rows)
