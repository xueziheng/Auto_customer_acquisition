"""真实PG内部candidate commit后start失败；按原actor/key/payload恢复。"""

import asyncio
import json
import time
from dataclasses import replace
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from apps.api.controlled import initialize_identities
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from shared.errors import TransientError
from shared.schemas.identifiers import new_id
from tests.integration.conftest import RedactedUrl, _to_asyncpg
from tests.integration.test_email_inbound_gateway import (
    owned_infrastructure,  # noqa: F401
)
from tests.integration.test_email_inbound_page import page_runtime  # noqa: F401


@pytest.fixture
def db_url(_postgres_container, _migrated):
    """故障注入只作用于本测试拥有的容器，忽略外部连接配置。"""
    return RedactedUrl(_to_asyncpg(_postgres_container.get_connection_url()))


@pytest.mark.parametrize("failure", ["start_error", "database_unavailable"])
async def test_playbook_candidate_commit_then_start_failure_recovers_exact_command(
    page_runtime,  # noqa: F811
    _postgres_container,
    failure,
    monkeypatch,
    caplog,
):
    runtime = page_runtime
    config = runtime["config"].model_copy(
        update={
            "tenant_id": runtime["route"].tenant_id,
            "identities": tuple(
                i.model_copy(
                    update={"employee_id": new_id("emp"), "user_id": new_id("usr")}
                )
                for i in runtime["config"].identities
            ),
        }
    )
    await initialize_identities(config)
    tenant = config.tenant_id
    deps = runtime["deps"]
    original = deps.workflow_engine.start
    calls = []

    async def failed_start(*args, **kwargs):
        calls.append(args)
        if failure == "start_error":
            raise TransientError("受控start故障")
        container = _postgres_container.get_wrapped_container()
        container.reload()

        async def restore_database():
            await asyncio.sleep(2)
            container.reload()
            owned = container
            if owned.status == "paused":
                await asyncio.to_thread(owned.unpause)

        restoration = asyncio.create_task(restore_database())
        try:
            await asyncio.to_thread(container.pause)
            return await asyncio.wait_for(original(*args, **kwargs), timeout=1)
        finally:
            await asyncio.shield(restoration)
            container.reload()
            owned = container
            if owned.status == "paused":
                await asyncio.to_thread(owned.unpause)
            container.reload()
            owned = container
            assert owned.status == "running"
            ready = await asyncio.to_thread(
                owned.exec_run, ["pg_isready"]
            )
            assert ready.exit_code == 0
            async with runtime["factory"]() as probe:
                assert await probe.scalar(text("SELECT 1")) == 1

    monkeypatch.setattr(deps.workflow_engine, "start", failed_start)
    app = create_app(
        settings=ApiSettings(tenant_id=tenant, dev_mode=True, retry_after_seconds=2),
        dependencies=deps,
    )
    headers = {
        "X-Tenant-Id": tenant,
        "X-Employee-Id": config.identities[0].employee_id,
        "Idempotency-Key": "task12-settings-original",
    }
    payload = {
        "company_type": "trading_company",
        "minimum_deal_amount": "1000.00",
        "minimum_deal_currency": "USD",
        "excluded_categories": [],
        "excluded_countries": [],
        "sourcing_regions": ["controlled"],
    }
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://controlled.test",
        headers=headers,
    ) as client:
        failure_started = time.monotonic()
        failed = await client.post("/settings/playbook/proposals", json=payload)
        failure_elapsed = time.monotonic() - failure_started
        assert failure_elapsed < 8
        assert failed.status_code == (503 if failure == "start_error" else 500)
        assert len(calls) == 1
        recovery_statuses = []
        recovery_started = time.monotonic()
        for _ in range(20):
            versions = await client.get("/settings/playbook/versions")
            recovery_statuses.append(versions.status_code)
            if versions.status_code == 200:
                break
            await asyncio.sleep(0.25)
        if versions.status_code != 200:
            kinds = [
                getattr(record, "error_type", "unknown") for record in caplog.records
            ]
            try:
                async with runtime["factory"]() as diagnostic_session:
                    await diagnostic_session.execute(text("SELECT 1"))
                connection_probe = "success"
            except Exception as error:  # noqa: BLE001 - safe diagnostic types only
                connection_probe = type(error).__name__
            pytest.fail("受控恢复固定诊断: " + connection_probe + ";" + ",".join(kinds))
        runtime["settings_recovery"] = {
            "first_write_status": failed.status_code,
            "fault_elapsed_seconds": round(failure_elapsed, 3),
            "read_statuses": recovery_statuses,
            "elapsed_seconds": round(time.monotonic() - recovery_started, 3),
        }
        evidence = Path(__file__).resolve().parents[2] / "output/acceptance/task12"
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / ("settings-" + failure + ".json")).write_text(
            json.dumps(runtime["settings_recovery"])
        )
        persisted = versions.json()
        assert len(persisted) == 1
        assert persisted[0]["approval_id"] is None
        candidate_id = persisted[0]["version"]["playbook_version_id"]
        async with runtime["factory"]() as session:
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM workflow_runs WHERE tenant_id=:tenant AND workflow_type=:kind"
                    ),
                    {"tenant": tenant, "kind": "playbook_change"},
                )
                == 0
            )
        monkeypatch.setattr(deps.workflow_engine, "start", original)
        # 新API实例重建请求层；候选只能从PG原键恢复，不能依赖旧HTTP内存回执。
        restarted = create_app(
            settings=ApiSettings(
                tenant_id=tenant, dev_mode=True, retry_after_seconds=2
            ),
            dependencies=replace(deps),
        )
        async with AsyncClient(
            transport=ASGITransport(app=restarted),
            base_url="http://controlled.test",
            headers=headers,
        ) as recovered:
            first = await recovered.post("/settings/playbook/proposals", json=payload)
            replay = await recovered.post("/settings/playbook/proposals", json=payload)
            assert first.status_code == replay.status_code == 202
            assert first.json() == replay.json()
            assert first.json()["playbook_version_id"] == candidate_id
            assert len((await recovered.get("/settings/playbook/versions")).json()) == 1
        async with runtime["factory"]() as session:
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM workflow_runs WHERE tenant_id=:tenant AND workflow_type=:kind"
                    ),
                    {"tenant": tenant, "kind": "playbook_change"},
                )
                == 1
            )
