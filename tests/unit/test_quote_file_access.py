"""文件用途独立于成本角色及审批决定人；当前scope不能由格式推导。"""

import asyncio
from contextlib import asynccontextmanager

import pytest

from domains.quotations.file_access import (
    QuoteFileAccessServiceImpl,
    require_quote_file_scope,
)
from domains.quotations.file_access_schemas import QuoteFileScopeFacts
from shared.schemas.quote_facts import QuoteEmployeeFact


def scope(
    role="sales",
    *,
    actor="emp_owner",
    active=True,
    owner_active=True,
    manager="emp_manager",
):
    return QuoteFileScopeFacts(
        tenant_id="tn_00000000000000000000000001",
        opportunity_id="opp_existing",
        actor=QuoteEmployeeFact(
            tenant_id="tn_00000000000000000000000001",
            employee_id=actor,
            role=role,
            is_active=active,
            manager_id=None,
            team_id=None,
        ),
        owner=QuoteEmployeeFact(
            tenant_id="tn_00000000000000000000000001",
            employee_id="emp_owner",
            role="sales",
            is_active=owner_active,
            manager_id=manager,
            team_id=None,
        ),
    )


@pytest.mark.parametrize(
    "role,actor",
    [("sales", "emp_owner"), ("manager", "emp_manager"), ("boss", "legacy_boss")],
)
def test_only_current_file_scope_can_pass(role, actor):
    assert require_quote_file_scope(scope(role, actor=actor)) is None


@pytest.mark.parametrize(
    "options",
    [
        {"role": role, "actor": "emp_owner"}
        for role in ("product", "sourcing", "finance")
    ]
    + [
        {"actor": "other_sales"},
        {"active": False},
        {"owner_active": False},
        {"role": "manager", "actor": "not_direct_manager"},
    ],
)
def test_cost_roles_ownership_or_revocation_cannot_bypass_scope(options):
    with pytest.raises(Exception) as error:
        require_quote_file_scope(scope(**options))
    assert error.value.code == "permission_denied"


async def test_policy_cleanup_does_not_replace_primary_cancellation():
    class Policies:
        @asynccontextmanager
        async def open(self, tenant_id, category):
            yield object()
            raise RuntimeError("private cleanup detail")

    service = QuoteFileAccessServiceImpl(
        None,
        None,
        None,
        None,
        None,
        Policies(),
        None,
        None,
        now=lambda: None,
        template_version="quote_pdf_v1",
    )
    primary = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError) as error:
        async with service._policy_lease("tenant", "category"):
            raise primary
    assert error.value is primary
