"""实际API/worker工厂报价装配；真实解析链只在已验收Linux入口运行。"""

import io
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

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


class ControlledObjects:
    """仅SDK网络边界受控；真实S3适配/Store/Gateway/renderer全部保留。"""

    def __init__(self):
        self.values, self.calls, self.clients = {}, [], 0

    def client(self, *args, **kwargs):
        self.clients += 1
        return self

    def put_object(self, *, Bucket, Key, Body):
        self.calls.append(("put", Key))
        self.values[Key] = Body

    def get_object(self, *, Bucket, Key):
        self.calls.append(("get", Key))
        return {
            "Body": io.BytesIO(self.values[Key]),
            "ContentLength": len(self.values[Key]),
        }

    def delete_object(self, *, Bucket, Key):
        self.calls.append(("delete", Key))
        self.values.pop(Key, None)

    def close(self):
        pass


@asynccontextmanager
async def actual_api_case(engine, monkeypatch, *, files=True):
    from fastapi import Request
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from apps.api import runtime as api
    from apps.api.dependencies import get_api_dependencies
    from tests.integration.test_api_runtime import _runtime_env

    tenant = new_id("tn")
    clock = [datetime(2026, 8, 29, 8, tzinfo=UTC)]

    class BusinessClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0]

    environ = _runtime_env(engine.url.render_as_string(hide_password=False))
    settings = quotation_settings_values()
    if not files:
        settings["files"] = None
    environ.update(
        {
            "TRADEOS_TENANT_ID": tenant,
            "TRADEOS_QUOTATION_SETTINGS_JSON": json.dumps(settings),
        }
    )
    objects = ControlledObjects()
    monkeypatch.setattr(s3.boto3, "client", objects.client)
    monkeypatch.setattr(api.os, "environ", environ)
    monkeypatch.setattr(api, "datetime", BusinessClock)
    app = api.create_runtime_app()
    assert objects.clients == 0 and objects.calls == []
    request = Request(
        {"type": "http", "app": app, "headers": [], "method": "GET", "path": "/"}
    )
    dependencies = get_api_dependencies(request)
    parser = dependencies.quotation.evidence.parser
    assert parser.capability().status == "unavailable"
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app), base_url="http://test") as client,
    ):
        yield SimpleNamespace(
            app=app,
            client=client,
            dependencies=dependencies,
            parser=parser,
            objects=objects,
            tenant=tenant,
            clock=clock,
            sessions=async_sessionmaker(engine, expire_on_commit=False),
        )
    assert parser._closed


async def seed_runtime_facts(case, actor="boss-runtime"):
    from pydantic import TypeAdapter

    from artifact_store.store import RawArtifactKind
    from infra.db.tables import (
        ConversationRow,
        EmployeeRow,
        MessageRow,
        OpportunityRow,
        ProspectAccountRow,
        ValidatedNeedRow,
    )
    from shared.schemas.provenance import FactualField, Provenance, SourceType
    from tests.unit.test_evidence_text_profiles import pdf_bytes
    from workflows.employee_work_intake.schemas import WorkSourceKind

    case.actor, case.decider, case.owner = actor, "independent-boss", "sales-owner"
    case.need, case.account, case.opportunity, case.conversation, case.message = (
        new_id(prefix) for prefix in ("need", "acc", "opp", "con", "msg")
    )
    raw = case.dependencies.work_uploads._artifacts
    email = await raw.put(
        case.tenant,
        RawArtifactKind.EMAIL_RAW,
        b"Content-Type: text/plain; charset=utf-8\r\n\r\nWe need 50 pieces.\r\n",
        "message/rfc822",
    )

    def field(value):
        return TypeAdapter(FactualField[type(value)]).dump_python(
            FactualField(
                value,
                Provenance(
                    SourceType.CONVERSATION,
                    case.message,
                    actor,
                    case.clock[0],
                    confirmed_by=actor,
                    confirmed_at=case.clock[0],
                    source_quote="We need 50 pieces."
                    if type(value) is int
                    else "Private provenance marker",
                ),
            ),
            mode="json",
        )

    async with case.sessions.begin() as session:
        for employee, role in [
            (actor, "boss"),
            (case.decider, "boss"),
            (case.owner, "sales"),
            ("product-reader", "product"),
            ("finance-reader", "finance"),
            ("manager-reader", "manager"),
        ]:
            session.add(
                EmployeeRow(
                    tenant_id=case.tenant,
                    employee_id=employee,
                    name=employee,
                    role=role,
                    is_active=True,
                    created_at=case.clock[0],
                )
            )
        session.add(
            ProspectAccountRow(
                tenant_id=case.tenant,
                account_id=case.account,
                name="Controlled Buyer",
                country="DE",
                source_signal_refs=[],
                created_at=case.clock[0],
            )
        )
        session.add(
            ConversationRow(
                tenant_id=case.tenant,
                conversation_id=case.conversation,
                account_id=case.account,
                channel="email",
                created_at=case.clock[0],
            )
        )
        await session.flush()
        session.add(
            MessageRow(
                tenant_id=case.tenant,
                message_id=case.message,
                conversation_id=case.conversation,
                direction="inbound",
                sent_at=case.clock[0],
                raw_artifact_ref=email.artifact_id,
                external_message_id="controlled:" + case.message,
            )
        )
        session.add(
            ValidatedNeedRow(
                tenant_id=case.tenant,
                need_id=case.need,
                account_id=case.account,
                product_category=field("hardware"),
                quantity=field(50),
                material=field("steel"),
                size_spec=field("50 mm"),
                packaging=field("cartons"),
                destination=field("DE"),
                source_message_id=case.message,
                source_conversation_id=case.conversation,
                status="validated",
                created_at=case.clock[0],
            )
        )
        session.add(
            OpportunityRow(
                tenant_id=case.tenant,
                opportunity_id=case.opportunity,
                need_id=case.need,
                account_id=case.account,
                account_name="Controlled Buyer",
                country="DE",
                product_category="hardware",
                state="qualified",
                owner=case.owner,
                created_at=case.clock[0],
            )
        )
    case.statement = "Quoted unit price: USD 2.00 for 50 pieces."
    upload = await case.dependencies.work_uploads.create_upload(
        case.tenant,
        actor,
        uploaded_by=None,
        artifact_kind=RawArtifactKind.PDF,
        source_kind=WorkSourceKind.PDF_TEXT,
        content=pdf_bytes(case.statement),
        mime_type="application/pdf",
        occurred_at=case.clock[0],
        customer_timezone="UTC",
    )
    case.source = "upload:" + upload.upload_id
    return case


