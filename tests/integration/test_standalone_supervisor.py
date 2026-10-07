"""真实三进程停启：模型配置同版、单副本与仅调度器接收凭证。"""

import asyncio
import json
import secrets
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from infra.db.session import create_engine_from
from infra.db.tables import ModelInvocationRow, ModelRuntimeProcessRow
from infra.pilot.config import PilotError
from scripts.run_web_pilot import start_profile
from tests.integration.test_pilot_persistence import initialized
from tests.integration.test_pilot_persistence import (
    owned_profiles as owned_profiles,  # noqa: PLC0414
)
from tests.unit.test_standalone_model_settings import settings


async def model_state(config):
    engine = create_engine_from(config.database_url.get_secret_value())
    try:
        async with async_sessionmaker(engine)() as db:
            processes = (
                await db.execute(
                    select(
                        ModelRuntimeProcessRow.process,
                        ModelRuntimeProcessRow.version,
                        ModelRuntimeProcessRow.instance_id,
                    ).where(ModelRuntimeProcessRow.tenant_id == config.tenant_id)
                )
            ).all()
            calls = await db.scalar(
                select(func.count())
                .select_from(ModelInvocationRow)
                .where(ModelInvocationRow.tenant_id == config.tenant_id)
            )
            return processes, calls
    finally:
        await engine.dispose()


def test_standalone_supervisor_restarts_same_profile_without_calls(
    owned_profiles, monkeypatch
):
    directory, profiles = owned_profiles
    profile = initialized(directory, profiles)
    profile.stop()
    model_path = profile.path / "model.json"
    model_path.write_text(json.dumps(settings()))
    model_path.chmod(0o600)
    marker = secrets.token_urlsafe(24)
    unrelated = secrets.token_urlsafe(24)
    monkeypatch.setenv("DEEPSEEK_API_KEY", marker)
    monkeypatch.setenv("UNRELATED_PRIVATE_VALUE", unrelated)
    previous = None

    for _ in range(2):
        start_profile(profile.path, model_settings=model_path)
        profile.reload()
        state = profile.runtime_state()
        assert state.status == "running"
        assert {p.name for p in state.processes} == {"api", "scheduler", "notification"}
        origin = f"http://127.0.0.1:{profile.config.api_port}"
        with httpx.Client(base_url=origin, trust_env=False) as client:
            assert client.get("/api/health/ready").status_code == 200
            assert client.get("/commands").status_code == 200
            assert client.get("/api/auth/session").status_code == 401
        rows, calls = asyncio.run(model_state(profile.config))
        assert {(r.process, r.version) for r in rows} == {
            ("api", "explicit-v1"),
            ("scheduler", "explicit-v1"),
        }
        assert calls == 0
        instances = {r.instance_id for r in rows}
        if previous is not None:
            assert instances.isdisjoint(previous)
        previous = instances
        if Path("/proc/self/environ").exists():
            for process in state.processes:
                environment = (
                    Path(f"/proc/{process.pid}/environ").read_bytes().split(b"\0")
                )
                exposed = f"DEEPSEEK_API_KEY={marker}".encode() in environment
                assert exposed == (process.name == "scheduler"), (
                    "MODEL_CREDENTIAL_SCOPE"
                )
                hidden = (
                    f"UNRELATED_PRIVATE_VALUE={unrelated}".encode() not in environment
                )
                assert hidden, "UNRELATED_CREDENTIAL_EXPOSED"
        with pytest.raises(PilotError):
            start_profile(profile.path, model_settings=model_path)
        assert profile.runtime_state().status == "running"
        profile.stop()
        assert profile.runtime_state().status == "stopped"
        assert not state.supervisor.live()


def test_invalid_model_settings_does_not_start_storage(tmp_path):
    from scripts.pilot_web_supervisor import PilotSupervisor
    from tests.unit.test_pilot_profile import make_config

    make_config(tmp_path)
    build = tmp_path / "apps/web/dist"
    build.mkdir(parents=True)
    (build / "index.html").write_text("<html>TradeOS</html>")
    supervisor = PilotSupervisor(
        tmp_path / "profile",
        root=tmp_path,
        model_settings=tmp_path / "missing-model.json",
    )
    try:
        with pytest.raises(PilotError, match="configuration_invalid"):
            supervisor.start()
        assert supervisor.profile.config.storage == {}
    finally:
        supervisor.close()
