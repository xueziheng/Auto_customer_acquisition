"""受控预览烟测：真实路由确认后才出现合成结果，绝不连接外部服务。"""

from httpx import ASGITransport, AsyncClient

from tests.research_ui_preview import TENANT, app
from tests.unit.test_runs_router import BOSS


async def test_confirmed_without_run_recovers_by_read_then_explicit_same_key(monkeypatch):
    from shared.errors import TransientError
    from tests.research_ui_preview import fixture

    start = fixture.start
    calls = []

    async def fail_first(*args):
        calls.append(args[-1])
        if len(calls) == 1:
            raise TransientError("受控启动暂不可用")
        return await start(*args)

    monkeypatch.setattr(fixture, "start", fail_first)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test",
        headers={"X-Tenant-Id": TENANT, "X-Employee-Id": BOSS},
    ) as client:
        proposal = (await client.post("/commands/discovery-proposals", json={"message": "只研究"})).json()
        path = f"/commands/discovery-proposals/{proposal['proposal_id']}"
        assert (await client.post(f"{path}/confirm")).status_code == 503
        assert (await client.get(path)).json()["state"] == "confirmed"
        assert not any(run.subject_ref == proposal["proposal_id"] for run in fixture.runs.values())
        execution = await client.get(f"{path}/execution")
        assert execution.status_code == 200
        assert execution.json()["state"] == "not_started"
        assert execution.json()["can_resume"] is True
        assert len(calls) == 1
        resumed = await client.post(f"{path}/confirm")
        assert resumed.status_code == 200
        execution = (await client.get(f"{path}/execution")).json()
        assert execution["state"] == "started"
        assert execution["run_id"] == resumed.json()["run_id"]
        assert execution["can_resume"] is False
        assert calls == [f"demand-discovery:{proposal['proposal_id']}"] * 2


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
        assert proposal["planned_source_channels"] == ["public_web", "industry_directory"]
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
        assert run.json()["summary"]["research"]["planned_source_channels"] == ["public_web", "industry_directory"]
        assert run.json()["summary"]["research"]["searched_source_channels"] == ["public_web"]
        assert run.json()["summary"]["research"]["source_channels"] == ["public_web"]
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
