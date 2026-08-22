"""显式配置的 Phase 1 API 进程 runtime factory。"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from connectors.object_store.config import S3ObjectStoreSettings
from infra.db.schema import (
    DatabaseSchemaError,
)
from infra.db.schema import (
    assert_database_schema_current as _assert_database_schema_current,
)
from infra.db.session import create_engine_from
from infra.secrets import EnvironmentSecretResolver

from .composition.runtime import build_phase1_dependencies
from .main import create_app
from .middleware import ApiSettings
from .runtime_config import (
    Phase1RuntimeSettings,
    RuntimeConfigurationError,
)

logger = logging.getLogger(__name__)


class RuntimeStartupError(RuntimeError):
    """固定、脱敏的 runtime 启动错误。"""

    def __init__(self) -> None:
        super().__init__("API runtime 启动检查失败")


class DatabaseReadinessProbe:
    """只验证数据库是否能执行固定轻量查询。"""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def is_ready(self) -> bool:
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001 数据库驱动细节不得穿透 readiness 边界
            return False


async def assert_database_schema_current(engine: AsyncEngine) -> None:
    """复用 infra schema probe，并保持 API 固定错误契约。"""
    try:
        await _assert_database_schema_current(engine)
    except DatabaseSchemaError:
        raise RuntimeStartupError() from None


def create_runtime_app() -> FastAPI:
    """读取显式配置并装配一次真实 runtime；数据库 IO 延迟到 lifespan。"""
    try:
        settings = Phase1RuntimeSettings.from_environ(os.environ)
    except RuntimeConfigurationError as exc:
        logger.error(
            "API runtime 配置无效",
            extra={
                "config_name": exc.field_name,
                "error_type": type(exc).__name__,
            },
        )
        raise
    try:
        engine = create_engine_from(settings.database_url.get_secret_value())
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        object_store_settings = S3ObjectStoreSettings.from_environ(os.environ)
        dependencies = build_phase1_dependencies(
            settings,
            factory,
            now=lambda: datetime.now(UTC),
            secret_resolver=EnvironmentSecretResolver(os.environ),
            object_store_settings=object_store_settings,
        )
    except Exception as exc:  # noqa: BLE001 装配异常只记录类型并固定映射
        logger.error(
            "API runtime 装配失败",
            extra={"error_type": type(exc).__name__},
        )
        raise RuntimeStartupError() from None
    probe = DatabaseReadinessProbe(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        try:
            await assert_database_schema_current(engine)
            yield
        finally:
            try:
                await engine.dispose()
            except Exception as exc:  # noqa: BLE001 清理失败不得覆盖启动/退出异常
                logger.error(
                    "API runtime 数据库资源释放失败",
                    extra={"error_type": type(exc).__name__},
                )

    app = create_app(
        settings=ApiSettings(
            tenant_id=settings.tenant_id,
            dev_mode=settings.dev_mode,
            retry_after_seconds=settings.retry_after_seconds,
        ),
        dependencies=dependencies,
        lifespan=lifespan,
        cors_allowed_origins=settings.cors_allowed_origins,
        readiness_probe=probe,
    )
    app.state.runtime_engine = engine
    app.state.readiness_probe = probe
    return app
