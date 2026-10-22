"""普通业务来信独立运行模式：当前QQowner、单实例锁、先保存后分类。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_runtime.business_mail import BusinessMailClassifier
from apps.composition_support.model import build_model_composition
from artifact_store.repository import ArtifactUnitOfWork
from artifact_store.service_impl import RawArtifactStoreImpl
from connectors.object_store.bounded import S3BoundedObjectBlobTransport
from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.deferred import DeferredS3ObjectBlobTransport
from connectors.qq_mail.business_inbound import QQBusinessInboundConnector
from connectors.qq_mail.inbound_transport import QQImapInboundTransport
from domains.assistant.schemas import AssistantActor
from domains.assistant.service import ModelConfigurationService
from domains.assistant.service_impl import ModelConfigurationServiceImpl
from domains.conversations.business_mail import BusinessMailActor
from infra.aliyun_mail_store import AliyunMailStore
from infra.business_mail_binding import CurrentBusinessMailboxBinding
from infra.db.advisory_lock import PostgresAdvisoryLock, derive_advisory_lock_key
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.business_mail import SqlBusinessMailRepository
from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.db.model_usage import SqlModelUsageRepository
from infra.db.runtime_scope import verify_runtime_database_scope
from infra.db.schema import assert_database_schema_current
from infra.db.session import create_engine_from
from infra.db.tables import ModelInvocationRow
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.email_inbound_artifacts import InboundRawArtifactArchiver
from infra.pilot.config import PilotConfig, private_read
from infra.standalone.settings import deployment_verification, load_model_settings
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.email_inbound import MIME_BYTES
from shared.schemas.evidence_read import ObjectReadLimits
from shared.schemas.identifiers import new_id
from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelGenerationError,
    ModelLimits,
)
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.business_mail import GatewayBusinessMailReader
from tool_gateway.repository import ToolGatewayUnitOfWork

logger = logging.getLogger(__name__)


class BusinessMailScope(BaseModel):
    """无凭证的明确启动授权，必须精确绑定当前连接操作；不借用QQ测试grant。"""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    enabled: Literal[True]
    actor: BusinessMailActor
    address: str = Field(pattern=r"^[0-9]{5,12}@qq\.com$")
    binding_operation_id: str = Field(min_length=1, max_length=40)
    mailbox_directory: Path = Field(repr=False, exclude=True)
    initial_uid_window: int = Field(strict=True, ge=1, le=2000)
    page_size: int = Field(strict=True, ge=1, le=50)
    poll_seconds: int = Field(strict=True, ge=15, le=300)


class _ActorAuthority:
    def __init__(
        self,
        repository: SqlBusinessMailRepository,
        actor: BusinessMailActor,
        mailbox: str,
    ) -> None:
        self.repository, self.actor, self.mailbox = repository, actor, mailbox

    async def check(self, actor: AssistantActor) -> None:
        if actor.model_dump() != self.actor.model_dump():
            raise PermissionDenied("普通来信模型操作者不符")
        await self.repository.authorize(self.actor, self.mailbox)

    async def require_admin(self, actor: AssistantActor) -> None:
        raise PermissionDenied("普通收件进程不能管理模型配置")


class BusinessMailModelAuthority:
    """持久mail run、当前owner、现有导出许可与配额共享同一原模型Gateway。"""

    def __init__(
        self,
        repository: SqlBusinessMailRepository,
        actor: BusinessMailActor,
        configuration: ModelConfigurationService,
        model: str,
        version: str,
        *,
        configuration_repository: SqlModelConfigurationRepository,
        limits: ModelLimits,
    ) -> None:
        self.configuration_repository, self.limits = configuration_repository, limits
        self.repository, self.actor, self.configuration, self.model, self.version = (
            repository,
            actor,
            configuration,
            model,
            version,
        )

    async def check(self, identity: InvocationIdentity) -> None:
        expected = {
            **self.actor.model_dump(),
            "capability": "business_mail_triage",
            "configuration_version": self.version,
            "turn_id": None,
        }
        if any(getattr(identity, key) != value for key, value in expected.items()):
            raise ModelGenerationError("permission")
        try:
            snapshot = await self.configuration_repository.get(identity.tenant_id)
            if (
                snapshot is None
                or snapshot.configuration_version != self.version
                or snapshot.model != self.model
                or snapshot.limits != self.limits
                or not snapshot.model_data_export_enabled
            ):
                raise ModelGenerationError("configuration")
            async with self.repository.sessions() as db:
                prior = (
                    await db.scalars(
                        select(ModelInvocationRow).where(
                            ModelInvocationRow.tenant_id == identity.tenant_id,
                            ModelInvocationRow.run_id == identity.run_id,
                        )
                    )
                ).all()
                for row in prior:
                    if (
                        row.user_id,
                        row.employee_id,
                        row.capability,
                        row.configuration_version,
                        row.sequence,
                        row.model,
                    ) != (
                        identity.user_id,
                        identity.employee_id,
                        identity.capability,
                        identity.configuration_version,
                        identity.sequence,
                        self.model,
                    ):
                        raise ModelGenerationError("permission")
            await self.repository.validate_claim(
                self.actor,
                str(identity.run_id),
                self.model,
                self.version,
                sequence=identity.sequence,
                allow_corrected=any(
                    row.state in {"dispatched", "succeeded"} for row in prior
                ),
            )
            await self.configuration.authorize(
                AssistantActor(**self.actor.model_dump()), self.version, probe=False
            )
        except PermissionDenied:
            raise ModelGenerationError("permission") from None


class BusinessMailDriver:
    """由调用者持锁；先追完当前有界历史，再按真实IMAP接收时间从新到旧识别。"""

    def __init__(
        self,
        repository: SqlBusinessMailRepository,
        actor: BusinessMailActor,
        mailbox: str,
        reader: GatewayBusinessMailReader,
        classifier: BusinessMailClassifier,
        lock: PostgresAdvisoryLock,
        *,
        model: str,
        version: str,
        stop: asyncio.Event,
        limits: ModelLimits,
    ) -> None:
        (
            self.repository,
            self.actor,
            self.mailbox,
            self.reader,
            self.classifier,
            self.lock,
        ) = repository, actor, mailbox, reader, classifier, lock
        self.model, self.version, self.stop = model, version, stop
        self.limits = limits

    async def heartbeat(self) -> None:
        if not await self.lock.heartbeat():
            raise PermissionDenied("普通收件单实例锁已失效")

    async def once(self) -> int:
        """不在API开后台任务；任一未确认整页失败都保留原游标。"""
        pages = 0
        snapshot_end = None
        while not self.stop.is_set():
            await self.heartbeat()
            expected = await self.repository.checkpoint(self.actor, self.mailbox)
            page = await self.reader.fetch(
                uidvalidity=expected.uidvalidity,
                after_uid=expected.after_uid,
                before_uid=snapshot_end,
            )
            if snapshot_end is None:
                snapshot_end = page.snapshot_end_uid
            await self.heartbeat()
            await self.repository.apply_page(self.actor, expected, page)
            pages += 1
            if page.next_uid == page.after_uid or (
                snapshot_end is not None and page.next_uid >= snapshot_end
            ):
                break
        classified = 0
        while not self.stop.is_set():
            await self.heartbeat()
            available, _ = await self.repository.model_availability(
                self.actor, self.mailbox, self.limits
            )
            if available != "ready":
                break
            claim = await self.repository.claim_next(
                self.actor, self.mailbox, model=self.model, version=self.version
            )
            if claim is None:
                break
            identity = InvocationIdentity.model_validate(dict(
                **self.actor.model_dump(),
                run_id=claim.run_id,
                capability="business_mail_triage",
                configuration_version=self.version,
                sequence=claim.sequence,
            ))
            failure: str | None = None
            result = None
            try:
                result = await self.classifier.classify(
                    identity, subject=claim.subject, body=claim.body_text
                )
            except ModelGenerationError as exc:
                failure = exc.code
            except ValidationError:
                failure = "credential_marker"
            except asyncio.CancelledError:
                # 已提交claim和模型预留；重启只展示未知，不重放外发。
                raise
            except Exception:  # noqa: BLE001 -- 固定错误，不能保存正文/SDK异常
                failure = "unknown"
            await self.heartbeat()
            blocked_until = None
            if failure == "quota":
                _, blocked_until = await self.repository.model_availability(
                    self.actor, self.mailbox, self.limits
                )
                if await self.repository.defer_unattempted_quota(self.actor, claim):
                    break
            await self.repository.finish(
                self.actor,
                claim,
                result=result,
                failure=failure,
                model_blocked_until=blocked_until,
            )
            classified += 1
            if failure in {
                "quota",
                "configuration",
                "permission",
                "unknown",
                "rate_limit",
                "provider_error",
            }:
                break
        return classified


async def run(
    profile_file: Path, model_file: Path, scope_file: Path, *, once: bool = False
) -> int:
    profile = PilotConfig.read(profile_file)
    settings = load_model_settings(model_file)
    try:
        scope = BusinessMailScope.model_validate_json(private_read(scope_file))
    except Exception:  # noqa: BLE001 -- 私有配置错误必须脱敏
        raise ValueError("普通收件授权配置无效") from None
    if (
        scope.actor.tenant_id != profile.tenant_id
        or not scope.mailbox_directory.is_absolute()
        or ".." in scope.mailbox_directory.parts
    ):
        raise ValueError("普通收件运行企业或目录无效")
    # 强制复用公司Codex绑定，绝不通过脚本或普通SDK直连模型。
    if settings.model_catalog_directory is None or settings.codex_job_root is None:
        raise ValueError("普通收件必须配置现有Codex公司模型")
    engine = create_engine_from(profile.database_url.get_secret_value())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    lock = PostgresAdvisoryLock(
        engine,
        derive_advisory_lock_key(
            scope.actor.tenant_id, "business-mail:" + scope.address
        ),
    )
    objects = None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    try:
        await assert_database_schema_current(engine)
        await verify_runtime_database_scope(engine, scope.actor.tenant_id)
        if not await lock.acquire():
            return 0
        binding = CurrentBusinessMailboxBinding(
            AliyunMailStore(scope.mailbox_directory)
        )
        repository = SqlBusinessMailRepository(sessions, binding)
        mailbox = await repository.register(
            scope.actor, address=scope.address, operation=scope.binding_operation_id
        )
        await repository.recover_claims(scope.actor, mailbox)
        fingerprints = HmacFingerprintProvider(
            "v1", profile.resolve("PILOT_FINGERPRINT").encode()
        )
        configuration_repository = SqlModelConfigurationRepository(
            sessions,
            fingerprints,
            deployment_verification=deployment_verification(settings),
            deployment_provider=settings.provider,
        )
        configuration = ModelConfigurationServiceImpl(
            configuration_repository,
            _ActorAuthority(repository, scope.actor, mailbox),
            now=lambda: datetime.now(UTC),
        )
        authority = BusinessMailModelAuthority(
            repository,
            scope.actor,
            configuration,
            settings.model,
            settings.configuration_version,
            configuration_repository=configuration_repository,
            limits=settings.limits,
        )
        ledger = lambda tenant: cast(
            ToolGatewayUnitOfWork, SqlAlchemyToolGatewayUnitOfWork(sessions, tenant)
        )
        model = build_model_composition(
            settings=settings,
            resolver=profile,
            authority=authority,
            usage=SqlModelUsageRepository(sessions),
            ledger_factory=ledger,
            fingerprints=fingerprints,
            lease_owner="business-mail",
            lease_duration=timedelta(seconds=settings.limits.timeout_seconds + 30),
        )
        classifier = BusinessMailClassifier(
            model.generator,
            model=settings.model,
            max_output_tokens=settings.limits.max_output_tokens,
        )
        object_settings = S3ObjectStoreSettings.from_pilot_environ(
            profile.runtime_environment()
        )
        objects = DeferredS3ObjectBlobTransport(object_settings, profile)
        raw_store = RawArtifactStoreImpl(
            lambda tenant: cast(
                ArtifactUnitOfWork, SqlAlchemyArtifactUnitOfWork(sessions, tenant)
            ),
            objects,
            MIME_BYTES,
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

        async def authorize() -> None:
            await repository.authorize(scope.actor, mailbox)

        reader = GatewayBusinessMailReader(
            tenant=scope.actor.tenant_id,
            user=scope.actor.user_id,
            mailbox=mailbox,
            reader=lambda: QQBusinessInboundConnector(
                QQImapInboundTransport(
                    scope.address,
                    binding.resolver(
                        scope.actor, scope.address, scope.binding_operation_id
                    ),
                    "BUSINESS_QQ_MAIL",
                ),
                initial_uid_window=scope.initial_uid_window,
                page_size=scope.page_size,
            ),
            archiver=InboundRawArtifactArchiver(
                raw_store, raw_store, before_archive=authorize
            ),
            authorize=authorize,
            fingerprints=fingerprints,
            ledger_factory=ledger,
            lease_owner="business-mail",
        )
        driver = BusinessMailDriver(
            repository,
            scope.actor,
            mailbox,
            reader,
            classifier,
            lock,
            model=settings.model,
            version=settings.configuration_version,
            stop=stop,
            limits=settings.limits,
        )
        while not stop.is_set():
            try:
                await driver.once()
            except PermissionDenied:
                logger.error("普通收件当前授权已失效，停止进程")
                return 1
            except Exception:  # noqa: BLE001 -- 外部读取是只读；保留游标，不记录原始异常
                logger.error("普通收件本轮未完成，保留原游标")
                if once:
                    return 1
            if once:
                return 0
            try:
                await asyncio.wait_for(stop.wait(), timeout=scope.poll_seconds)
            except TimeoutError:
                pass
        return 0
    finally:
        if objects is not None:
            await objects.aclose()
        await lock.close()
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="TradeOS普通业务来信")
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--scope", required=True, type=Path)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args.profile, args.model, args.scope, once=args.once))
    except Exception:  # noqa: BLE001 -- 不输出私有路径、凭证或异常链
        logger.error("普通业务来信启动失败")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
