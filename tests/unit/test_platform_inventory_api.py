"""平台额度修改验证真实会话中间件、CSRF及受信公司绑定。"""

from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient

from domains.prospecting.service import InventoryStatus
from shared.schemas.identifiers import TenantId
from tests.unit.test_platform_access import ORIGIN, TRUSTED, build_app, login

COMPANY = TenantId("tn_inventory_company")


class QuotaService:
    def __init__(self):
        self.limit, self.writes = 50, []

    async def status(self, tenant_id):
        assert tenant_id == COMPANY
        return InventoryStatus(
            tenant_id=COMPANY,
            daily_limit=self.limit,
            used_today=2,
            remaining_today=max(0, self.limit - 2),
            reset_at=datetime(2026, 10, 14, tzinfo=UTC),
            fresh_candidates=100,
        )

    async def set_daily_limit(self, tenant_id, daily_limit):
        assert tenant_id == COMPANY
        self.writes.append(daily_limit)
        self.limit = daily_limit


async def test_quota_requires_platform_session_csrf_and_bound_company(monkeypatch):
    app, child, grants, _, _ = build_app(monkeypatch)
    service = QuotaService()
    child.state.inventory_companies = {str(COMPANY): service}
    path = f"/api/platform/inventory/companies/{COMPANY}/quota"
    async with (
        child.router.lifespan_context(child),
        AsyncClient(transport=ASGITransport(app), base_url=ORIGIN) as client,
    ):
        assert (await client.get(path)).status_code == 401
        session = await login(client)
        headers = {**TRUSTED, "X-CSRF-Token": session.json()["csrf_token"]}
        assert (
            await client.put(path, json={"daily_limit": 100}, headers=TRUSTED)
        ).status_code == 403
        changed = await client.put(path, json={"daily_limit": 100}, headers=headers)
        assert changed.status_code == 200 and changed.json()["daily_limit"] == 100
        assert "fresh_candidates" not in changed.json()
        assert (
            await client.put(
                path.replace(str(COMPANY), "other"),
                json={"daily_limit": 100},
                headers=headers,
            )
        ).status_code == 404
        assert (
            await client.put(path, json={"daily_limit": True}, headers=headers)
        ).status_code == 400
        assert (
            await client.put(
                path, json={"daily_limit": 3, "tenant_id": "other"}, headers=headers
            )
        ).status_code == 400
        assert (
            await client.put(
                path + "?tenant_id=other", json={"daily_limit": 3}, headers=headers
            )
        ).status_code == 400
        assert service.writes == [100]
        grants.allowed = False
        assert (
            await client.put(path, json={"daily_limit": 3}, headers=headers)
        ).status_code == 401
        assert service.writes == [100]
