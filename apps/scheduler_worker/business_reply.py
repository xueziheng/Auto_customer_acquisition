"""精确单封询盘的显式接力入口；无模型调用，不改变全局自动发送开关。"""

import argparse
import asyncio
import json
import logging
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker

from artifact_store.repository import ArtifactUnitOfWork
from artifact_store.service_impl import RawArtifactStoreImpl
from connectors.object_store.bounded import S3BoundedObjectBlobTransport
from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.deferred import DeferredS3ObjectBlobTransport
from connectors.qq_mail.business_reply import QQBusinessReplyConnector
from connectors.qq_mail.client import QQMailConnector
from domains.conversations.business_mail_followup import BusinessMailFollowupService
from domains.conversations.business_reply import BusinessReplyScope
from domains.sending_identity.permissions import (
    Actor,
    Phase1SendingIdentityAuthorizer,
    ScopeLevel,
    SendingIdentityScope,
    StandardAuditLogger,
)
from domains.sending_identity.service_impl import SendingIdentityServiceImpl
from infra.aliyun_mail_store import AliyunMailStore
from infra.business_mail_binding import CurrentBusinessMailboxBinding
from infra.db.advisory_lock import PostgresAdvisoryLock, derive_advisory_lock_key
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.business_mail import SqlBusinessMailRepository
from infra.db.business_mail_followup import SqlBusinessMailFollowupRepository
from infra.db.business_reply import CurrentBusinessReplyPolicy
from infra.db.runtime_scope import verify_runtime_database_scope
from infra.db.schema import assert_database_schema_current
from infra.db.sending_identity_uow import SqlAlchemySendingIdentityUnitOfWork
from infra.db.session import create_engine_from
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.pilot.config import PilotConfig, private_read
from shared.errors import PermissionDenied
from shared.schemas.evidence_read import ObjectReadLimits
from shared.schemas.identifiers import (
    IdempotencyKey,
    SendingIdentityId,
    TenantId,
    new_id,
)
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.business_reply import (
    MANIFEST,
    STAGES,
    BusinessReplyCheck,
    BusinessReplyHandler,
)
from tool_gateway.handlers.business_reply_evidence import BusinessReplyEvidenceReader
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import ToolGateway, _GatewayRegistry
from tool_gateway.repository import ToolGatewayUnitOfWork
from workflows.business_reply.flow import BusinessReplyDriver


