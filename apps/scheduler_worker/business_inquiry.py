"""普通询盘模型接力入口：持锁、显式时间范围、现有模型和邮件Gateway。"""

import argparse
import asyncio
import json
import logging
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_runtime.business_inquiry import BusinessInquiryPlanner
from apps.composition_support.model import build_model_composition
from artifact_store.service_impl import RawArtifactStoreImpl
from connectors.object_store.bounded import S3BoundedObjectBlobTransport
from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.deferred import DeferredS3ObjectBlobTransport
from connectors.qq_mail.business_reply import QQBusinessReplyConnector
from connectors.qq_mail.client import QQMailConnector
from domains.assistant.schemas import AssistantActor
from domains.assistant.service_impl import ModelConfigurationServiceImpl
from domains.conversations.business_inquiry import (
    InquiryAutomationScope,
    InquiryReplyScope,
    inquiry_analysis_status,
)
from domains.conversations.business_reply import eligible_reply
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
from infra.db.business_inquiry import (
    InquiryRecords,
    InquirySendPolicy,
    InquirySourcePolicy,
)
from infra.db.business_mail import SqlBusinessMailRepository
from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.db.model_usage import SqlModelUsageRepository
from infra.db.runtime_scope import verify_runtime_database_scope
from infra.db.schema import assert_database_schema_current
from infra.db.sending_identity_uow import SqlAlchemySendingIdentityUnitOfWork
from infra.db.session import create_engine_from
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.pilot.config import PilotConfig, private_read
from infra.standalone.settings import deployment_verification, load_model_settings
from shared.errors import PermissionDenied
from shared.schemas.evidence_read import ObjectReadLimits
from shared.schemas.identifiers import IdempotencyKey, SendingIdentityId, new_id
from shared.schemas.model_invocation import ModelGenerationError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.business_inquiry import InquiryReplyHandler
from tool_gateway.handlers.business_reply import MANIFEST, STAGES, BusinessReplyCheck
from tool_gateway.handlers.business_reply_evidence import BusinessReplyEvidenceReader
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import ToolGateway
from workflows.business_inquiry.flow import BusinessInquiryDriver


class InquiryActorAuthority:
    def __init__(self, records):
        self.records = records

    async def check(self, actor):
        if actor.model_dump() != self.records.scope.actor.model_dump():
            raise PermissionDenied("模型操作者不匹配")
        await self.records.authorize()

    async def require_admin(self, actor):
        raise PermissionDenied("自动跟进不能修改或验证模型配置")


class InquiryModelAuthority:
    """每个模型run绑定已提交的精确来源和当前授权，不能借分类run扩大用途。"""

    def __init__(
        self,
        records,
        original,
        source,
        identity,
        configuration,
        repository,
        settings,
        metadata,
        fingerprints,
    ):
        self.fingerprints = fingerprints
        self.records, self.original, self.source, self.identity = (
            records,
            original,
            source,
            identity,
        )
        self.configuration, self.repository, self.settings, self.metadata = (
            configuration,
            repository,
            settings,
            metadata,
        )

    async def check(self, identity):
        if identity != self.identity or identity.capability != "business_mail_followup":
            raise ModelGenerationError("permission")
        await self.records.authorize()
        key = self.original + ":" + self.source["message_id"]
        claim = await self.records.event(self.original, "inquiry-claim:" + key)
        if claim is None:
            raise ModelGenerationError("permission")
        value = json.loads(claim.note)
        current = await self.metadata(self.source["message_id"])
        followup = self.source["message_id"] != self.original
        if value.get("followup", False) != followup:
            raise ModelGenerationError("permission")
        if followup:
            saved = await self.records.event(
                self.original, "inquiry-scope:" + self.original
            )
            sent = await self.records.event(
                self.original, "inquiry-sent:" + self.original
            )
            if saved is None or sent is None:
                raise ModelGenerationError("permission")
            reply_scope = InquiryReplyScope.model_validate_json(saved.note)
            outgoing = (
                "<"
                + InquiryReplyHandler(reply_scope, self.fingerprints, None)
                .envelope()[0]
                .deterministic_message_id
                + ">"
            )
            if not eligible_reply(reply_scope, self.source["headers"], outgoing):
                raise ModelGenerationError("permission")
        if (
            value["identity"] != identity.model_dump()
            or value["policy_digest"] != self.records.scope.digest
            or value["model"] != self.settings.model
            or current["source_sha256"] != value["source_sha256"]
            or current["revision"] != value["source_revision"]
            or inquiry_analysis_status(current, followup=followup) != "ready"
            or (await self.records.view(self.original)).status == "completed"
        ):
            raise ModelGenerationError("permission")
        snapshot = await self.repository.get(identity.tenant_id)
        if (
            snapshot is None
            or snapshot.configuration_version != self.settings.configuration_version
            or snapshot.model != self.settings.model
            or snapshot.limits != self.settings.limits
            or not snapshot.model_data_export_enabled
        ):
            raise ModelGenerationError("configuration")
        await self.configuration.authorize(
            AssistantActor(**self.records.scope.actor.model_dump()),
            self.settings.configuration_version,
            probe=False,
        )


