"""企业资料的进程内机械装配；网络只经现有 model.generate Gateway。"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.enterprise_knowledge.analyze import (
    KnowledgeAnalysisResult,
    analyze_knowledge,
)
from apps.composition_support.model import build_model_composition
from artifact_store.repository import ArtifactUnitOfWorkFactory
from artifact_store.service_impl import RawArtifactStoreImpl
from artifact_store.store import BoundedRawArtifactStore
from connectors.codex.client import (
    CodexCliProvider,
    CodexRunnerSettings,
    encode_codex_input,
)
from connectors.deepseek.client import DeepSeekClient
from connectors.files.knowledge import (
    KnowledgeFileParser,
    KnowledgeParseFailure,
    KnowledgeParseLimits,
)
from connectors.gmail.client import SecretResolver
from connectors.object_store.bounded import S3BoundedObjectBlobTransport
from connectors.object_store.config import S3ObjectStoreSettings
from connectors.object_store.deferred import DeferredS3ObjectBlobTransport
from connectors.obsidian.vault import TenantMarkdownVault
from connectors.obsidian.workspace import KnowledgeTaskScope
from domains.assistant.schemas import AssistantActor
from domains.assistant.service_impl import ModelConfigurationServiceImpl
from domains.products.schemas import KnowledgeClaim, KnowledgeDocumentDetail
from domains.products.service import (
    EnterpriseKnowledgeService,
    EnterpriseKnowledgeServiceImpl,
)
from infra.db.artifact_uow import SqlAlchemyArtifactUnitOfWork
from infra.db.enterprise_knowledge import SqlAlchemyKnowledgeUnitOfWork
from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.db.model_usage import SqlModelUsageRepository
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.pilot.config import PilotConfig
from infra.standalone.knowledge_settings import KnowledgeSettings
from infra.standalone.settings import StandaloneModelSettings
from shared.errors import InvalidStateTransition, PermissionDenied, ValidationError
from shared.schemas.evidence_read import ObjectReadLimits
from shared.schemas.identifiers import EmployeeId, RunId, TenantId, UserId, new_id
from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelGenerationError,
    ModelGenerationPort,
    ModelRequest,
    ModelResponse,
)
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.repository import ToolGatewayUnitOfWork
from workflows.enterprise_knowledge.markdown import render_knowledge
from workflows.enterprise_knowledge.processor import (
    EnterpriseKnowledgeDriver,
    KnowledgeInput,
    KnowledgeProcessingFailure,
)


class _ClaimIdentity:
    """当前上传任务授权同时约束模型配置检查，不接受模型指定身份。"""
    def __init__(self, service: EnterpriseKnowledgeService, claim: KnowledgeClaim) -> None:
        self._service, self._claim = service, claim

    async def check(self, actor: AssistantActor) -> None:
        claim = self._claim
        if (actor.tenant_id, actor.employee_id, actor.user_id) != (
            claim.tenant_id, claim.actor_id, claim.user_id,
        ):
            raise PermissionDenied("企业资料模型身份无效")
        await self._service.authorize_processing(claim.tenant_id, claim)

    async def require_admin(self, actor: AssistantActor) -> None:
        raise PermissionDenied("企业资料任务不得修改模型配置")

class KnowledgeModelAuthority:
    """每次Gateway检查都重读当前任务、员工、部署配置及模型就绪状态。"""
    def __init__(
        self, service: EnterpriseKnowledgeService, claim: KnowledgeClaim,
        configuration: ModelConfigurationServiceImpl, identity: InvocationIdentity,
    ) -> None:
        self._service, self._claim = service, claim
        self._configuration, self._identity = configuration, identity

    async def check(self, identity: InvocationIdentity) -> None:
        if identity != self._identity or identity.capability != "knowledge_ingest":
            raise ModelGenerationError("permission")
        try:
            await self._service.authorize_processing(identity.tenant_id, self._claim)
            await self._configuration.authorize(
                AssistantActor(tenant_id=identity.tenant_id, user_id=identity.user_id, employee_id=identity.employee_id),
                identity.configuration_version, probe=False,
            )
        except (PermissionDenied, InvalidStateTransition):
            raise ModelGenerationError("permission") from None

class _SourceReader:
    def __init__(self, store: BoundedRawArtifactStore, maximum_bytes: int) -> None:
        self._store, self._maximum = store, maximum_bytes

    async def read(self, claim: KnowledgeClaim) -> bytes:
        source = claim.source
        if source.size_bytes > self._maximum:
            raise KnowledgeProcessingFailure("source_limit_exceeded")
        meta, content = await self._store.get_bounded(
            claim.tenant_id, source.artifact_id, maximum_bytes=self._maximum,
        )
        if (
            meta.tenant_id != claim.tenant_id or meta.artifact_id != source.artifact_id
            or meta.mime_type != source.mime_type or meta.size_bytes != source.size_bytes
            or meta.content_hash != source.sha256 or len(content) != source.size_bytes
            or hashlib.sha256(content).hexdigest() != source.sha256
        ):
            raise KnowledgeProcessingFailure("source_integrity_failed")
        return content

class _Parser:
    def __init__(self, parser: KnowledgeFileParser) -> None:
        self._parser = parser

    async def parse(self, content: bytes, mime_type: str) -> KnowledgeInput:
        try:
            result = await self._parser.extract(content, mime_type)
        except KnowledgeParseFailure as error:
            limit = error.reason in {"size_limit", "text_limit", "page_limit", "archive_limit", "source_limit_exceeded"}
            raise KnowledgeProcessingFailure("source_limit_exceeded" if limit else "source_unsupported") from None
        return KnowledgeInput(result.text, result.images, result.image_source_pages, result.warnings)

class _Exporter:
    def __init__(self, vault: TenantMarkdownVault) -> None:
        self._vault = vault

    async def export(self, detail: KnowledgeDocumentDetail) -> None:
        revision, doc = detail.revision, detail.document
        status = doc.status
        if revision is None or (status != "awaiting_confirmation" and status != "confirmed"):
            raise ValidationError("资料分析版本不存在")
        await self._vault.publish(
            tenant_id=str(doc.tenant_id), document_id=doc.document_id,
            version=doc.version, status=status,
            source_text=(
                "图片来源说明：请查看不可变原件。\nArtifact: " + str(doc.source.artifact_id)
                + "\nSHA256: " + doc.source.sha256
                + "\nAI转录仅见 Docs 中的分析版本，不是原件文本。"
                if revision.source_kind == "vision_transcription" else revision.source_text
            ),
            markdown=render_knowledge(detail),
        )

class _KnowledgeInputBudget:
    """在 Gateway 预留额度前校验 CLI 实际封套容量，不裁剪资料或改变请求。"""

    def __init__(self, gateway: ModelGenerationPort, *, max_input_bytes: int) -> None:
        if type(max_input_bytes) is not int or max_input_bytes <= 0:
            raise ValueError("企业资料输入容量无效")
        self._gateway = gateway
        self._maximum = max_input_bytes

    async def generate(
        self, identity: InvocationIdentity, request: ModelRequest,
    ) -> ModelResponse:
        if len(encode_codex_input(request)) > self._maximum:
            raise KnowledgeProcessingFailure("source_limit_exceeded")
        return await self._gateway.generate(identity, request)

class _Analyzer:
    def __init__(
        self, *, service: EnterpriseKnowledgeService, sessions: async_sessionmaker[AsyncSession],
        settings: StandaloneModelSettings, knowledge: KnowledgeSettings,
        resolver: SecretResolver, fingerprints: HmacFingerprintProvider, instance_id: str,
    ) -> None:
        self._service, self._sessions = service, sessions
        self._settings, self._knowledge = settings, knowledge
        self._resolver, self._fingerprints, self._instance = resolver, fingerprints, instance_id

    async def analyze(self, claim: KnowledgeClaim, source: KnowledgeInput) -> KnowledgeAnalysisResult:
        settings, knowledge = self._settings, self._knowledge
        identity = InvocationIdentity(
            tenant_id=claim.tenant_id, user_id=UserId(claim.user_id),
            employee_id=EmployeeId(claim.actor_id), run_id=RunId(claim.job_id),
            capability="knowledge_ingest", configuration_version=settings.configuration_version,
            sequence=0,
        )
        configuration = ModelConfigurationServiceImpl(
            SqlModelConfigurationRepository(self._sessions, self._fingerprints),
            _ClaimIdentity(self._service, claim), now=lambda: datetime.now(UTC),
        )
        authority = KnowledgeModelAuthority(self._service, claim, configuration, identity)
        scope = KnowledgeTaskScope(
            tenant_id=str(claim.tenant_id), employee_id=str(claim.actor_id), run_id=claim.job_id,
        )
        runner_settings = CodexRunnerSettings(
            codex_binary=knowledge.codex_binary, bwrap_binary=Path("/usr/bin/bwrap"),
            python_binary=Path("/usr/bin/python3"), job_root=knowledge.root / "jobs",
            timeout_seconds=min(knowledge.timeout_seconds, settings.limits.timeout_seconds),
            max_input_bytes=min(settings.limits.max_input_bytes, 1024 * 1024),
            max_output_bytes=1024 * 1024,
        )
        composition = build_model_composition(
            settings=settings, resolver=self._resolver, authority=authority,
            usage=SqlModelUsageRepository(self._sessions),
            ledger_factory=lambda tenant: cast(
                ToolGatewayUnitOfWork, SqlAlchemyToolGatewayUnitOfWork(self._sessions, tenant),
            ),
            fingerprints=self._fingerprints, lease_owner=self._instance,
            lease_duration=timedelta(seconds=settings.limits.timeout_seconds + 30),
            provider_factory=lambda: CodexCliProvider(
                DeepSeekClient(settings.secret_ref, self._resolver, timeout_seconds=settings.limits.timeout_seconds),
                settings=runner_settings, scope=scope,
            ),
        )
        try:
            return await analyze_knowledge(
                _KnowledgeInputBudget(
                    composition.generator, max_input_bytes=runner_settings.max_input_bytes,
                ),
                identity, source.text, model_name=settings.model, images=source.images,
                image_source_pages=source.image_source_pages,
                max_output_tokens=settings.limits.max_output_tokens,
            )
        except ModelGenerationError as error:
            if error.code == "output_limit":
                raise KnowledgeProcessingFailure("source_limit_exceeded") from None
            if error.code in {"unknown", "rate_limit", "provider_error"}:
                raise KnowledgeProcessingFailure("model_result_unknown", uncertain=True) from None
            reason = "authorization_revoked" if error.code == "permission" else "provider_unavailable"
            raise KnowledgeProcessingFailure(reason) from None

def build_knowledge_driver(
    *, profile: PilotConfig, settings: StandaloneModelSettings,
    knowledge: KnowledgeSettings, model_resolver: SecretResolver,
    sessions: async_sessionmaker[AsyncSession], fingerprints: HmacFingerprintProvider,
    instance_id: str,
) -> EnterpriseKnowledgeDriver:
    """独立启用配置才组装；构造不读取凭证、不联网、不创建任务目录。"""
    if not knowledge.enabled:
        raise ValueError("企业资料未启用")
    tenant = TenantId(profile.tenant_id)
    service = EnterpriseKnowledgeServiceImpl(lambda requested: SqlAlchemyKnowledgeUnitOfWork(sessions, requested))
    objects = S3ObjectStoreSettings.from_pilot_environ(profile.runtime_environment())
    store = RawArtifactStoreImpl(
        cast(ArtifactUnitOfWorkFactory, lambda requested: SqlAlchemyArtifactUnitOfWork(sessions, requested)),
        DeferredS3ObjectBlobTransport(objects, profile),
        objects.raw_max_bytes, lambda: datetime.now(UTC), new_id,
        bounded_transport=S3BoundedObjectBlobTransport(
            objects, profile, limits=ObjectReadLimits(
                connect_timeout_ms=5000, read_timeout_ms=10000, total_timeout_ms=20000,
                chunk_bytes=65536, maximum_attempts=1,
            ),
        ),
    )
    return EnterpriseKnowledgeDriver(
        tenant_id=tenant, service=service,
        reader=_SourceReader(store, knowledge.max_upload_bytes),
        parser=_Parser(KnowledgeFileParser(
            python_binary=Path("/usr/bin/python3"), bwrap_binary=Path("/usr/bin/bwrap"),
            limits=KnowledgeParseLimits(max_bytes=knowledge.max_upload_bytes, max_chars=knowledge.max_text_chars),
        )),
        analyzer=_Analyzer(
            service=service, sessions=sessions, settings=settings, knowledge=knowledge,
            resolver=model_resolver, fingerprints=fingerprints, instance_id=instance_id,
        ),
        exporter=_Exporter(TenantMarkdownVault(knowledge.root / "vaults")),
        lease_owner=instance_id, lease_seconds=settings.limits.timeout_seconds + 180,
        model_name=settings.model,
    )
