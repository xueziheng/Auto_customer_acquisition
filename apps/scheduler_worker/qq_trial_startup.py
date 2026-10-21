"""原 standalone 的单次 QQ 准备装配；真实采集经 Gateway，恢复只认原键原证据。"""
import asyncio
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from apps.composition_support.email_inbound import (
    InboundProviderAdapter,
    InboundRuntimePorts,
    qq_adapter,
)
from artifact_store.repository import ArtifactUnitOfWork
from artifact_store.service_impl import RawArtifactStoreImpl
from connectors._deadline_process import PreparationDeadline
from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.qq_preparation import QQPreparationObjects
from connectors.qq_mail.delivery_collect import (
    DeliveryReadRequest,
    QQDeliveryAliasCollector,
)
from connectors.qq_mail.sent_alias import verify_sent_alias
from connectors.qq_mail.sent_collect import QQSentAliasCollector, SentReadRequest
from domains.conversations.aliyun_connection import AliyunActor
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.qq_alias import provision_trial, record_verified_alias
from infra.db.qq_preparation import QQPreparationDatabase
from infra.db.qq_startup_scope import QQStartupScope
from infra.db.qq_trial import SqlQQTrialGate
from infra.db.qq_trial_startup import QQStartupFacts
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.email_inbound_artifacts import InboundRawArtifactArchiver
from infra.pilot.config import PilotConfig
from infra.qq_inbound_binding import CurrentQQBinding
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    IdempotencyKey,
    TenantId,
    UserId,
    new_id,
)
from shared.schemas.qq_trial import QQTrialScope, QQTrialStopped
from tool_gateway.checks.idempotency import IdempotencyCheck
from tool_gateway.errors import ToolCallStatus
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.qq_delivery_alias import (
    MANIFEST as DELIVERY_MANIFEST,
)
from tool_gateway.handlers.qq_delivery_alias import (
    DeliveryAliasCheck,
    DeliveryAliasReadHandler,
)
from tool_gateway.handlers.qq_sent_alias import (
    MANIFEST,
    SentAliasCheck,
    SentAliasReadHandler,
)
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import ToolCallContext, ToolGateway
from tool_gateway.repository import ToolGatewayUnitOfWork


