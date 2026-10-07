"""经 Tool Gateway 显式验证当前 Hunter Provider 配置。"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from typing import Never, cast

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from apps.scheduler_worker.config import HunterContactsSettings
from connectors.hunter.client import HunterConnector
from connectors.hunter.transport import HunterApiHttpTransport, HunterHttpTransport
from infra.db.provider_readiness_uow import SqlAlchemyProviderReadinessUnitOfWork
from infra.db.session import create_engine_from
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.secrets import EnvironmentSecretResolver
from shared.errors import ValidationError
from shared.schemas.identifiers import IdempotencyKey, TenantId, UserId, new_id
from tool_gateway.checks.idempotency import IdempotencyCheck
from tool_gateway.checks.permission import PermissionCheck
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.provider_validation import (
    PROVIDER_VALIDATION_MANIFEST,
    ProviderValidationHandler,
    ProviderValidationRateLimitCheck,
)
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import (
    CheckRejection,
    ToolCallContext,
    ToolCallResult,
    ToolGateway,
    ToolInvocationState,
)
from tool_gateway.provider_readiness import (
    HUNTER_CONTACT_CAPABILITIES,
    ProviderReadinessActor,
    ProviderReadinessPermission,
    ProviderReadinessServiceImpl,
    require_provider_validation_key,
)
from tool_gateway.repository import (
    ToolGatewayUnitOfWork,
    ToolGatewayUnitOfWorkFactory,
)

_SAFE_ARGUMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}\Z")
_INPUT_FAILURE = "Hunter Provider 验证输入无效"
_EXECUTION_FAILURE = "Hunter Provider 验证不可用"
_INPUT_EXIT_CODE = 2
_EXECUTION_EXIT_CODE = 3


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        del message
        raise ValueError(_INPUT_FAILURE)


class _ProviderValidationTenantCheck:
    name = "tenant"

    def __init__(self, tenant_id: TenantId) -> None:
        self._tenant_id = tenant_id

    async def check(
        self,
        ctx: ToolCallContext,
        state: ToolInvocationState,
    ) -> CheckRejection | None:
        del state
        if ctx.tenant_id == self._tenant_id:
            return None
        return CheckRejection(
            self.name,
            "provider_validation:tenant_binding",
            "Provider 验证租户绑定无效",
        )


def _arguments(argv: Sequence[str] | None) -> tuple[str, IdempotencyKey]:
    parser = _ArgumentParser(add_help=False)
    parser.add_argument("--actor-id", required=True)
    parser.add_argument("--idempotency-key", required=True)
    try:
        parsed = parser.parse_args(argv)
        actor_id = parsed.actor_id
        idempotency_key = parsed.idempotency_key
    except (SystemExit, ValueError):
        raise ValidationError(_INPUT_FAILURE) from None
    if (
        not isinstance(actor_id, str)
        or len(actor_id) > 32
        or _SAFE_ARGUMENT.fullmatch(actor_id) is None
        or not isinstance(idempotency_key, str)
        or _SAFE_ARGUMENT.fullmatch(idempotency_key) is None
    ):
        raise ValidationError(_INPUT_FAILURE)
    try:
        validation_key = require_provider_validation_key(idempotency_key)
    except ValidationError:
        raise ValidationError(_INPUT_FAILURE) from None
    return actor_id, validation_key


def _lease_seconds(environ: Mapping[str, str]) -> int:
    try:
        value = int(environ["TRADEOS_TOOL_LEASE_SECONDS"])
    except (KeyError, TypeError, ValueError):
        raise ValidationError(_INPUT_FAILURE) from None
    if not 1 <= value <= 86_400:
        raise ValidationError(_INPUT_FAILURE)
    return value


async def _validate(
    database_url: str,
    tenant_id: TenantId,
    actor_id: str,
    idempotency_key: IdempotencyKey,
    settings: HunterContactsSettings,
    environ: Mapping[str, str],
    transport_factory: Callable[[], HunterHttpTransport],
    lease_seconds: int,
) -> ToolCallResult:
    configuration = settings.configuration
    secret_ref = settings.secret_ref
    if not settings.enabled or configuration is None or secret_ref is None:
        raise ValidationError(_INPUT_FAILURE)
    engine: AsyncEngine = create_engine_from(database_url)
    try:
        factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        actor = ProviderReadinessActor(
            actor_id,
            tenant_id,
            frozenset(
                {
                    ProviderReadinessPermission.READ,
                    ProviderReadinessPermission.VALIDATE,
                }
            ),
        )
        readiness = ProviderReadinessServiceImpl(
            lambda requested: SqlAlchemyProviderReadinessUnitOfWork(
                factory, requested
            ),
            runtime_actor=actor,
        )
        current = await readiness.get_snapshot(
            tenant_id,
            HUNTER_CONTACT_CAPABILITIES,
            actor=actor,
        )
        if current.configuration != configuration:
            raise ValidationError("Provider 当前配置不匹配")

        secrets = EnvironmentSecretResolver(environ)
        try:
            fingerprint_ref = environ["TOOL_CALL_FINGERPRINT_KEY_REF"]
            fingerprint_version = environ["TOOL_CALL_FINGERPRINT_KEY_VERSION"]
        except (KeyError, TypeError):
            raise ValidationError(_INPUT_FAILURE) from None
        fingerprint_key = secrets.resolve(fingerprint_ref).encode("utf-8")
        fingerprints = HmacFingerprintProvider(
            fingerprint_version,
            fingerprint_key,
        )

        def actor_provider(ctx: ToolCallContext) -> ProviderReadinessActor:
            if (
                ctx.tenant_id != tenant_id
                or str(ctx.user_id) != actor_id
                or ctx.tool_id != PROVIDER_VALIDATION_MANIFEST.tool_id
            ):
                raise ValidationError("Provider validation actor 绑定无效")
            return actor

        def connector_factory(requested: TenantId) -> HunterConnector:
            if requested != tenant_id:
                raise ValidationError("Hunter connector 租户不匹配")
            return HunterConnector(transport_factory())

        handler = ProviderValidationHandler(
            readiness,
            connector_factory,
            secrets,
            secret_ref,
            actor_provider,
            fingerprints,
        )
        registry = ToolRegistry()
        registry.register(PROVIDER_VALIDATION_MANIFEST, handler)

        async def authorize(
            ctx: ToolCallContext,
            state: ToolInvocationState,
        ) -> bool:
            del state
            return (
                ctx.tenant_id == tenant_id
                and str(ctx.user_id) == actor_id
                and ctx.tool_id == PROVIDER_VALIDATION_MANIFEST.tool_id
            )

        def tool_uow(requested: TenantId) -> ToolGatewayUnitOfWork:
            return cast(
                ToolGatewayUnitOfWork,
                SqlAlchemyToolGatewayUnitOfWork(factory, requested),
            )

        gateway = ToolGateway(
            registry,  # type: ignore[arg-type]
            {
                "tenant": _ProviderValidationTenantCheck(tenant_id),
                "permission": PermissionCheck(authorize),
                "idempotency": IdempotencyCheck(),
                "rate_limit": ProviderValidationRateLimitCheck(
                    readiness, actor_provider
                ),
            },
            cast(ToolGatewayUnitOfWorkFactory, tool_uow),
            lease_duration=timedelta(seconds=lease_seconds),
            lease_owner="provider_validation_cli",
            id_factory=new_id,
        )
        return await gateway.invoke(
            ToolCallContext(
                tenant_id,
                UserId(actor_id),
                PROVIDER_VALIDATION_MANIFEST.tool_id,
                {
                    "configuration_version": configuration.configuration_version
                },
                idempotency_key=idempotency_key,
            )
        )
    finally:
        await engine.dispose()


def _safe_output(
    result: ToolCallResult,
    configuration_version: str,
) -> tuple[dict[str, str], bool]:
    tool_call_id = result.tool_call_id
    if not isinstance(tool_call_id, str):
        raise ValidationError(_EXECUTION_FAILURE)
    if result.status in {ToolCallStatus.SUCCEEDED, ToolCallStatus.DUPLICATE}:
        provider_ref = None if result.output is None else result.output.get(
            "provider_ref"
        )
        if not isinstance(provider_ref, str):
            raise ValidationError(_EXECUTION_FAILURE)
        return (
            {
                "tool_id": PROVIDER_VALIDATION_MANIFEST.tool_id,
                "status": "validation_passed",
                "tool_call_id": tool_call_id,
                "configuration_version": configuration_version,
            },
            True,
        )
    category = result.error_category or ToolErrorCategory.UNEXPECTED
    return (
        {
            "tool_id": PROVIDER_VALIDATION_MANIFEST.tool_id,
            "category": category.value,
            "tool_call_id": tool_call_id,
            "configuration_version": configuration_version,
        },
        False,
    )


def main(
    environ: Mapping[str, str] | None = None,
    argv: Sequence[str] | None = None,
    *,
    transport_factory: Callable[[], HunterHttpTransport] | None = None,
) -> int:
    """验证当前配置；所有失败只输出固定字段与分类。"""
    environment = os.environ if environ is None else environ
    try:
        if not isinstance(environment, Mapping):
            raise ValidationError(_INPUT_FAILURE)
        actor_id, idempotency_key = _arguments(argv)
        database_url = environment["DATABASE_URL"]
        tenant_id = TenantId(environment["TRADEOS_TENANT_ID"])
        if not isinstance(database_url, str):
            raise ValidationError(_INPUT_FAILURE)
        settings = HunterContactsSettings.from_environ(environment)
        configuration = settings.configuration
        if not settings.enabled or configuration is None:
            raise ValidationError(_INPUT_FAILURE)
        lease_seconds = _lease_seconds(environment)
        build_transport = transport_factory or HunterApiHttpTransport
        if not callable(build_transport):
            raise ValidationError(_INPUT_FAILURE)
    except Exception:  # noqa: BLE001 - CLI 输入不得被异常回显。
        print(_INPUT_FAILURE, file=sys.stderr)
        return _INPUT_EXIT_CODE

    try:
        result = asyncio.run(
            _validate(
                database_url,
                tenant_id,
                actor_id,
                idempotency_key,
                settings,
                environment,
                build_transport,
                lease_seconds,
            )
        )
        output, succeeded = _safe_output(
            result,
            configuration.configuration_version,
        )
    except Exception:  # noqa: BLE001 - 存储、凭证和 transport 细节不得穿透。
        print(_EXECUTION_FAILURE, file=sys.stderr)
        return _EXECUTION_EXIT_CODE
    print(
        json.dumps(output, ensure_ascii=False, separators=(",", ":")),
        file=sys.stdout if succeeded else sys.stderr,
    )
    return 0 if succeeded else _EXECUTION_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
