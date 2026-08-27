"""受控预览烟测：真实路由确认后才出现合成结果，绝不连接外部服务。"""

from httpx import ASGITransport, AsyncClient

from tests.research_ui_preview import TENANT, app
from tests.unit.test_runs_router import BOSS


async def test_controlled_preview_confirm_result_and_run_flow(monkeypatch):
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": TENANT, "X-Employee-Id": BOSS},
    ) as client:
        result = await client.post(
            "/commands/discovery-proposals",
            json={
                "message": "只研究美国铰链，三线路，排除消费电子，最多4个查询、总计6页、6条信号、3条假设"
            },
        )
        assert result.status_code == 200, result.text
        proposal = result.json()
        assert proposal["can_confirm"] is True
        confirmed = await client.post(
            f"/commands/discovery-proposals/{proposal['proposal_id']}/confirm"
        )
        assert confirmed.status_code == 200, confirmed.text
        assert (await client.get("/demand/signals")).json()[0]["research_evidence"][
            "discovery_lane"
        ] == "importer"
        accounts = await client.get("/prospects/accounts")
        assert accounts.status_code == 200, accounts.text
        assert len(accounts.json()[0]["research_signals"]) == 1
        run = await client.get(f"/runs/{confirmed.json()['run_id']}")
        assert run.status_code == 200, run.text
        assert run.json()["summary"]["research"]["consumed_credits"] == 3
        from apps.api.research import ResearchAccessService
        from tests.research_ui_preview import dependencies

        monkeypatch.setattr(
            dependencies,
            "research_access",
            ResearchAccessService(TENANT, None, configured=False),
        )
        replay = await client.post(
            f"/commands/discovery-proposals/{proposal['proposal_id']}/confirm"
        )
        assert replay.status_code == 200
        assert replay.json()["run_id"] == confirmed.json()["run_id"]
