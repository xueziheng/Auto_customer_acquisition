"""候选领取API绑定会话身份、裁剪归属与拒绝越权输入。"""

from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.dependencies import get_request_identity
from apps.api.identity import RequestIdentity
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.schemas import EmployeeView
from domains.opportunities.permissions import Actor, OpportunityScope, ScopeLevel
from domains.prospecting.inventory import (
    AcquisitionJob,
    InventoryQuery,
    InventoryResult,
    InventoryStatus,
)
from shared.errors import IdempotencyConflict
from shared.schemas.identifiers import AcquisitionJobId, EmployeeId, TenantId

TENANT = TenantId("tn_inventory_api")
EMPLOYEE = EmployeeId("emp_inventory_api")
NOW = datetime(2026, 10, 13, tzinfo=UTC)


class Inventory:
    def __init__(self):
        self.calls = []
        self.job = AcquisitionJob(
            tenant_id=TENANT,
            id=AcquisitionJobId("acq_01K39P9M5D6K4A91YEQ80EJZ0X"),
            employee_id=EMPLOYEE,
            query=InventoryQuery(product_tags=("tricycle",)),
            requested_count=3,
            remaining_count=3,
            status="queued",
            lease_token="internal-fence",
            created_at=NOW,
            updated_at=NOW,
        )

    async def assign(
        self, tenant_id, allocation_ids, employee_id, *, acting_employee_id
    ):
        self.calls.append((tenant_id, allocation_ids, employee_id, acting_employee_id))
        return ()

    async def get_allocation(self, tenant_id, allocation_id):
        return self.allocation

    async def status(self, tenant_id):
        assert tenant_id == TENANT
        return InventoryStatus(
            tenant_id=TENANT,
            daily_limit=50,
            used_today=2,
            remaining_today=48,
            reset_at=NOW,
            fresh_candidates=0,
        )

    async def request(self, tenant_id, employee_id, query, count, request_key):
        self.calls.append((tenant_id, employee_id, query, count, request_key))
        return InventoryResult(allocations=(), job=self.job)

    async def list_allocations(self, tenant_id, allowed_owner_ids, limit, cursor=None):
        self.calls.append((tenant_id, allowed_owner_ids, limit, cursor))
        return ()

    async def list_jobs(self, tenant_id, allowed_owner_ids, limit):
        return (self.job,)

    async def get_job(self, tenant_id, job_id):
        return self.job


def setup(role="sales", owner=EMPLOYEE):
    employee = EmployeeView(
        employee_id=EMPLOYEE, tenant_id=TENANT, name="测试员工", role=role
    )
    scope = OpportunityScope(
        level=ScopeLevel.TENANT
        if role == "boss"
        else ScopeLevel.MANAGER
        if role == "manager"
        else ScopeLevel.SELF,
        allowed_owners=None if role == "boss" else frozenset({owner}),
    )
    identity = RequestIdentity(
        TENANT,
        employee,
        EmployeeActor(
            str(EMPLOYEE),
            EmployeeScope.TENANT if role == "boss" else EmployeeScope.SELF,
            role,
        ),
        Actor(str(EMPLOYEE), scope, role),
    )
    app = create_app(
        settings=ApiSettings(
            tenant_id=str(TENANT), dev_mode=True, retry_after_seconds=17
        )
    )
    app.dependency_overrides[get_request_identity] = lambda: identity
    service = Inventory()
    app.state.prospect_inventory_factory = lambda identity: service
    return app, service


@pytest.mark.asyncio
async def test_claim_uses_session_identity_and_hides_internal_lease():
    app, service = setup(role="boss")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": TENANT},
    ) as client:
        response = await client.post(
            "/prospects/inventory/requests",
            json={"query": {"product_tags": ["tricycle"]}, "count": 3},
            headers={"Idempotency-Key": "claim-intent"},
        )
    assert response.status_code == 202
    assert service.calls[0][:2] == (TENANT, EMPLOYEE)
    assert service.calls[0][-1] == "claim-intent"
    assert response.json()["job"]["status"] == "queued"
    assert "lease_token" not in response.text and "internal-fence" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra", [{"tenant_id": "other"}, {"employee_id": "other"}, {"count": 101}]
)
async def test_claim_rejects_spoof_and_invalid_count(extra):
    app, service = setup()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": TENANT},
    ) as client:
        response = await client.post(
            "/prospects/inventory/requests",
            json={"query": {"product_tags": ["tricycle"]}, "count": 3, **extra},
            headers={"Idempotency-Key": "claim-intent"},
        )
    assert response.status_code == 400
    assert service.calls == []


@pytest.mark.asyncio
async def test_claim_requires_idempotency_key():
    app, service = setup()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": TENANT},
    ) as client:
        response = await client.post(
            "/prospects/inventory/requests",
            json={"query": {"product_tags": ["tricycle"]}, "count": 3},
        )
    assert response.status_code == 400 and not service.calls


@pytest.mark.asyncio
async def test_allocation_read_is_scoped_to_current_employee():
    app, service = setup()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": TENANT},
    ) as client:
        response = await client.get("/prospects/inventory/allocations")
    assert response.status_code == 200
    assert service.calls == [(TENANT, (EMPLOYEE,), 50, None)]


@pytest.mark.asyncio
async def test_viewer_cannot_claim():
    app, service = setup(role="viewer")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": TENANT},
    ) as client:
        response = await client.post(
            "/prospects/inventory/requests",
            json={"query": {"product_tags": ["tricycle"]}, "count": 3},
            headers={"Idempotency-Key": "claim-intent"},
        )
    assert response.status_code == 403 and not service.calls


