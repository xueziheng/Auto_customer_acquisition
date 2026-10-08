"""平台与企业生命周期必须一起就绪、一起关闭，不能只挂路由。"""
import asyncio
from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apps.api.pilot import mount_web


def test_platform_mount_precedes_business_and_manages_both_lifespans(tmp_path):
    (tmp_path / "index.html").write_text("<html>TradeOS</html>")
    events = []
    def child(name):
        @asynccontextmanager
        async def lifecycle(app):
            events.append(name + ":start")
            yield
            events.append(name + ":stop")
        app = FastAPI(lifespan=lifecycle)
        @app.get("/who")
        async def who():
            return {"name": name}
        return app
    app = mount_web(child("business"), tmp_path, platform=child("platform"))
    with TestClient(app) as client:
        assert set(events) == {"business:start", "platform:start"}
        assert client.get("/api/platform/who").json() == {"name": "platform"}
        assert client.get("/api/who").json() == {"name": "business"}
    assert events[-2:] == ["business:stop", "platform:stop"]


def test_platform_start_failure_prevents_business_start(tmp_path):
    (tmp_path / "index.html").write_text("<html>TradeOS</html>")
    events = []
    @asynccontextmanager
    async def fail(app):
        raise ValueError("controlled startup failure")
        yield
    @asynccontextmanager
    async def business(app):
        events.append("started")
        yield
    app = mount_web(FastAPI(lifespan=business), tmp_path, platform=FastAPI(lifespan=fail))
    with pytest.raises(ValueError, match="controlled startup failure"), TestClient(app):
        pass
    assert events == []


@pytest.mark.parametrize(
    "primary",
    [None, ValueError("controlled startup failure"), asyncio.CancelledError()],
    ids=["cleanup-only", "startup-and-cleanup", "cancelled-startup-and-cleanup"],
)
async def test_preprobe_cleanup_preserves_primary_and_sanitizes_standalone_failure(
    tmp_path, primary,
):
    """关闭连接的故障不能取代启动失败，也不能把驱动细节暴露给调用方。"""
    (tmp_path / "index.html").write_text("<html>TradeOS</html>")

    class FailingEngine:
        async def dispose(self):
            raise RuntimeError("synthetic driver details")

    @asynccontextmanager
    async def platform_lifecycle(app):
        if primary is not None:
            raise primary
        yield

    business = FastAPI()
    business.state.runtime_engine = FailingEngine()
    app = mount_web(
        business, tmp_path, platform=FastAPI(lifespan=platform_lifecycle),
    )
    expected_type = type(primary) if primary is not None else RuntimeError
    with pytest.raises(expected_type) as failure:
        async with app.router.lifespan_context(app):
            pass
    if primary is not None:
        assert failure.value is primary
    else:
        assert str(failure.value) == "web_database_close_failed"
        assert failure.value.__suppress_context__ is True


async def test_preprobe_cleanup_does_not_translate_cancellation(tmp_path):
    """正常结束时取消清理仍保持取消语义，不伪装为成功或普通错误。"""
    (tmp_path / "index.html").write_text("<html>TradeOS</html>")
    cancelled = asyncio.CancelledError()

    class CancelledEngine:
        async def dispose(self):
            raise cancelled

    business = FastAPI()
    business.state.runtime_engine = CancelledEngine()
    app = mount_web(business, tmp_path, platform=FastAPI())
    with pytest.raises(asyncio.CancelledError) as failure:
        async with app.router.lifespan_context(app):
            pass
    assert failure.value is cancelled