async def run(args):
    profile = PilotConfig.read(args.profile)
    settings = load_model_settings(args.model)
    scope = InquiryAutomationScope.model_validate_json(private_read(args.scope))
    if (
        profile.tenant_id != scope.actor.tenant_id
        or not args.mailbox_directory.is_absolute()
        or settings.model_catalog_directory is None
        or settings.codex_job_root is None
    ):
        raise PermissionDenied("普通询盘运行范围或公司模型通道不匹配")
    engine = create_engine_from(profile.database_url.get_secret_value())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    lock = PostgresAdvisoryLock(
        engine,
        derive_advisory_lock_key(
            scope.actor.tenant_id, "business-inquiry:" + scope.mailbox_id
        ),
    )
    objects = None
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    try:
        await assert_database_schema_current(engine)
        await verify_runtime_database_scope(engine, scope.actor.tenant_id)
        if not await lock.acquire():
            return 0

        async def heartbeat():
            if not await lock.heartbeat():
                raise PermissionDenied("普通询盘单实例锁已失效")

        binding = CurrentBusinessMailboxBinding(AliyunMailStore(args.mailbox_directory))
        source = SqlBusinessMailRepository(sessions, binding)
        records = InquiryRecords(source, args.scope, scope)
        await records.authorize()
        fingerprints = HmacFingerprintProvider(
            "v1", profile.resolve("PILOT_FINGERPRINT").encode()
        )
        contact_fingerprint = lambda value: fingerprints.fingerprint(
            (b"contact-value-v1", value.encode())
        )[0]
        ledger = lambda tenant: SqlAlchemyToolGatewayUnitOfWork(sessions, tenant)
        object_settings = S3ObjectStoreSettings.from_pilot_environ(
            profile.runtime_environment()
        )
        objects = DeferredS3ObjectBlobTransport(object_settings, profile)
        store = RawArtifactStoreImpl(
            lambda tenant: SqlAlchemyArtifactUnitOfWork(sessions, tenant),
            objects,
            4 * 1024 * 1024,
            lambda: datetime.now(UTC),
            new_id,
            bounded_transport=S3BoundedObjectBlobTransport(
                object_settings,
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

        async def read_source(original, message_id):
            policy = InquirySourcePolicy(records, original, contact_fingerprint)
            return await BusinessReplyEvidenceReader(
                policy, store, fingerprints, ledger
            ).read(message_id)

        configuration_repository = SqlModelConfigurationRepository(
            sessions,
            fingerprints,
            deployment_verification=deployment_verification(settings),
            deployment_provider=settings.provider,
        )
        configuration = ModelConfigurationServiceImpl(
            configuration_repository,
            InquiryActorAuthority(records),
            now=lambda: datetime.now(UTC),
        )

        def plan_factory(original, material, identity):
            source_policy = InquirySourcePolicy(records, original, contact_fingerprint)
            authority = InquiryModelAuthority(
                records,
                original,
                material,
                identity,
                configuration,
                configuration_repository,
                settings,
                source_policy.metadata,
                fingerprints,
            )
            model = build_model_composition(
                settings=settings,
                resolver=profile,
                authority=authority,
                usage=SqlModelUsageRepository(sessions),
                ledger_factory=ledger,
                fingerprints=fingerprints,
                lease_owner="business-inquiry",
                lease_duration=timedelta(seconds=settings.limits.timeout_seconds + 30),
            )
            return BusinessInquiryPlanner(
                model.generator,
                model=settings.model,
                max_output_tokens=settings.limits.max_output_tokens,
            )

        identity_service = SendingIdentityServiceImpl(
            lambda tenant: SqlAlchemySendingIdentityUnitOfWork(
                sessions, tenant, now=lambda: datetime.now(UTC)
            ),
            Phase1SendingIdentityAuthorizer(scope.actor.tenant_id),
            StandardAuditLogger(),
            now=lambda: datetime.now(UTC),
        )
        identity_actor = Actor(
            "business_inquiry",
            SendingIdentityScope(
                level=ScopeLevel.SYSTEM,
                allowed_identity_ids=frozenset({SendingIdentityId(scope.identity_id)}),
            ),
            "system",
        )

        def send_factory(reply_scope):
            policy = InquirySendPolicy(records, reply_scope, contact_fingerprint)

            async def connector():
                await heartbeat()
                await policy.authorize()
                await identity_service.reserve_send_slot(
                    scope.actor.tenant_id,
                    SendingIdentityId(scope.identity_id),
                    IdempotencyKey(reply_scope.key),
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

            handler = InquiryReplyHandler(reply_scope, fingerprints, connector)
            registry = ToolRegistry()
            registry.register(MANIFEST, handler)
            gateway = ToolGateway(
                registry,
                {n: BusinessReplyCheck(n, reply_scope, policy) for n in STAGES},
                ledger,
                lease_duration=timedelta(seconds=120),
                lease_owner="business-inquiry",
                id_factory=new_id,
            )
            return policy, handler, gateway

        driver = BusinessInquiryDriver(
            records,
            read_source,
            plan_factory,
            send_factory,
            heartbeat,
            model=settings.model,
            version=settings.configuration_version,
        )
        if args.preflight:
            permission = await identity_service.check_send_permission(
                scope.actor.tenant_id,
                SendingIdentityId(scope.identity_id),
                False,
                actor=identity_actor,
            )
            await configuration.authorize(
                AssistantActor(**scope.actor.model_dump()),
                settings.configuration_version,
                probe=False,
            )
            print(
                json.dumps(
                    {
                        "status": "preflight_passed"
                        if permission.allowed
                        else "identity_not_ready",
                        "eligible_count": len(await records.list_messages()),
                        "model_calls": 0,
                        "send_calls": 0,
                    }
                ),
                flush=True,
            )
            return 0 if permission.allowed else 2
        previous = {}
        while not stop.is_set():
            await heartbeat()
            for message_id in await records.list_messages():
                if stop.is_set():
                    break
                try:
                    result = await driver.once(message_id)
                except PermissionDenied:
                    raise
                except Exception as error:  # noqa: BLE001 -- 只输出固定类别，不记录邮件或原始异常
                    result = "review_required:" + type(error).__name__
                if previous.get(message_id) != result:
                    print(
                        json.dumps({"message_id": message_id, "status": result}),
                        flush=True,
                    )
                    previous[message_id] = result
            if args.once:
                return 0
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


def main():
    logging.disable(logging.CRITICAL)
    parser = argparse.ArgumentParser(description="普通询盘模型自动跟进")
    for name in ("profile", "model", "scope", "mailbox-directory"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    try:
        return asyncio.run(run(parser.parse_args()))
    except Exception as error:  # noqa: BLE001 -- 私有进程边界固定错误
        print(
            json.dumps({"status": "stopped", "error_type": type(error).__name__}),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