@pytest.mark.asyncio
async def test_other_employees_job_is_hidden():
    app, service = setup()
    service.job = service.job.model_copy(
        update={"employee_id": EmployeeId("emp_other")}
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": TENANT},
    ) as client:
        response = await client.get("/prospects/inventory/jobs/" + str(service.job.id))
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_unconfigured_inventory_is_explicitly_unavailable():
    app, _ = setup(role="boss")
    app.state.prospect_inventory_factory = None
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": TENANT},
    ) as client:
        response = await client.get("/prospects/inventory/status")
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_changed_idempotent_request_has_safe_conflict_response():
    app, service = setup(role="boss")

    async def conflict(*args):
        raise IdempotencyConflict("内部请求参数不得回显")

    service.request = conflict
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": TENANT},
    ) as client:
        response = await client.post(
            "/prospects/inventory/requests",
            json={"query": {"product_tags": ["tricycle"]}, "count": 3},
            headers={"Idempotency-Key": "claim-intent"},
        )
    assert response.status_code == 409
    assert "内部请求参数" not in response.text


@pytest.mark.parametrize("cursor", ["", "x" * 129])
async def test_history_invalid_cursor_is_bad_request(cursor):
    app, service = setup()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": TENANT},
    ) as client:
        response = await client.get(
            "/prospects/inventory/allocations", params={"cursor": cursor}
        )
    assert response.status_code == 400 and not service.calls


async def test_history_cursor_reaches_scoped_service_and_invalid_anchor_is_safe():
    app, service = setup()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": TENANT},
    ) as client:
        response = await client.get(
            "/prospects/inventory/allocations",
            params={"cursor": "allocation_cursor", "limit": 100},
        )
        assert response.status_code == 200
        assert service.calls == [(TENANT, (EMPLOYEE,), 100, "allocation_cursor")]

        async def invalid(*args):
            raise ValueError("private storage error")

        service.list_allocations = invalid
        response = await client.get(
            "/prospects/inventory/allocations", params={"cursor": "other_tenant_cursor"}
        )
        assert response.status_code == 400 and "private" not in response.text


async def test_sources_preserve_all_rows_and_runtime_capability_projection():
    from domains.prospecting.service import SourceState

    app, _ = setup(role="boss")

    class Reader:
        async def list_sources(self, tenant):
            assert tenant == TENANT
            return (
                SourceState(
                    source_id="tavily",
                    availability="quota_exhausted",
                    cost_class="free",
                    relevant=False,
                    inventory_capability="search_backend",
                ),
            )

    app.state.inventory_sources = Reader()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": TENANT},
    ) as client:
        response = await client.get("/prospects/inventory/sources")
    assert response.status_code == 200
    rows = {s["source_id"]: s for s in response.json()}
    assert len(rows) == 33
    assert rows["tavily"]["state"]["inventory_capability"] == "search_backend"
    assert rows["tavily"]["state"]["availability"] == "quota_exhausted"
    assert rows["tavily"]["state"]["source_role"] == "base_search"
    assert rows["codex_cli"]["state"]["source_role"] == "model_executor"
    assert rows["codex_cli"]["name"] == "Codex CLI"
    assert "内置网页搜索" not in rows["codex_cli"]["access_methods"]
    assert rows["rss"]["state"]["inventory_capability"] == "not_integrated"


@pytest.mark.parametrize("role", ["sales", "manager"])
async def test_staff_cannot_search_or_read_company_diagnostics(role):
    app, service = setup(role=role)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": TENANT},
    ) as client:
        response = await client.post(
            "/prospects/inventory/requests",
            json={"query": {"product_tags": ["tricycle"]}, "count": 3},
            headers={"Idempotency-Key": "key"},
        )
        assert response.status_code == 403
        for path in ("status", "sources", "jobs", "jobs/" + str(service.job.id)):
            assert (await client.get("/prospects/inventory/" + path)).status_code == 403
    assert service.calls == []


async def test_manager_only_reads_self_even_if_session_scope_has_subordinates():
    app, service = setup(role="manager", owner=EmployeeId("subordinate"))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": TENANT},
    ) as client:
        response = await client.get("/prospects/inventory/allocations")
    assert response.status_code == 200
    assert service.calls == [(TENANT, (EMPLOYEE,), 50, None)]


async def test_assignment_binds_actor_from_session():
    app, service = setup(role="boss")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": TENANT},
    ) as client:
        response = await client.post(
            "/prospects/inventory/assignments",
            json={"allocation_ids": ["cal_one", "cal_two"], "employee_id": "target"},
        )
    assert response.status_code == 200
    assert service.calls == [(TENANT, ("cal_one", "cal_two"), "target", EMPLOYEE)]


@pytest.mark.parametrize("role", ["sales", "manager"])
async def test_assignment_staff_is_denied_before_service(role):
    app, service = setup(role=role)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": TENANT},
    ) as client:
        response = await client.post(
            "/prospects/inventory/assignments",
            json={"allocation_ids": ["cal_one"], "employee_id": "target"},
        )
    assert response.status_code == 403 and not service.calls


@pytest.mark.parametrize(
    "extra",
    [
        {"allocation_ids": []},
        {"allocation_ids": ["cal_one"] * 2},
        {"allocation_ids": [f"cal_{i}" for i in range(101)]},
        {"employee_id": " "},
        {"tenant_id": "other"},
        {"acting_employee_id": "spoof"},
    ],
)
async def test_assignment_rejects_invalid_batch_and_spoofed_authority(extra):
    app, service = setup(role="boss")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": TENANT},
    ) as client:
        response = await client.post(
            "/prospects/inventory/assignments",
            json={"allocation_ids": ["cal_one"], "employee_id": "target", **extra},
        )
    assert response.status_code == 400 and not service.calls
