"""声明 Hunter Provider 的安全部署元数据，不读取凭证或访问 Hunter。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from apps.scheduler_worker.config import HunterContactsSettings
from infra.db.provider_readiness_uow import SqlAlchemyProviderReadinessUnitOfWork
from infra.db.session import create_engine_from
from shared.errors import ValidationError
from shared.schemas.identifiers import IdempotencyKey, TenantId
from tool_gateway.provider_readiness import (
    ProviderReadinessActor,
    ProviderReadinessPermission,
    ProviderReadinessServiceImpl,
)

_INPUT_FAILURE = "Hunter Provider 配置输入无效"
_DECLARATION_FAILURE = "Hunter Provider 配置声明失败"
_INPUT_EXIT_CODE = 2
_DECLARATION_EXIT_CODE = 3


class _ArgumentParser(argparse.ArgumentParser):
    """拒绝参数时不回显用户输入，避免 CLI 意外输出敏感文本。"""

    def error(self, message: str) -> None:
        del message
        raise ValueError(_INPUT_FAILURE)


def _actor_id(argv: Sequence[str] | None) -> str:
    parser = _ArgumentParser(add_help=False)
    parser.add_argument("--actor-id", required=True)
    try:
        parsed = parser.parse_args(argv)
    except (SystemExit, ValueError):
        raise ValidationError(_INPUT_FAILURE) from None
    if not isinstance(parsed.actor_id, str):
        raise ValidationError(_INPUT_FAILURE)
    return parsed.actor_id


async def _declare(
    database_url: str,
    tenant_id: TenantId,
    actor: ProviderReadinessActor,
    settings: HunterContactsSettings,
) -> tuple[str, str]:
    """在已迁移数据库中写入 configured 事实，并确保引擎总被释放。"""
    configuration = settings.configuration
    if not settings.enabled or configuration is None:
        raise ValidationError(_INPUT_FAILURE)
    engine: AsyncEngine = create_engine_from(database_url)
    try:
        session_factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        service = ProviderReadinessServiceImpl(
            lambda requested_tenant: SqlAlchemyProviderReadinessUnitOfWork(
                session_factory, requested_tenant
            ),
            runtime_actor=actor,
        )
        snapshot = await service.declare_configuration(
            tenant_id,
            configuration,
            actor=actor,
            idempotency_key=IdempotencyKey(
                f"hunter-config:{configuration.configuration_version}"
            ),
        )
        return configuration.configuration_version, snapshot.state.value
    finally:
        await engine.dispose()


def main(
    environ: Mapping[str, str] | None = None,
    argv: Sequence[str] | None = None,
) -> int:
    """声明 Hunter 配置；错误仅返回固定分类，绝不展示环境或异常详情。"""
    environment = os.environ if environ is None else environ
    try:
        actor_id = _actor_id(argv)
        if not isinstance(environment, Mapping):
            raise ValidationError(_INPUT_FAILURE)
        database_url = environment["DATABASE_URL"]
        tenant_id = TenantId(environment["TRADEOS_TENANT_ID"])
        if not isinstance(database_url, str):
            raise ValidationError(_INPUT_FAILURE)
        settings = HunterContactsSettings.from_environ(environment)
        if not settings.enabled or settings.configuration is None:
            raise ValidationError(_INPUT_FAILURE)
        actor = ProviderReadinessActor(
            actor_id=actor_id,
            tenant_id=tenant_id,
            permissions=frozenset(
                {
                    ProviderReadinessPermission.CONFIGURE,
                    ProviderReadinessPermission.READ,
                }
            ),
        )
    except Exception:  # noqa: BLE001 - CLI errors must never expose input values.
        print(_INPUT_FAILURE, file=sys.stderr)
        return _INPUT_EXIT_CODE

    try:
        configuration_version, state = asyncio.run(
            _declare(database_url, tenant_id, actor, settings)
        )
    except Exception:  # noqa: BLE001 - storage/engine details must remain private.
        print(_DECLARATION_FAILURE, file=sys.stderr)
        return _DECLARATION_EXIT_CODE
    print(
        json.dumps(
            {
                "provider": "hunter",
                "configuration_version": configuration_version,
                "state": state,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
