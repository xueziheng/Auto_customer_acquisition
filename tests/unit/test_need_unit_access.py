"""单位用途是原成本与机会矩阵交集，不借文件scope增加owner前置。"""

import importlib
from contextlib import asynccontextmanager

import pytest

from domains.demand import service
from domains.demand.errors import NeedUnitError, NeedUnitPermissionError
from domains.opportunities.permissions import Phase1OpportunityAuthorizer
from tests.unit.test_need_units import ACTOR, NEED, TENANT
from tests.unit.test_quote_context_contracts import employee


class Contexts:
    def __init__(self, actor):
        self.actor = actor

    async def read_actor(self, tenant_id, actor_id):
        return self.actor


class Scopes:
    def __init__(self, actor):
        self.actor, self.active = actor, False

    @asynccontextmanager
    async def open(self, tenant_id, need_id, actor_id):
        self.active = True
        try:
            yield service.NeedUnitScopeFacts(
                tenant_id=TENANT,
                need_id=NEED,
                account_id="acct_test",
                opportunity_id="opp_test",
                actor=self.actor,
            )
        finally:
            self.active = False


def case(actor, locked=None):
    assert hasattr(service, "NeedUnitScopeFacts"), "缺少独立单位范围事实契约"
    module = importlib.import_module("workflows.quote_approval.need_unit_access")
    scopes = Scopes(locked or actor)
    return module.CurrentNeedUnitAuthorizer(
        Contexts(actor), scopes, Phase1OpportunityAuthorizer(TENANT)
    ), scopes


@pytest.mark.parametrize(
    "role", ["boss", "product", "sourcing", "finance", "sales", "manager", "viewer"]
)
async def test_unit_access_uses_actual_costing_and_opportunity_intersection(role):
    authorizer, scopes = case(employee(role))
    if role == "boss":
        async with authorizer.guard(TENANT, NEED, ACTOR, action="confirm") as access:
            assert access.account_id == "acct_test" and access.need_id == NEED
            assert access.authorization_ref == "phase1:boss:tenant:opportunity:read"
            assert scopes.active
    else:
        with pytest.raises(NeedUnitPermissionError):
            await authorizer.check(TENANT, NEED, ACTOR, action="read")
    assert not scopes.active


@pytest.mark.parametrize(
    "actor",
    [
        None,
        employee(is_active=False),
        employee(tenant_id="tenant_other"),
        employee(employee_id="emp_other"),
    ],
)
async def test_unit_access_rejects_missing_inactive_or_wrong_identity(actor):
    authorizer, scopes = case(actor)
    with pytest.raises(NeedUnitPermissionError):
        await authorizer.check(TENANT, NEED, ACTOR, action="read")
    assert not scopes.active


async def test_unit_guard_rechecks_locked_actor():
    authorizer, scopes = case(employee(), employee("finance"))
    with pytest.raises(NeedUnitPermissionError):
        async with authorizer.guard(TENANT, NEED, ACTOR, action="confirm"):
            pytest.fail("锁内撤权不能放行")
    assert not scopes.active


async def test_unit_access_rejects_unknown_action():
    authorizer, scopes = case(employee())
    with pytest.raises(NeedUnitError) as error:
        await authorizer.check(TENANT, NEED, ACTOR, action="approve")
    assert error.value.code == "invalid_input"
    assert not scopes.active
