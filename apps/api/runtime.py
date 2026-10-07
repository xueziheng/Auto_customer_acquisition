"""显式配置的 Phase 1 API 进程 runtime factory。"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from agent_runtime.model_client import StructuredJsonModelClient
from apps.composition_support.email_inbound import InboundMailbox
from connectors.gmail.client import SecretResolver
from connectors.gmail.transport import GmailHttpTransport
from connectors.object_store.config import S3ObjectStoreSettings
from infra.db.repositories.opportunities import assert_handoff_reminder_compatibility
from infra.db.runtime_scope import verify_runtime_database_scope
from infra.db.schema import (
    DatabaseSchemaError,
)
from infra.db.schema import (
    assert_database_schema_current as _assert_database_schema_current,
)
from infra.db.session import create_engine_from
from infra.secrets import EnvironmentSecretResolver
from shared.authentication import AuthenticationService
from shared.schemas.identifiers import TenantId
from shared.schemas.runtime_capabilities import RuntimeCapability

from .authentication import AuthenticationCookieSettings
from .composition.assistant import AssistantApiComposition
from .composition.runtime import ManualSendComposition, build_phase1_dependencies
from .dependencies import ConfiguredApiDependencies
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


class RuntimeCleanupError(RuntimeError):
    """资源释放结果未知时让监督器收到固定非成功退出。"""

    def __init__(self) -> None:
        super().__init__("API runtime 资源释放失败")


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
        object_store_settings = S3ObjectStoreSettings.from_environ(os.environ)
    except Exception:  # noqa: BLE001 配置边界不得泄漏对象存储参数
        raise RuntimeStartupError() from None
    return create_runtime_app_from_settings(
        settings,
        secret_resolver=EnvironmentSecretResolver(os.environ),
        object_store_settings=object_store_settings,
    )


def create_runtime_app_from_settings(
    settings: Phase1RuntimeSettings,
    *,
    secret_resolver: SecretResolver,
    object_store_settings: S3ObjectStoreSettings,
    model_client: StructuredJsonModelClient | None = None,
    manual_send: ManualSendComposition | None = None,
    gmail_transport: GmailHttpTransport | None = None,
    inbound_mailbox: InboundMailbox | None = None,
    authentication: AuthenticationService | None = None,
    authentication_origin: str | None = None,
    authentication_cookie: AuthenticationCookieSettings | None = None,
    assistant_factory: Callable[[async_sessionmaker[AsyncSession], ConfiguredApiDependencies], AssistantApiComposition] | None = None,
) -> FastAPI:
    """按显式配置与端口装配；旧模型仅借给开发测试，生产模型须由 assistant Gateway 装配。

    构造严格无连接或 SDK/解析器启动；失败时没有已打开资源需要新事件循环。
    """
    try:
        engine = create_engine_from(settings.database_url.get_secret_value())
        factory = async_sessionmaker(bind=engine, expire_on_commit=False)
        dependencies = build_phase1_dependencies(
            settings,
            factory,
            now=lambda: datetime.now(UTC),
            secret_resolver=secret_resolver,
            object_store_settings=object_store_settings,
            model_client=model_client,
            manual_send=manual_send,
            gmail_transport=gmail_transport,
            inbound_mailbox=inbound_mailbox,
        )
        assistant = assistant_factory(factory, dependencies) if assistant_factory is not None else None
        if assistant is not None:
            dependencies = replace(
                dependencies,
                assistant=assistant.service,
                model_configuration=assistant.configuration,
                trade_manager=None,
                runtime_capabilities=(
                    *(capability for capability in dependencies.runtime_capabilities
                      if capability.name not in {"builtin_assistant", "model"}),
                    RuntimeCapability(name="builtin_assistant", status="enabled", reason="composed"),
                    RuntimeCapability(name="model", status="enabled", reason="composed"),
                ),
            )
    except Exception as exc:  # noqa: BLE001 装配异常只记录类型并固定映射
        logger.error("API runtime 装配失败", extra={"error_type": type(exc).__name__})
        raise RuntimeStartupError() from None
    probe = DatabaseReadinessProbe(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        del app
        primary: BaseException | None = None
        try:
            await assert_database_schema_current(engine)
            await verify_runtime_database_scope(engine, str(settings.tenant_id))
            await assert_handoff_reminder_compatibility(
                factory,
                TenantId(settings.tenant_id),
                None
                if settings.owner_reminder_interval is None
                else settings.owner_reminder_interval // timedelta(seconds=1),
            )
            if assistant is not None:
                await assistant.lifecycle.startup()
            if dependencies.quotation is not None:
                await dependencies.quotation.lifecycle.startup()
            yield
        except BaseException as exc:
            primary = exc
            raise
        finally:
            cleanup_error: BaseException | None = None
            resources = (
                assistant.lifecycle if assistant is not None else None,
                dependencies.email_inbound,
                dependencies.quotation.lifecycle
                if dependencies.quotation is not None
                else None,
                dependencies.model_lifecycle,
                dependencies.object_store_lifecycle,
            )
            for resource in resources:
                if resource is None:
                    continue
                try:
                    await resource.aclose()
                except BaseException as exc:  # noqa: BLE001 取消也不能阻断后续清理
                    if cleanup_error is None or not isinstance(exc, Exception):
                        cleanup_error = exc
                    logger.error(
                        "API runtime 资源释放失败",
                        extra={"error_type": type(exc).__name__},
                    )
            try:
                await engine.dispose()
            except BaseException as exc:  # noqa: BLE001 清理失败不得覆盖主异常
                if cleanup_error is None or not isinstance(exc, Exception):
                    cleanup_error = exc
                logger.error(
                    "API runtime 数据库资源释放失败",
                    extra={"error_type": type(exc).__name__},
                )
            if primary is None and cleanup_error is not None:
                if not isinstance(cleanup_error, Exception):
                    raise cleanup_error
                raise RuntimeCleanupError() from None

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
        authentication=authentication,
        authentication_origin=authentication_origin,
        authentication_cookie=authentication_cookie,
    )
    app.state.runtime_engine = engine
    app.state.readiness_probe = probe
    return app
