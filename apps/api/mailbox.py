"""本人邮箱的独立本机 API；只装配登录、员工身份与只读镜像。"""

from __future__ import annotations

import argparse
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Never

import uvicorn
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.composition_support.employee_readers import employee_service_scope
from domains.conversations.mailbox import MailboxService
from domains.employees.permissions import (
    Actor,
    EmployeeScope,
    Phase1EmployeeAuthorizer,
    StandardAuditLogger,
)
from infra.authentication.service import PostgresAuthentication
from infra.db.mailbox import SqlMailboxRepository
from infra.db.runtime_scope import verify_runtime_database_scope
from infra.db.schema import assert_database_schema_current
from infra.db.session import create_engine_from
from shared.schemas.identifiers import TenantId

from .dependencies import MailboxApiDependencies
from .main import create_app
from .middleware import ApiSettings
from .pilot import mount_web
from .runtime import DatabaseReadinessProbe, RuntimeCleanupError, RuntimeStartupError

if TYPE_CHECKING:
    from infra.pilot.mailbox_config import MailboxConfig


def create_mailbox_app(config: MailboxConfig, web_build: Path) -> FastAPI:
    """构造不连接数据库；schema 核验先于服务，退出时对称释放唯一 engine。"""
    try:
        if not (web_build / "index.html").is_file():
            raise RuntimeStartupError()
        tenant = TenantId(config.tenant_id)
        engine = create_engine_from(config.database_url.get_secret_value())
        sessions = async_sessionmaker(bind=engine, expire_on_commit=False)
        dependencies = MailboxApiDependencies(
            employees=partial(
                employee_service_scope,
                sessions,
                now=lambda: datetime.now(UTC),
                authorizer=Phase1EmployeeAuthorizer(tenant),
                audit=StandardAuditLogger(),
            ),
            employee_lookup_actor=Actor(
                "system:mailbox-identity", EmployeeScope.SYSTEM, "system"
            ),
            mailbox=MailboxService(SqlMailboxRepository(sessions)),
        )
        authentication = PostgresAuthentication(sessions, tenant)
        probe = DatabaseReadinessProbe(engine)

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            del app
            primary: BaseException | None = None
            try:
                try:
                    await assert_database_schema_current(engine)
                    await verify_runtime_database_scope(engine, str(config.tenant_id))
                except Exception:  # noqa: BLE001 schema/驱动错误不得携带私有连接参数
                    raise RuntimeStartupError() from None
                yield
            except BaseException as exc:
                primary = exc
                raise
            finally:
                try:
                    await engine.dispose()
                except BaseException as exc:
                    if primary is None:
                        if not isinstance(exc, Exception):
                            raise
                        raise RuntimeCleanupError() from None

        business = create_app(
            settings=ApiSettings(
                tenant_id=config.tenant_id, dev_mode=False, retry_after_seconds=30
            ),
            dependencies=dependencies,
            lifespan=lifespan,
            readiness_probe=probe,
            authentication=authentication,
            authentication_origin=config.origin,
        )
        business.state.runtime_engine = engine
        business.state.readiness_probe = probe
        return mount_web(business, web_build)
    except Exception:  # noqa: BLE001 装配边界只返回固定错误
        raise RuntimeStartupError() from None


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise RuntimeStartupError()


def main() -> int:
    """父进程只传私有 profile 路径；不继承凭证环境、不回显底层错误。"""
    logging.disable(logging.CRITICAL)
    parser = _SafeParser(description="启动本人邮箱只读 API", allow_abbrev=False)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument(
        "--web-build",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "web/dist",
    )
    try:
        args = parser.parse_args()
        from infra.pilot.mailbox_config import MailboxConfig

        config = MailboxConfig.read(args.profile)
        app = create_mailbox_app(config, args.web_build)
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=config.api_port,
            access_log=False,
            log_config=None,
        )
        return 0
    except Exception:  # noqa: BLE001 监督器不可接收到配置或凭证原文
        print("本人邮箱服务启动失败（mailbox_api_failed）")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