async def run(args) -> int:
    profile = PilotConfig.read(args.profile)
    scope = BusinessReplyScope.model_validate_json(private_read(args.scope))
    if (
        profile.tenant_id != scope.actor.tenant_id
        or not args.mailbox_directory.is_absolute()
    ):
        raise PermissionDenied("询盘运行范围不匹配")
    engine = create_engine_from(profile.database_url.get_secret_value())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    lock = PostgresAdvisoryLock(
        engine,
        derive_advisory_lock_key(
            scope.actor.tenant_id, "business-reply:" + scope.message_id
        ),
    )
    objects = None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        await assert_database_schema_current(engine)
        await verify_runtime_database_scope(engine, scope.actor.tenant_id)
        if not await lock.acquire():
            print(json.dumps({"status": "already_running"}))
            return 0

        async def heartbeat():
            if not await lock.heartbeat():
                raise PermissionDenied("接力单实例锁失效")

        binding = CurrentBusinessMailboxBinding(AliyunMailStore(args.mailbox_directory))
        source = SqlBusinessMailRepository(sessions, binding)
        followup = BusinessMailFollowupService(
            SqlBusinessMailFollowupRepository(source)
        )
        fingerprints = HmacFingerprintProvider(
            "v1", profile.resolve("PILOT_FINGERPRINT").encode()
        )
        policy = CurrentBusinessReplyPolicy(
            source,
            args.scope,
            scope,
            lambda value: fingerprints.fingerprint(
                (b"contact-value-v1", value.encode())
            )[0],
        )
        def identity_uow(tenant_id: TenantId) -> SqlAlchemySendingIdentityUnitOfWork:
            return SqlAlchemySendingIdentityUnitOfWork(sessions, tenant_id, now=lambda: datetime.now(UTC))

        identity_service = SendingIdentityServiceImpl(
            identity_uow,
            Phase1SendingIdentityAuthorizer(scope.actor.tenant_id),
            StandardAuditLogger(),
            now=lambda: datetime.now(UTC),
        )
        identity_actor = Actor(
            "business_reply",
            SendingIdentityScope(
                level=ScopeLevel.SYSTEM,
                allowed_identity_ids=frozenset({SendingIdentityId(scope.identity_id)}),
            ),
            "system",
        )
        def ledger(tenant_id: TenantId) -> ToolGatewayUnitOfWork:
            return SqlAlchemyToolGatewayUnitOfWork(sessions, tenant_id)

        def artifact_uow(tenant_id: TenantId) -> ArtifactUnitOfWork:
            return SqlAlchemyArtifactUnitOfWork(sessions, tenant_id)
        settings = S3ObjectStoreSettings.from_pilot_environ(
            profile.runtime_environment()
        )
        objects = DeferredS3ObjectBlobTransport(settings, profile)
        store = RawArtifactStoreImpl(
            artifact_uow,
            objects,
            4 * 1024 * 1024,
            lambda: datetime.now(UTC),
            new_id,
            bounded_transport=S3BoundedObjectBlobTransport(
                settings,
                profile,
                limits=ObjectReadLimits(
                    connect_timeout_ms=2000,
                    read_timeout_ms=2000,
                    total_timeout_ms=10000,
                    maximum_attempts=1,
                    chunk_bytes=65536,
                ),
            ),
        )
        evidence = BusinessReplyEvidenceReader(policy, store, fingerprints, ledger)

        async def connector():
            await heartbeat()
            await policy.authorize()
            await identity_service.reserve_send_slot(
                scope.actor.tenant_id,
                SendingIdentityId(scope.identity_id),
                IdempotencyKey(scope.key),
                False,
                actor=identity_actor,
            )
            await policy.authorize()
            password = binding.resolver(
                scope.actor, scope.sender, scope.binding_operation_id
            ).resolve("BUSINESS_QQ_MAIL")
            return QQBusinessReplyConnector(
                QQMailConnector(scope.sender, SecretStr(password))
            )

        handler = BusinessReplyHandler(scope, fingerprints, connector)
        registry = ToolRegistry()
        registry.register(MANIFEST, handler)
        # 此具名注册只含上述已校验manifest/handler；Pipeline使用只读的结构化端口。
        gateway = ToolGateway(
            cast(_GatewayRegistry, registry),
            {n: BusinessReplyCheck(n, scope, policy) for n in STAGES},
            ledger,
            lease_duration=timedelta(seconds=120),
            lease_owner="business_reply",
            id_factory=new_id,
        )
        driver = BusinessReplyDriver(
            scope, policy, followup, evidence, gateway, handler, heartbeat
        )
        if args.preflight:
            await policy.authorize()
            permission = await identity_service.check_send_permission(
                scope.actor.tenant_id,
                SendingIdentityId(scope.identity_id),
                False,
                actor=identity_actor,
            )
            if not permission.allowed:
                print(
                    json.dumps(
                        {"status": "identity_not_ready", "reason": permission.reason}
                    )
                )
                return 2
            original = await evidence.read(scope.message_id)
            print(
                json.dumps(
                    {
                        "status": "preflight_passed",
                        "message_id": scope.message_id,
                        "source_sha256": original["source_sha256"],
                        "send_calls": 0,
                        "model_calls": 0,
                    }
                )
            )
            return 0
        previous = None
        while not stop.is_set():
            result = await driver.once(reconcile=args.reconcile)
            safe = {k: v for k, v in result.items() if k != "evidence"}
            if safe != previous:
                print(json.dumps(safe, ensure_ascii=False), flush=True)
                previous = safe
            if args.once or result["status"] in {
                "owner_review",
                "needs_reconciliation",
            }:
                return 0 if result["status"] != "needs_reconciliation" else 2
            try:
                await asyncio.wait_for(stop.wait(), timeout=20)
            except TimeoutError:
                pass
        return 0
    finally:
        if objects is not None:
            await objects.aclose()
        await lock.close()
        await engine.dispose()


def main() -> int:
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description="单封普通询盘接力")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--scope", type=Path, required=True)
    parser.add_argument("--mailbox-directory", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--reconcile", action="store_true")
    try:
        return asyncio.run(run(parser.parse_args()))
    except Exception as error:  # noqa: BLE001 -- 进程边界只输出固定类别，禁止回显凭证或供应商异常
        print(
            json.dumps({"status": "stopped", "error_type": type(error).__name__}),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
