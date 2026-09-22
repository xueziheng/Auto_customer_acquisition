"""持久历史在当前范围变化后不泄露派生总结；读取工厂保留当前授权。"""

from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_runtime.assistant.context import AssistantContextBuilder, HistoryProjector
from agent_runtime.assistant.reads import BusinessReads, CurrentEmployeeIdentity
from domains.assistant.schemas import AuthorizedFragment, Explanation, ObjectRef
from domains.employees.schemas import EmployeeView
from shared.errors import PermissionDenied
from tests.integration.test_assistant import actor, input_text, service
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414
)


async def test_persisted_summary_and_followup_hidden_after_revocation(
    unit_engine,
):
    from infra.db.assistant import SqlAssistantRepository
    from tests.unit.test_assistant_context import A, B, Identity, Reads

    owner = actor()
    app = service(unit_engine)
    session = await app.create_session(owner)
    first = await app.accept_turn(owner, session.session_id, input_text())
    await app.deliver(owner, session.session_id, first.turn_id, "running")
    await app.deliver(
        owner,
        session.session_id,
        first.turn_id,
        "completed",
        result=Explanation(
            fragments=(
                AuthorizedFragment(text="PRIVATE_SENTINEL", dependencies=(A, B)),
            )
        ),
    )
    second = await app.accept_turn(owner, session.session_id, input_text(key="second"))
    repo = SqlAssistantRepository(
        async_sessionmaker(unit_engine, expire_on_commit=False)
    )
    reads = Reads()
    projector = HistoryProjector(Identity(), reads, repo)
    app._projector = projector
    assert not (
        await app.get_turn(owner, session.session_id, first.turn_id)
    ).content_hidden
    reads.allowed = {A}
    history = await app.list_turns(owner, session.session_id)
    assert all(t.content_hidden for t in history)
    assert "PRIVATE_SENTINEL" not in str([t.model_dump() for t in history])
    builder = AssistantContextBuilder(
        Identity(), reads, repo, projector, configuration_version="v1", max_bytes=50000
    )
    from shared.errors import ValidationError

    with pytest.raises(ValidationError):
        await builder.build(owner, session.session_id, second.turn_id)


async def test_current_identity_validates_real_user_mapping_and_team_each_read():
    owner = actor()
    employee = EmployeeView(
        owner.employee_id, owner.tenant_id, "经理", "manager", owner.user_id
    )
    report = EmployeeView(
        "report",
        owner.tenant_id,
        "销售",
        "sales",
        "report_user",
        manager_id=owner.employee_id,
    )

    class Employees:
        async def get_employee(self, *args, **kwargs):
            return employee

        async def list_active(self, *args, **kwargs):
            return [employee, report]

    @asynccontextmanager
    async def scope(tenant):
        yield Employees()

    identity = CurrentEmployeeIdentity(scope, None)
    assert "report" in (await identity.opportunity_actor(owner)).scope.allowed_owners
    report = replace(report, manager_id="other")
    assert (
        "report" not in (await identity.opportunity_actor(owner)).scope.allowed_owners
    )
    employee = replace(employee, role="finance")
    assert await identity.resolve(owner) == ("finance", frozenset({"product_help"}))
    with pytest.raises(PermissionDenied):
        await identity.opportunity_actor(owner)
    for change in ({"user_id": "other"}, {"is_active": False}, {"tenant_id": "other"}):
        employee = replace(employee, **change)
        with pytest.raises(PermissionDenied):
            await identity.resolve(owner)


async def test_need_access_is_checked_before_need_fetch_and_list_is_owner_scoped():
    owner = actor()
    seen = []

    class Identity:
        async def resolve(self, actor):
            return "sales", frozenset({"business_read"})

        async def opportunity_actor(self, actor):
            from domains.opportunities.service import (
                Actor,
                OpportunityScope,
                ScopeLevel,
            )

            return Actor(
                str(actor.employee_id),
                OpportunityScope(
                    level=ScopeLevel.SELF, allowed_owners=frozenset({actor.employee_id})
                ),
                "sales",
            )

    class Opportunities:
        async def get_by_need(self, *args, **kwargs):
            return None

        async def list_opportunities(self, tenant, bound, **kwargs):
            seen.append(kwargs["scope"].allowed_owners)
            return []

    @asynccontextmanager
    async def opportunities(tenant):
        yield Opportunities()

    @asynccontextmanager
    async def demand(tenant):
        pytest.fail("未授权不能读取需求")
        yield

    reader = BusinessReads(Identity(), opportunities, demand, None)
    with pytest.raises(PermissionDenied):
        await reader.read(owner, ObjectRef(kind="need", object_id="other_need"))
    from domains.assistant.schemas import AssistantReadQuery

    assert await reader.list(owner, AssistantReadQuery(kind="need", limit=10)) == ()
    assert seen == [frozenset({owner.employee_id})]
    with pytest.raises(PermissionDenied):
        await reader.read(owner, ObjectRef(kind="run", object_id="other_run"))
