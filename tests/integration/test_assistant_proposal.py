"""提案提交提交后失联和并发恢复不会产生第二个提案。"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from domains.directives.schemas import (
    DemandDiscoveryPlanInput,
    DiscoverySearchQueryInput,
)
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import TenantId, new_id
from tests.integration.test_directives_persistence import BOSS, _Employees
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414
)
from tests.unit.workflows.test_research_discovery import research_plan as workflow_plan


def research_plan():
    plan = workflow_plan()
    return DemandDiscoveryPlanInput(
        **{
            **vars(plan),
            "queries": tuple(
                DiscoverySearchQueryInput(**vars(q)) for q in plan.queries
            ),
        }
    )


def service(engine):
    from domains.directives.service_impl import DirectiveServiceImpl
    from infra.db.directive_uow import SqlAlchemyDirectiveUnitOfWork

    factory = async_sessionmaker(engine, expire_on_commit=False)
    return DirectiveServiceImpl(
        lambda tenant: SqlAlchemyDirectiveUnitOfWork(factory, tenant),
        _Employees(),
        now=lambda: datetime.now(UTC),
    )


async def submit(app, tenant, **kwargs):
    args = {
        "source_turn_id": "turn_source",
        "source_version": 1,
        "request_hmac": "hmac-v1",
        "raw_text": "仅研究美国铰链",
        "plan": research_plan(),
        "interpretation_summary": "研究三条线路",
        "expected_behavior_changes": [
            "只保存信号与假设，禁止发信、报价或晋升已验证需求"
        ],
        "parsed_by": "assistant-v1",
        "submitted_by": BOSS,
    }
    args.update(kwargs)
    return await app.submit_discovery_proposal_once(tenant, **args)


async def test_two_connections_and_restart_return_same_proposal(unit_engine):
    tenant = TenantId(new_id("tn"))
    app = service(unit_engine)
    ids = await asyncio.gather(
        submit(app, tenant), submit(service(unit_engine), tenant)
    )
    assert ids[0] == ids[1]
    assert await submit(service(unit_engine), tenant) == ids[0]
    with pytest.raises(ValidationError):
        await submit(app, tenant, request_hmac="different")
    with pytest.raises(ValidationError):
        await submit(app, tenant, plan=replace(research_plan(), max_pages_read=3))


async def test_sales_cannot_create_source_proposal(unit_engine):
    with pytest.raises(PermissionDenied):
        await submit(service(unit_engine), TenantId(new_id("tn")), submitted_by="sales")