class QQTrialStartup:
    def __init__(self, profile: PilotConfig, settings: StandaloneModelSettings) -> None:
        qq = settings.qq_reply
        if qq is None or qq.trial is None or qq.trial.route.tenant_id != profile.tenant_id:
            raise ValueError("QQ 启动租户绑定无效")
        self.profile, self.settings, self.qq, self.launch = profile,settings,qq,qq.trial
        self.gate: SqlQQTrialGate | None = None
        self.sessions: async_sessionmaker[AsyncSession] | None = None
        self.startup_scope: QQStartupScope | None = None

    def compose(self, sessions: async_sessionmaker[AsyncSession]) -> InboundProviderAdapter:
        """仅构造同一门禁和适配器；未获原 scheduler 锁时零邮箱 IO。"""
        if self.sessions is not None:
            raise QQTrialStopped("runtime_already_bound")
        self.sessions = sessions
        tenant = TenantId(self.profile.tenant_id)
        binding = CurrentQQBinding(self.qq.mailbox_directory,
            AliyunActor(tenant_id=tenant,employee_id=EmployeeId(self.qq.employee_id),
                user_id=UserId(self.qq.user_id)),self.qq.address)
        facts = QQStartupFacts(sessions,self.qq,self.settings,binding)
        self.gate = SqlQQTrialGate(sessions,None,facts.authorize)
        return qq_adapter(self.qq,tenant,sessions,trial_gate=self.gate,trial_commit=self.gate.commit_scan)

    async def activate(self) -> None:
        if self.sessions is None or self.gate is None:
            raise QQTrialStopped("runtime_not_composed")
        if self.startup_scope is None:
            await self.run_with_inbound(self._activate)
        else:
            await self._activate()

    async def run_with_inbound(self, callback: Callable[[], Awaitable[None]]) -> None:
        if self.sessions is None or self.startup_scope is not None:
            raise QQTrialStopped("runtime_not_composed")
        scope = QQStartupScope(self.sessions.kw["bind"], PreparationDeadline(self.launch.sent.timeout_seconds))
        self.startup_scope = scope
        try:
            await scope.run(callback)
        finally:
            self.startup_scope = None

    async def _activate(self) -> None:
        assert self.startup_scope is not None and self.gate is not None
        deadline = self.startup_scope.deadline
        database = QQPreparationDatabase(self.profile.database_url.get_secret_value(), deadline)
        try:
            scope = await self._prepare(database.factory, deadline)
            deadline.remaining()
            self.gate.bind_scope(scope)
            await self.gate.check()
            deadline.remaining()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 超时/数据库中止不泄露底层连接或 SDK 细节。
            raise QQTrialStopped("preparation_failed") from None
        finally:
            await database.close()

    async def _prepare(self, sessions: async_sessionmaker[AsyncSession],
                       deadline: PreparationDeadline) -> QQTrialScope:
        assert self.gate is not None
        tenant = TenantId(self.profile.tenant_id)
        qq, launch = self.qq,self.launch
        assert qq.attempt_id is not None
        binding = CurrentQQBinding(qq.mailbox_directory,
            AliyunActor(tenant_id=tenant,employee_id=EmployeeId(qq.employee_id),user_id=UserId(qq.user_id)),qq.address)
        facts = QQStartupFacts(sessions,qq,self.settings,binding)
        async def authorized() -> bool:
            deadline.remaining()
            result = await facts.authorize()
            if result and facts.authorization_deadline is not None:
                deadline.shorten(facts.authorization_deadline)
            deadline.remaining()
            return result

        if not await authorized():
            raise QQTrialStopped("scope_changed")
        existing = await facts.existing()
        delivery = launch.delivery
        source: Literal["controlled_recipient_imap", "sender_sent"] = (
            "controlled_recipient_imap" if delivery else "sender_sent")
        reader = UserId(delivery.user_id if delivery else qq.user_id)
        objects_settings = S3ObjectStoreSettings.from_pilot_environ(self.profile.runtime_environment())
        objects = QQPreparationObjects(objects_settings,self.profile,deadline)
        now = lambda: datetime.now(UTC)
        raw = RawArtifactStoreImpl(
            lambda t: cast(ArtifactUnitOfWork,SqlAlchemyArtifactUnitOfWork(sessions,t)),
            objects,launch.sent.header_bytes,now,new_id,
            bounded_transport=objects)
        fingerprints = HmacFingerprintProvider("v1",self.profile.resolve("PILOT_FINGERPRINT").encode())

        async def require_current() -> None:
            if not await authorized():
                raise QQTrialStopped("scope_changed")

        collection_handler: DeliveryAliasReadHandler | SentAliasReadHandler
        checker: type[DeliveryAliasCheck | SentAliasCheck]
        archiver = InboundRawArtifactArchiver(raw,raw,before_archive=require_current)
        if delivery is not None:
            request = DeliveryReadRequest(launch.canonical_header,launch.canonical_header[1:].split("@")[0],
                qq.address,qq.recipient,delivery.folder,delivery.uidvalidity,delivery.uid,
                delivery.timeout_seconds,delivery.header_bytes)
            if facts.delivery_binding is None:
                raise QQTrialStopped("scope_changed")
            collection_handler = DeliveryAliasReadHandler(tenant=tenant,user=reader,
                attempt=qq.attempt_id,request=request,authorization_binding=launch.model_dump_json().encode(),
                authorize=authorized,collector=QQDeliveryAliasCollector(facts.delivery_binding,authorized,deadline=deadline),
                archiver=archiver,fingerprints=fingerprints,deadline=deadline)
            manifest, checker = DELIVERY_MANIFEST, DeliveryAliasCheck
            idem = "qq-delivery-alias:"+qq.attempt_id
        else:
            sent_request = SentReadRequest(launch.canonical_header,launch.canonical_header[1:].split("@")[0],
                qq.address,qq.recipient,**launch.sent.model_dump())
            collection_handler = SentAliasReadHandler(tenant=tenant,user=reader,
                attempt=qq.attempt_id,request=sent_request,
                authorization_binding=launch.model_dump_json(exclude={"delivery"}).encode(),
                authorize=authorized,collector=QQSentAliasCollector(binding,authorized,deadline=deadline),
                archiver=archiver,fingerprints=fingerprints,deadline=deadline)
            manifest, checker = MANIFEST, SentAliasCheck
            idem = "qq-sent-alias:"+qq.attempt_id
        registry = ToolRegistry()
        registry.register(manifest,collection_handler)
        # 原 ToolRegistry 的冻结 manifest 与 Gateway 内部可写 Protocol 存在既有类型差异。
        gateway = ToolGateway(registry,{  # type: ignore[arg-type]
            name: checker(name,tenant,reader,qq.attempt_id,authorized)
            for name in ("tenant","permission")
        } | {"idempotency":IdempotencyCheck()},
            lambda t: cast(ToolGatewayUnitOfWork,SqlAlchemyToolGatewayUnitOfWork(sessions,t)),
            lease_owner="qq-sent-"+self.profile.owner,
            lease_duration=timedelta(seconds=launch.sent.timeout_seconds+30),now=now,id_factory=new_id)
        try:
            result = await gateway.invoke(ToolCallContext(tenant_id=tenant,user_id=reader,
                tool_id=manifest.tool_id,params={"attempt_id":qq.attempt_id},
                idempotency_key=IdempotencyKey(idem)))
            if result.status not in {ToolCallStatus.SUCCEEDED,ToolCallStatus.DUPLICATE} or result.output is None:
                raise QQTrialStopped("alias_collection_not_succeeded")
            artifact = result.output.get("provider_ref")
            call = result.duplicate_of or result.tool_call_id
            if not isinstance(artifact,str) or call is None:
                raise QQTrialStopped("alias_provenance_missing")
            await require_current()
            meta,content = await raw.get_bounded(tenant,ArtifactId(artifact),maximum_bytes=launch.sent.header_bytes)
            evidence = verify_sent_alias((content,),source=source,canonical_header=launch.canonical_header,
                idempotency_header=launch.canonical_header[1:].split("@")[0],sender=qq.address,recipient=qq.recipient)
            if meta.content_hash != evidence.headers_sha256:
                raise QQTrialStopped("alias_provenance_missing")
            scope = QQTrialScope(**launch.model_dump(exclude={"enabled","sent","delivery"}),
                alias_source=source,delivery_employee_id=delivery.employee_id if delivery else None,
                delivery_user_id=delivery.user_id if delivery else None,
                attempt_id=qq.attempt_id,employee_id=EmployeeId(qq.employee_id),user_id=UserId(qq.user_id),
                classifier_employee_id=EmployeeId(qq.classifier_employee_id),model=self.settings.model,
                model_configuration_version=self.settings.configuration_version,
                provider_header=evidence.provider_header)
            if existing is not None and existing != scope:
                raise QQTrialStopped("scope_changed")
        finally:
            await objects.aclose(abort=sys.exc_info()[0] is not None)
        # 所有阻塞 IO 及 SDK 收尾确认完成，才允许登记/激活；收尾失败不能迟到启用。
        await require_current()
        await record_verified_alias(sessions,scope,evidence,artifact_id=artifact,
            tool_call_id=str(call),authorize=authorized)
        if existing is None:
            await provision_trial(sessions,scope,authorized,preparation_collected=True)
        deadline.remaining()
        return scope


@dataclass(frozen=True, repr=False, kw_only=True)
class QQTrialRuntimePorts(InboundRuntimePorts):
    """仅 QQ trial 的启动生命周期；不扩展共享 provider 契约。"""

    startup: QQTrialStartup
