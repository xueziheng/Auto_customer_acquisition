"""实际API/worker工厂报价装配；真实解析链只在已验收Linux入口运行。"""

import json

import pytest

from apps.scheduler_worker import runtime as worker
from connectors.evidence_text.client import LinuxEvidenceTextParser
from connectors.object_store import s3
from shared.schemas.identifiers import new_id
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414 - pytest fixture
)
from tests.integration.test_scheduler_worker import (
    _factory_dependencies,
    _factory_environ,
    _FactoryHealthServer,
    _FactoryResolver,
    _TrackingEnvironmentSecrets,
)
from tests.quotation_runtime_fixtures import quotation_settings_values


def worker_environment(engine, tenant, mode):
    environ = _factory_environ(
        engine.url.render_as_string(hide_password=False), tenant, hunter_enabled=False
    )
    if mode != "no_config":
        values = quotation_settings_values()
        if mode == "no_files":
            values["files"] = None
        environ["TRADEOS_QUOTATION_SETTINGS_JSON"] = (
            "null" if mode == "malformed" else json.dumps(values)
        )
    if mode != "no_store":
        environ.update(
            {
                "TRADEOS_DEV_MODE": "true",
                "S3_ENDPOINT": "http://localhost:9000",
                "S3_BUCKET_ARTIFACTS": "test-bucket",
                "S3_ACCESS_KEY_REF": "TEST_ACCESS",
                "S3_SECRET_KEY_REF": "TEST_SECRET",
                "S3_REGION": "us-east-1",
                "RAW_ARTIFACT_MAX_BYTES": "2097152",
                "GENERATED_ARTIFACT_MAX_BYTES": "2097152",
            }
        )
    return environ


@pytest.mark.parametrize("mode", ["enabled", "no_files", "no_config", "no_store"])
async def test_actual_worker_factory_binds_unique_approvals_and_defers_probe_until_activation(
    unit_engine, monkeypatch, mode
):
    secrets = _TrackingEnvironmentSecrets()
    monkeypatch.setattr(worker, "EnvironmentSecretResolver", lambda _: secrets)
    monkeypatch.setattr(
        s3.boto3, "client", lambda *a, **kw: pytest.fail("worker构造不能创建S3 SDK")
    )
    probes = []
    original_probe = LinuxEvidenceTextParser.probe

    async def probe(parser):
        probes.append(parser)
        return await original_probe(parser)

    monkeypatch.setattr(LinuxEvidenceTextParser, "probe", probe)
    factory = worker.SchedulerRuntimeFactory(
        worker_environment(unit_engine, new_id("tn"), mode),
        _factory_dependencies(worker, with_hunter=False),
        resolver_factory=_FactoryResolver,
        health_server_factory=_FactoryHealthServer,
    )
    async with factory() as runtime:
        handlers = runtime.workflow._handlers
        enabled = mode not in {"no_config", "no_store"}
        assert (("quote_approval", 1) in runtime.workflow._definitions) is enabled
        assert (runtime.activation is not None) is enabled
        assert probes == []
        assert secrets.calls.count("SCHEDULER_FINGERPRINT_KEY") == 1
        assert "TEST_ACCESS" not in secrets.calls and "TEST_SECRET" not in secrets.calls
        if enabled:
            app = handlers["quote_approval.assemble"]._application
            assert app._approvals._quote_access._quotes is app._quotes
            assert handlers["quote_approval.apply"]._application is app
            assert app._quotes._approvals._runs._engine() is runtime.workflow
            assert (app._quotes._files is None) is (mode == "no_files")
            parser = runtime.activation.quotation_lifecycle._parser
    if mode not in {"no_config", "no_store"}:
        assert parser._closed
    assert probes == []


async def test_worker_explicit_invalid_quotation_json_is_not_silently_disabled(
    unit_engine, monkeypatch
):
    monkeypatch.setattr(
        worker, "EnvironmentSecretResolver", lambda _: _TrackingEnvironmentSecrets()
    )
    factory = worker.SchedulerRuntimeFactory(
        worker_environment(unit_engine, new_id("tn"), "malformed"),
        _factory_dependencies(worker, with_hunter=False),
        resolver_factory=_FactoryResolver,
        health_server_factory=_FactoryHealthServer,
    )
    with pytest.raises(ValueError, match="报价运行配置无效"):
        async with factory():
            pass


@pytest.mark.parametrize("failure", ["no_lock", "probe", "cancel", "before_yield"])
async def test_actual_worker_singleton_activation_and_cleanup(
    unit_engine, monkeypatch, failure
):
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncEngine

    from apps.scheduler_worker.main import WorkerStartStatus, run_scheduler_worker

    events = []
    parsers = []
    original_close = LinuxEvidenceTextParser.aclose
    original_dispose = AsyncEngine.dispose

    async def probe(parser):
        events.append("probe")
        parsers.append(parser)
        if failure == "cancel":
            raise asyncio.CancelledError()
        raise RuntimeError("private controlled startup failure")

    async def close(parser):
        events.append("close")
        parsers.append(parser)
        await original_close(parser)

    async def dispose(engine, *args, **kwargs):
        events.append("dispose")
        await original_dispose(engine, *args, **kwargs)

    class Health(_FactoryHealthServer):
        async def wait_started(self):
            if failure == "before_yield":
                raise RuntimeError("controlled before yield")
            await super().wait_started()

    monkeypatch.setattr(
        worker, "EnvironmentSecretResolver", lambda _: _TrackingEnvironmentSecrets()
    )
    monkeypatch.setattr(LinuxEvidenceTextParser, "probe", probe)
    monkeypatch.setattr(LinuxEvidenceTextParser, "aclose", close)
    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    environ = worker_environment(unit_engine, new_id("tn"), "enabled")
    factory = worker.SchedulerRuntimeFactory(
        environ,
        _factory_dependencies(worker, with_hunter=False),
        resolver_factory=_FactoryResolver,
        health_server_factory=Health,
    )

    if failure == "no_lock":
        async with factory() as runtime:  # noqa: SIM117 - 明确runtime资源早于持锁连接
            async with unit_engine.connect() as holder:
                assert await holder.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": runtime.config.lock_key},
                )
                await holder.commit()
                result = await run_scheduler_worker(
                    runtime, install_signal_handlers=False
                )
                assert result.status is WorkerStartStatus.NOT_STARTED
                assert events == []
                await holder.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": runtime.config.lock_key},
                )
                await holder.commit()
    else:
        expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
        with pytest.raises(expected):
            async with factory() as runtime:
                await run_scheduler_worker(runtime, install_signal_handlers=False)
        if failure != "before_yield":
            async with unit_engine.connect() as verifier:
                assert await verifier.scalar(
                    text("SELECT pg_try_advisory_lock(:key)"),
                    {"key": runtime.config.lock_key},
                )
                await verifier.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": runtime.config.lock_key},
                )
                await verifier.commit()
    assert events == (["probe"] if failure in {"probe", "cancel"} else []) + [
        "close",
        "dispose",
    ]
    assert len({id(parser) for parser in parsers}) == 1
    assert parsers[0]._closed
