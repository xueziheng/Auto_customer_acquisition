"""受控探针验证生命周期顺序；不代替Linux真实受限解析验收。"""

import asyncio
from types import SimpleNamespace

import pytest

from apps.api.composition.quotations import QuotationRuntimeLifecycle
from shared.schemas.evidence_read import EvidenceParserCapability


class Parser:
    def __init__(self, *, failure=None, fault=None, close_fault=None):
        self.calls = []
        self.failure, self.fault, self.close_fault = failure, fault, close_fault
        self.entered = asyncio.Event()

    async def probe(self):
        self.calls.append("probe")
        self.entered.set()
        if self.fault == "blocked":
            await asyncio.Event().wait()
        if self.fault is not None:
            raise self.fault
        return EvidenceParserCapability(
            status="available" if self.failure is None else "unavailable",
            profile_version="evidence-worker-v1",
            limits_hash="a" * 64,
            runtime_hash="b" * 64,
            failure=self.failure,
        )

    async def aclose(self):
        self.calls.append("close")
        if self.close_fault is not None:
            raise self.close_fault


@pytest.mark.parametrize(
    "failure", [None, "platform", "runtime", "resource", "protocol"]
)
async def test_startup_once_preserves_real_capability_degradation(failure):
    parser = Parser(failure=failure)
    lifecycle = QuotationRuntimeLifecycle(parser)
    assert parser.calls == []
    await asyncio.gather(lifecycle.startup(), lifecycle.startup())
    assert parser.calls == ["probe"]
    await lifecycle.aclose()
    await lifecycle.aclose()
    assert parser.calls == ["probe", "close"]


@pytest.mark.parametrize("close_fault", [None, RuntimeError("private close")])
async def test_unexpected_probe_failure_closes_and_raises_fixed_error(close_fault):
    parser = Parser(fault=RuntimeError("private startup"), close_fault=close_fault)
    lifecycle = QuotationRuntimeLifecycle(parser)
    with pytest.raises(RuntimeError, match="^报价运行依赖启动失败$"):
        await lifecycle.startup()
    assert parser.calls == ["probe", "close"]


async def test_startup_cancellation_closes_and_preserves_cancelled_error():
    parser = Parser(fault="blocked", close_fault=RuntimeError("private cleanup"))
    lifecycle = QuotationRuntimeLifecycle(parser)
    task = asyncio.create_task(lifecycle.startup())
    await parser.entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert parser.calls == ["probe", "close"]


async def test_closed_lifecycle_cannot_restart_probe():
    parser = Parser()
    lifecycle = QuotationRuntimeLifecycle(parser)
    await lifecycle.aclose()
    with pytest.raises(RuntimeError, match="^报价运行依赖启动失败$"):
        await lifecycle.startup()
    assert parser.calls == ["close"]


@pytest.mark.parametrize("failure", [None, "schema", "probe", "cancel", "body"])
@pytest.mark.parametrize("cleanup_failure", [None, "ordinary", "cancel"])
async def test_actual_api_lifespan_closes_quotation_before_database_even_before_yield(
    monkeypatch, failure, cleanup_failure
):
    from apps.api import runtime
    from tests.unit.test_api_runtime_config import _VALID_ENV

    events = []

    async def schema(engine):
        events.append("schema")
        if failure == "schema":
            raise RuntimeError("controlled schema failure")

    class Lifecycle:
        async def startup(self):
            events.append("probe")
            if failure == "probe":
                raise RuntimeError("controlled probe failure")
            if failure == "cancel":
                raise asyncio.CancelledError()

        async def aclose(self):
            events.append("close")
            if cleanup_failure == "ordinary":
                raise RuntimeError("private cleanup detail")
            if cleanup_failure == "cancel":
                raise asyncio.CancelledError()

    class Engine:
        async def dispose(self):
            events.append("dispose")

    # 仅替代本测试不关心的装配；被测为真实零参数factory生成的lifespan。
    monkeypatch.setattr(runtime.os, "environ", dict(_VALID_ENV))
    monkeypatch.setattr(runtime, "create_engine_from", lambda _: Engine())
    monkeypatch.setattr(runtime, "async_sessionmaker", lambda **_: object())
    monkeypatch.setattr(
        runtime.S3ObjectStoreSettings, "from_environ", lambda _: object()
    )
    monkeypatch.setattr(
        runtime,
        "build_phase1_dependencies",
        lambda *a, **kw: SimpleNamespace(
            quotation=SimpleNamespace(lifecycle=Lifecycle())
        ),
    )
    monkeypatch.setattr(runtime, "assert_database_schema_current", schema)
    monkeypatch.setattr(
        runtime,
        "create_app",
        lambda **kw: SimpleNamespace(
            state=SimpleNamespace(),
            router=SimpleNamespace(lifespan_context=kw["lifespan"]),
        ),
    )
    app = runtime.create_runtime_app()

    async def enter():
        async with app.router.lifespan_context(app):
            events.append("yield")
            if failure == "body":
                raise RuntimeError("controlled body failure")

    if failure is None and cleanup_failure != "cancel":
        await enter()
    else:
        with pytest.raises(
            asyncio.CancelledError
            if failure == "cancel" or failure is None
            else RuntimeError
        ):
            await enter()
    expected = ["schema"]
    if failure != "schema":
        expected.append("probe")
    if failure in {None, "body"}:
        expected.append("yield")
    assert events == [*expected, "close", "dispose"]
