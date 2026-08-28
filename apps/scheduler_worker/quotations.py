"""本scheduler进程的显式报价依赖束；不复用手工发送Gateway。"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from connectors.evidence_text.client import LinuxEvidenceTextParser
from connectors.quote_pdf.client import ReportLabQuotePdfRenderer
from domains.approvals.service import ApprovalService, QuoteApprovalAccess
from domains.costing.approval_policy import CostingApprovalPolicyReaderImpl
from domains.costing.freeze_service import CostingFreezeServiceImpl
from domains.costing.quote_service import CostingQuoteServiceImpl
from domains.costing.service import (
    CostingFreezeService,
    CostingFreezeUowFactory,
    CostingQuoteService,
    CostingUnitOfWorkFactory,
    CostScopeSourceAccess,
    PricingEvidenceReader,
)
from domains.demand.service import (
    NeedUnitAuthorizer,
    NeedUnitEvidenceReader,
    NeedUnitService,
    NeedUnitUnitOfWork,
)
from domains.demand.unit_service import NeedUnitServiceImpl
from domains.opportunities.permissions import Phase1OpportunityAuthorizer
from domains.quotations.file_service import QuoteFileServiceImpl
from domains.quotations.service import (
    ContextQuoteFileScopeAuthorizer,
    QuotationUowFactory,
    QuotationVersionService,
    QuoteContextProvider,
    QuoteCustomerVersionsService,
    QuoteCustomerVersionsServiceImpl,
    QuoteFileAccessServiceImpl,
    QuoteFileService,
    QuoteGeneratedArtifactReader,
    QuotePreparationReadService,
    QuotePreparationReadServiceImpl,
    QuoteWorkflowRunReader,
    StrictQuotePreparationPolicy,
)
from domains.quotations.service_impl import QuotationServiceImpl
from infra.db.costing_freeze_uow import SqlAlchemyCostingFreezeUow
from infra.db.costing_uow import SqlAlchemyCostingUnitOfWork
from infra.db.need_unit_scope import SqlAlchemyNeedUnitScopeReader
from infra.db.need_unit_uow import SqlAlchemyNeedUnitUnitOfWork
from infra.db.quotation_uow import SqlAlchemyQuotationUow
from infra.db.quote_context import SqlAlchemyQuoteContextProvider
from infra.db.quote_evidence_context import SqlAlchemyQuoteEvidenceContextReader
from infra.db.quote_file_rate_limit import (
    PostgresQuoteFileExecutionHistoryReader,
    PostgresQuoteFileGenerationRateLimiter,
)
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.quotation_settings import QuotationRuntimeSettings
from shared.evidence_read import QuoteEvidenceRawReader, QuoteEvidenceReader
from shared.schemas.generated_documents import (
    GeneratedDocumentMetadataReader,
    GeneratedDocumentStore,
)
from shared.schemas.identifiers import TenantId, new_id
from tool_gateway.checks.idempotency import IdempotencyCheck
from tool_gateway.checks.quote_evidence import (
    QuoteEvidencePermissionCheck,
    QuoteEvidenceTenantCheck,
)
from tool_gateway.checks.quote_files import (
    QuoteFileApprovalCheck,
    QuoteFilePermissionCheck,
    QuoteFileRateLimitCheck,
    QuoteFileTenantCheck,
)
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.quote_evidence import (
    MANIFEST,
    QuoteEvidenceReadHandler,
    ToolGatewayQuoteEvidenceReader,
)
from tool_gateway.handlers.quote_evidence_slots import QuoteEvidenceResultSlot
from tool_gateway.handlers.quote_file_recovery import QuoteFileRecoveryHandler
from tool_gateway.handlers.quote_files import (
    GENERATE_MANIFEST,
    QuoteFileGenerateHandler,
    QuoteFileReadHandler,
    QuoteFileResultSlot,
    register_quote_file_tools,
)
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import ToolGateway
from tool_gateway.quote_file_ledger import (
    PublicLedgerQuoteGenerationReader,
    PublicLedgerQuoteRecoveryAudit,
)
from tool_gateway.repository import ToolGatewayUnitOfWorkFactory
from workflows.employee_work_intake.service import WorkIntakeService
from workflows.engine.runner import WorkflowEngine
from workflows.quote_approval.application import (
    DemandNeedFactsValidator,
    QuoteApplicationService,
    QuotePreparationApplication,
)
from workflows.quote_approval.approvals import QuotationApprovalAccess
from workflows.quote_approval.completion_reader import (
    PersistentQuoteCreationCompletionReader,
)
from workflows.quote_approval.file_facts import (
    ApprovalServiceQuoteFileFactsReader,
    DemandQuoteFileNeedValidator,
)
from workflows.quote_approval.files import QuoteFilesApplication
from workflows.quote_approval.issuer_reader import DeferredQuoteIssuerReader
from workflows.quote_approval.need_unit_access import CurrentNeedUnitAuthorizer
from workflows.quote_approval.policy_reader import CostingQuoteApprovalPolicyReader
from workflows.quote_approval.preparation_facts import DemandQuotePreparationProjector
from workflows.quote_approval.run_reader import WorkflowQuoteRunReader
from workflows.quote_approval.runtime_readers import (
    CurrentCostingActorReader,
    CurrentQuotationActorReader,
    UnavailableQuoteSendReceiptReader,
)
from workflows.quote_approval.source_access import (
    CurrentCostScopeSourceAccess,
    QuoteEvidenceAccessImpl,
)
from workflows.quote_approval.source_readers import (
    GatewayNeedUnitEvidenceReader,
    GatewayPricingEvidenceReader,
)

logger = logging.getLogger(__name__)


class QuotationRuntimeLifecycle:
    """同一parser的进程启停端口，HTTP不调用probe。"""

    def __init__(self, parser: LinuxEvidenceTextParser) -> None:
        self._parser = parser
        self._started = False
        self._closed = False
        self._startup_lock = asyncio.Lock()

    async def startup(self) -> None:
        """普通能力失败由parser保留关闭；意外异常不伪装平台降级。"""
        async with self._startup_lock:
            if self._closed:
                raise RuntimeError("报价运行依赖启动失败")
            if self._started:
                return
            try:
                await self._parser.probe()
                self._started = True
            except BaseException as primary:
                try:
                    await self.aclose()
                except BaseException as cleanup:  # noqa: BLE001 - 清理取消不能覆盖primary
                    logger.error(
                        "报价解析资源释放失败",
                        extra={"error_type": type(cleanup).__name__},
                    )
                if isinstance(primary, Exception):
                    raise RuntimeError("报价运行依赖启动失败") from None  # noqa: TRY004 - 统一启动错误
                raise

    async def aclose(self) -> None:
        """回收同一parser所有资源，关闭后不得重启。"""
        if not self._closed:
            self._closed = True
            await self._parser.aclose()


@dataclass(frozen=True)
class QuotationDomainComposition:
    """来源已经显式注入的真实域服务，文件组可独立禁用。"""

    context_provider: QuoteContextProvider
    quotations: QuotationVersionService
    costing_quotes: CostingQuoteService
    costing_freeze: CostingFreezeService
    need_units: NeedUnitService
    creation: QuoteApplicationService
    preparation: QuotePreparationApplication
    preparation_reads: QuotePreparationReadService
    files: QuoteFileService | None
    approval_access: QuoteApprovalAccess


@dataclass(frozen=True)
class QuotationEvidenceComposition:
    """独立来源工具及其受限解析器，不持报价或旧发送服务。"""

    pricing: PricingEvidenceReader
    need_units: NeedUnitEvidenceReader
    scope_access: CostScopeSourceAccess
    preview_reader: QuoteEvidenceReader
    parser: LinuxEvidenceTextParser


@dataclass(frozen=True)
class QuotationRuntimeComposition:
    """发布后不可替换的worker能力；None只表示明确禁用的文件组。"""

    domain: QuotationDomainComposition
    customer_versions: QuoteCustomerVersionsService | None
    files_application: QuoteFilesApplication | None
    evidence: QuotationEvidenceComposition
    lifecycle: QuotationRuntimeLifecycle


SessionFactory = async_sessionmaker[AsyncSession]
Clock = Callable[[], datetime]


def build_need_unit_authorizer(
    factory: SessionFactory, settings: QuotationRuntimeSettings, *, tenant_id: TenantId
) -> NeedUnitAuthorizer:
    """来源与单位确认各用同一公开规则及显式运行租户，不构造默认scope许可。"""
    return CurrentNeedUnitAuthorizer(
        SqlAlchemyQuoteEvidenceContextReader(
            factory, statement_timeout_ms=settings.core.statement_timeout_ms
        ),
        SqlAlchemyNeedUnitScopeReader(
            factory,
            lock_timeout_ms=settings.core.lock_timeout_ms,
            statement_timeout_ms=settings.core.statement_timeout_ms,
        ),
        Phase1OpportunityAuthorizer(tenant_id),
    )


def build_quotation_evidence(
    factory: SessionFactory,
    settings: QuotationRuntimeSettings,
    *,
    tenant_id: TenantId,
    raw: QuoteEvidenceRawReader,
    uploads: WorkIntakeService,
    need_authorizer: NeedUnitAuthorizer,
    fingerprints: HmacFingerprintProvider,
    lease_duration: timedelta,
    lease_owner: str,
    now: Clock,
) -> QuotationEvidenceComposition:
    """来源专用registry先建；零invoke/probe/原件IO，无quotation构造依赖。"""
    contexts = SqlAlchemyQuoteEvidenceContextReader(
        factory, statement_timeout_ms=settings.core.statement_timeout_ms
    )
    access = QuoteEvidenceAccessImpl(contexts, raw, uploads, need_authorizer)
    parser = LinuxEvidenceTextParser(
        limits=settings.evidence.parser, probe_limits=settings.evidence.probe
    )
    slot = QuoteEvidenceResultSlot(new_id)
    handler = QuoteEvidenceReadHandler(
        access,
        raw,
        parser,
        slot,
        fingerprints,
        maximum_raw_bytes=settings.evidence.raw_maximum_bytes,
        parser_limits=settings.evidence.parser,
    )
    registry = ToolRegistry()
    registry.register(MANIFEST, handler)
    gateway = ToolGateway(
        registry,  # type: ignore[arg-type]  # 既有registry的可写Protocol成员不协变
        {
            "tenant": QuoteEvidenceTenantCheck(tenant_id),
            "permission": QuoteEvidencePermissionCheck(access, slot),
        },
        cast(
            ToolGatewayUnitOfWorkFactory,
            lambda tenant: SqlAlchemyToolGatewayUnitOfWork(factory, tenant, now=now),
        ),
        lease_duration=lease_duration,
        lease_owner=lease_owner,
        now=now,
        id_factory=new_id,
    )
    reader = ToolGatewayQuoteEvidenceReader(gateway, slot, access)
    return QuotationEvidenceComposition(
        GatewayPricingEvidenceReader(reader),
        GatewayNeedUnitEvidenceReader(reader, access, contexts, need_authorizer),
        CurrentCostScopeSourceAccess(access),
        reader,
        parser,
    )


def build_quotation_domains(
    factory: SessionFactory,
    settings: QuotationRuntimeSettings,
    *,
    tenant_id: TenantId,
    evidence: QuotationEvidenceComposition,
    run_reader: QuoteWorkflowRunReader,
    artifact_reader: QuoteGeneratedArtifactReader | None,
    now: Clock,
) -> QuotationDomainComposition:
    """先context/quotation再completion/freeze；闭包只在返回之前发布一次。"""
    core = settings.core
    actors_source = SqlAlchemyQuoteEvidenceContextReader(
        factory, statement_timeout_ms=core.statement_timeout_ms
    )
    actors = CurrentQuotationActorReader(actors_source)
    costing_actors = CurrentCostingActorReader(actors_source)
    quote_ref: QuotationVersionService | None = None
    context = SqlAlchemyQuoteContextProvider(
        factory,
        DeferredQuoteIssuerReader(lambda: quote_ref),
        lock_timeout_ms=core.lock_timeout_ms,
        statement_timeout_ms=core.statement_timeout_ms,
    )
    quote_uows = cast(
        QuotationUowFactory,
        lambda tenant: SqlAlchemyQuotationUow(
            factory,
            tenant,
            lock_timeout_ms=core.lock_timeout_ms,
            statement_timeout_ms=core.statement_timeout_ms,
        ),
    )
    freeze_uows = cast(
        CostingFreezeUowFactory,
        lambda tenant: SqlAlchemyCostingFreezeUow(
            factory,
            tenant,
            now=now,
            lock_timeout_ms=core.lock_timeout_ms,
            statement_timeout_ms=core.statement_timeout_ms,
        ),
    )
    policies = CostingQuoteApprovalPolicyReader(
        CostingApprovalPolicyReaderImpl(
            cast(CostingUnitOfWorkFactory, freeze_uows), now=now
        )
    )
    policy = StrictQuotePreparationPolicy()
    files = None
    if settings.files is not None:
        if artifact_reader is None:
            raise RuntimeError("报价文件依赖未完整配置")
        files = QuoteFileServiceImpl(
            quote_uows,
            actors,
            ContextQuoteFileScopeAuthorizer(context),
            artifact_reader,
            run_reader,
            id_generator=new_id,
        )
    quotations = QuotationServiceImpl(
        quote_uows,
        actors,
        policy,
        UnavailableQuoteSendReceiptReader(),
        context_provider=context,
        approval_policy_reader=policies,
        workflow_run_reader=run_reader,
        now=now,
        files=files,
    )
    quote_ref = quotations
    costing = CostingQuoteServiceImpl(
        cast(
            CostingUnitOfWorkFactory,
            lambda tenant: SqlAlchemyCostingUnitOfWork(factory, tenant, now=now),
        ),
        evidence.pricing,
        actor_reader=costing_actors,
        now=now,
    )
    freeze = CostingFreezeServiceImpl(
        freeze_uows,
        costing_actors,
        DemandNeedFactsValidator(),
        evidence.scope_access,
        PersistentQuoteCreationCompletionReader(quotations, actors),
        now=now,
    )
    units = NeedUnitServiceImpl(
        lambda tenant: cast(
            NeedUnitUnitOfWork,
            SqlAlchemyNeedUnitUnitOfWork(
                factory,
                tenant,
                lock_timeout_ms=core.lock_timeout_ms,
                statement_timeout_ms=core.statement_timeout_ms,
            ),
        ),
        build_need_unit_authorizer(factory, settings, tenant_id=tenant_id),
        evidence.need_units,
        now=now,
    )
    return QuotationDomainComposition(
        context,
        quotations,
        costing,
        freeze,
        units,
        QuoteApplicationService(context, freeze, quotations, actors, policy, now=now),
        QuotePreparationApplication(context, freeze, policy, costing_actors),
        QuotePreparationReadServiceImpl(
            context, policy, DemandQuotePreparationProjector(), now
        ),
        files,
        QuotationApprovalAccess(quotations),
    )


def build_quotation_runtime(
    domain: QuotationDomainComposition,
    *,
    factory: SessionFactory,
    approvals: ApprovalService,
    engine: WorkflowEngine,
    settings: QuotationRuntimeSettings,
    evidence: QuotationEvidenceComposition,
    generated: GeneratedDocumentStore | None,
    metadata_only: GeneratedDocumentMetadataReader | None,
    fingerprints: HmacFingerprintProvider,
    now: Clock,
) -> QuotationRuntimeComposition:
    """唯一approvals/engine发布后装配文件组；恢复只接独立metadata-only实例。"""
    core, limits = settings.core, settings.files
    actors = CurrentQuotationActorReader(
        SqlAlchemyQuoteEvidenceContextReader(
            factory, statement_timeout_ms=core.statement_timeout_ms
        )
    )
    files_application, customer_versions = None, None
    if (
        limits is not None
        and domain.files is not None
        and generated is not None
        and metadata_only is not None
    ):
        quote_uows = cast(
            QuotationUowFactory,
            lambda tenant: SqlAlchemyQuotationUow(
                factory,
                tenant,
                lock_timeout_ms=core.lock_timeout_ms,
                statement_timeout_ms=core.statement_timeout_ms,
            ),
        )
        costing_uows = cast(
            CostingUnitOfWorkFactory,
            lambda tenant: SqlAlchemyCostingFreezeUow(
                factory,
                tenant,
                now=now,
                lock_timeout_ms=core.lock_timeout_ms,
                statement_timeout_ms=core.statement_timeout_ms,
            ),
        )
        policies = CostingQuoteApprovalPolicyReader(
            CostingApprovalPolicyReaderImpl(costing_uows, now=now)
        )
        access = QuoteFileAccessServiceImpl(
            quote_uows,
            actors,
            domain.context_provider,
            ApprovalServiceQuoteFileFactsReader(approvals),
            DemandQuoteFileNeedValidator(),
            policies,
            WorkflowQuoteRunReader(lambda: engine),
            domain.files,
            now=now,
            template_version=limits.template_version,
        )
        customer_versions = QuoteCustomerVersionsServiceImpl(
            quote_uows,
            domain.context_provider,
            access,
            domain.files,
            now=now,
            maximum_page_size=core.maximum_page_size,
        )
        technical_now = lambda: datetime.now(UTC)
        ledger_uows = cast(
            ToolGatewayUnitOfWorkFactory,
            lambda tenant: SqlAlchemyToolGatewayUnitOfWork(
                factory, tenant, now=technical_now
            ),
        )
        ledger = PublicLedgerQuoteGenerationReader(ledger_uows)
        slot = QuoteFileResultSlot()
        renderer = ReportLabQuotePdfRenderer(
            limits.maximum_bytes,
            limits.maximum_pages,
            maximum_text_bytes=limits.maximum_text_bytes,
        )
        owner = "scheduler-quote-files"
        generate = QuoteFileGenerateHandler(
            access,
            domain.files,
            generated,
            renderer,
            PostgresQuoteFileExecutionHistoryReader(
                factory, statement_timeout_ms=core.statement_timeout_ms
            ),
            fingerprints,
            slot,
            now=now,
        )
        read = QuoteFileReadHandler(
            access,
            domain.files,
            generated,
            slot,
            fingerprints,
            maximum_bytes=limits.maximum_bytes,
            history=False,
        )
        history = QuoteFileReadHandler(
            access,
            domain.files,
            generated,
            slot,
            fingerprints,
            maximum_bytes=limits.maximum_bytes,
            history=True,
        )
        recovery = QuoteFileRecoveryHandler(
            access,
            domain.files,
            metadata_only,
            ledger,
            PublicLedgerQuoteRecoveryAudit(
                ledger_uows, now=technical_now, id_factory=new_id
            ),
            fingerprints,
            slot,
            generate_tool_version=GENERATE_MANIFEST.version,
            now=now,
        )
        registry = ToolRegistry()
        register_quote_file_tools(
            registry, generate=generate, read=read, history=history, recovery=recovery
        )
        gateway = ToolGateway(
            registry,  # type: ignore[arg-type]  # 既有registry的可写Protocol成员不协变
            {
                "tenant": QuoteFileTenantCheck(slot),
                "permission": QuoteFilePermissionCheck(actors, access, slot),
                "approval": QuoteFileApprovalCheck(access, slot),
                "idempotency": IdempotencyCheck(),
                "rate_limit": QuoteFileRateLimitCheck(
                    PostgresQuoteFileGenerationRateLimiter(
                        factory,
                        limits=limits.rate_limit,
                        lease_owner=owner,
                        id_generator=new_id,
                    ),
                    slot,
                ),
            },
            ledger_uows,
            lease_duration=timedelta(seconds=limits.gateway_lease_seconds),
            lease_owner=owner,
            now=technical_now,
            id_factory=new_id,
        )
        files_application = QuoteFilesApplication(
            gateway,
            access,
            domain.files,
            slot,
            ledger,
            fingerprints,
            generate_tool_version=GENERATE_MANIFEST.version,
        )
    return QuotationRuntimeComposition(
        domain,
        customer_versions,
        files_application,
        evidence,
        QuotationRuntimeLifecycle(evidence.parser),
    )
