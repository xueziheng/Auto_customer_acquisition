"""仅回复事实组合不得注册发送消费者；旧排队发送也不能调用Provider。"""
from dataclasses import replace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.scheduler_worker import runtime as module
from infra.db.session import create_engine_from
from infra.db.workflow_engine import PostgresWorkflowEngine
from shared.schemas.identifiers import new_id
from tests.integration.test_scheduler_worker import (
    _factory_dependencies,
    _factory_environ,
    _FactoryCampaignFacts,
    _FactoryHealthServer,
    _FactoryResolver,
)
from tests.runtime_database_fixtures import (
    runtime_database_url as runtime_database_url,  # noqa: PLC0414
)
from workflows.engine.runner import StepDefinition, WorkflowDefinition


async def test_reply_facts_runtime_never_registers_or_runs_queued_sending(runtime_database_url):
    tenant = new_id("tn")
    url = await runtime_database_url(tenant)
    engine = create_engine_from(url)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async def forbidden_send(*args, **kwargs):
            pytest.fail("仅回复运行时不应处理历史发送任务")
        old = PostgresWorkflowEngine(sessions, {"outreach_campaign.send_message": forbidden_send})
        old.register(WorkflowDefinition(workflow_type="outreach_campaign", version=1,
            steps=(StepDefinition("send", "outreach_campaign.send_message"),), transitions={"send": ()}))
        await old.start(tenant, "outreach_campaign", "fixture-old-send", {}, "fixture-old-send")
        facts = _FactoryCampaignFacts()
        deps = replace(_factory_dependencies(module, with_hunter=False),
            reply_outreach_facts=module.ReplyOutreachFacts(facts, facts, facts, facts))
        factory = module.SchedulerRuntimeFactory(_factory_environ(url, tenant, hunter_enabled=False),
            deps, resolver_factory=_FactoryResolver, health_server_factory=_FactoryHealthServer)
        async with factory() as current:
            assert current.campaign_driver is None
            assert not any(key.startswith("outreach_campaign.") for key in current.workflow._handlers)
            # 原引擎不领取未注册旧流程；历史发送仍排队，不发生自动补发。
            assert await current.workflow.poll_due(tenant, 10) == 0
    finally:
        await engine.dispose()