async def runtime_request(
    case, method, path, body=None, *, actor=None, key=None, expected=200
):
    headers = {"X-Employee-Id": actor or case.actor, "X-Tenant-Id": case.tenant}
    if key is not None:
        headers["Idempotency-Key"] = key
    response = await case.client.request(
        method,
        "/costing-quotes" + path,
        headers=headers,
        **({"json": body} if method != "GET" else {}),
    )
    if response.status_code != expected:
        code = response.json().get("code", "unknown")
        if isinstance(code, str) and code.replace("_", "").isalnum():
            print("runtime_http_error=" + code)
    assert response.status_code == expected
    return (
        response.json()
        if "application/json" in response.headers.get("content-type", "")
        else response
    )


async def test_actual_api_factory_degraded_parser_keeps_safe_metadata_and_core(
    unit_engine, monkeypatch
):
    import sys

    if sys.platform == "linux":
        pytest.skip("此例专验Mac普通platform降级，Linux另走真实链")
    async with actual_api_case(unit_engine, monkeypatch) as case:
        assert case.parser.capability().status == "unavailable"
        await seed_runtime_facts(case)
        context = await runtime_request(
            case, "GET", f"/opportunities/{case.opportunity}/quote-context"
        )
        assert {b["field"] for b in context["blockers"]} == {"unit", "issuer"}
        assert context["need"]["unit_quantity_fact_hash"] is None
        unit = await runtime_request(case, "GET", f"/needs/{case.need}/unit")
        assert unit["quantity_fact_hash"]
        await runtime_request(
            case,
            "POST",
            "/evidence/preview",
            {
                "operation": "preview",
                "source_ref": case.source,
                "scope": {"purpose": "pricing"},
                "profile": "pdf-text-v1",
                "page": 1,
            },
            expected=503,
        )
        await runtime_request(
            case,
            "POST",
            "/issuer",
            {
                "name": "Controlled Supplier",
                "address": "Test address",
                "contact": "sales@example.test",
            },
            key="issuer",
        )
        context = await runtime_request(
            case, "GET", f"/opportunities/{case.opportunity}/quote-context"
        )
        assert {b["field"] for b in context["blockers"]} == {"unit"}


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
        assert (runtime.quote_expiry_driver is not None) is enabled
        assert probes == []
        assert secrets.calls.count("SCHEDULER_FINGERPRINT_KEY") == 1
        assert "TEST_ACCESS" not in secrets.calls and "TEST_SECRET" not in secrets.calls
        if enabled:
            app = handlers["quote_approval.assemble"]._application
            assert runtime.quote_expiry_driver._quotations is app._quotes
            assert runtime.quote_expiry_driver._limit == 10
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


@pytest.mark.parametrize("lose_lock", [False, True])
async def test_actual_worker_only_lock_owner_scans_expiry_and_stops_on_loss(
    unit_engine, monkeypatch, lose_lock
):
    import asyncio

    from sqlalchemy import text

    from apps.scheduler_worker.main import WorkerStartStatus, run_scheduler_worker
    from tests.integration.test_scheduler_worker import _lock_holder

    monkeypatch.setattr(
        worker, "EnvironmentSecretResolver", lambda _: _TrackingEnvironmentSecrets()
    )
    factory = worker.SchedulerRuntimeFactory(
        worker_environment(unit_engine, new_id("tn"), "enabled"),
        _factory_dependencies(worker, with_hunter=False),
        resolver_factory=_FactoryResolver,
        health_server_factory=_FactoryHealthServer,
    )
    async with factory() as owner, factory() as contender:
        entered, release, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
        calls = []
        scan = owner.quote_expiry_driver.scan_once

        async def held_scan():
            calls.append("owner")
            assert await scan() == 0
            entered.set()
            await release.wait()
            return 0

        async def forbidden_scan():
            pytest.fail("未获锁副本不得扫描expiry")

        async def wait(interval, event):
            if lose_lock:
                pid, _ = await _lock_holder(unit_engine, owner.config.lock_key)
                async with unit_engine.begin() as connection:
                    assert await connection.scalar(
                        text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}
                    )
            else:
                event.set()

        monkeypatch.setattr(owner.quote_expiry_driver, "scan_once", held_scan)
        monkeypatch.setattr(contender.quote_expiry_driver, "scan_once", forbidden_scan)
        task = asyncio.create_task(
            run_scheduler_worker(
                owner, stop_event=stop, wait=wait, install_signal_handlers=False
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            rejected = await run_scheduler_worker(
                contender, install_signal_handlers=False
            )
            assert rejected.status is WorkerStartStatus.NOT_STARTED
            assert not contender.activation.quotation_lifecycle._started
        finally:
            release.set()
        result = await asyncio.wait_for(task, timeout=5)
        assert calls == ["owner"]
        assert result.cycles_completed == 1
        assert result.status is (
            WorkerStartStatus.LOCK_LOST if lose_lock else WorkerStartStatus.STARTED
        )
