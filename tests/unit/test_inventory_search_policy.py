"""平台搜索预算必须显式且金额不接受float。"""
import importlib
from decimal import Decimal

import pytest


def test_policy_default_disallows_paid_and_requires_decimal():
    module = importlib.import_module("shared.schemas.search_backend")
    policy = module.InventorySearchPolicy(version="v1")
    assert policy.backend_id == "deepseek" and not policy.paid_search_enabled
    assert policy.maximum_requests == 10
    with pytest.raises(ValueError):
        module.InventorySearchPolicy(version="v1", maximum_cost_cny=0.05)
    assert module.InventorySearchPolicy(version="v1", maximum_cost_cny=Decimal("0.05")).maximum_cost_cny == Decimal("0.05")


async def test_paid_disabled_never_resolves_or_probes():
    from types import SimpleNamespace

    from infra.db.inventory_sources import InventorySources
    from shared.schemas.search_backend import InventorySearchPolicy
    async def forbidden(*args):
        pytest.fail("未授权搜索不能解析或探测供应商")
    sources = InventorySources(tenant_id="t", backend=object(), quota=object(), configured=False,
        search_policy=InventorySearchPolicy(version="v1"), probe=forbidden)
    with pytest.raises(ValueError, match="许可"):
        await sources.prepare(SimpleNamespace(tenant_id="t"), forbidden)
