"""本api进程报价入口；共享机械代码，不共享实例或导入另一进程。"""

from dataclasses import dataclass
from datetime import timedelta

from apps.composition_support import quotations as common
from apps.composition_support.quotations import (
    Clock,
    QuotationDomainComposition,
    QuotationEvidenceComposition,
    QuotationRuntimeLifecycle,
    SessionFactory,
)
from domains.approvals.service import ApprovalService
from domains.demand.service import NeedUnitAuthorizer
from domains.quotations.service import (
    QuoteCustomerVersionsService,
    QuoteGeneratedArtifactReader,
    QuoteWorkflowRunReader,
)
from infra.quotation_settings import QuotationRuntimeSettings
from shared.evidence_read import QuoteEvidenceRawReader
from shared.schemas.generated_documents import (
    GeneratedDocumentMetadataReader,
    GeneratedDocumentStore,
)
from shared.schemas.identifiers import TenantId
from tool_gateway.fingerprint import HmacFingerprintProvider
from workflows.employee_work_intake.service import WorkIntakeService
from workflows.engine.runner import WorkflowEngine
from workflows.quote_approval.files import QuoteFilesApplication
from workflows.quote_approval.http import (
    CurrentQuoteApprovalStarter,
    QuoteApprovalStarter,
)

__all__ = [
    "Clock",
    "QuotationDomainComposition",
    "QuotationEvidenceComposition",
    "QuotationHttpComposition",
    "QuotationRuntimeLifecycle",
    "SessionFactory",
    "build_need_unit_authorizer",
    "build_quotation_domains",
    "build_quotation_evidence",
    "build_quotation_http",
]


@dataclass(frozen=True)
class QuotationHttpComposition:
    """发布后不可替换的API能力；None只表示明确禁用的文件组。"""

    domain: QuotationDomainComposition
    approval_starter: QuoteApprovalStarter
    customer_versions: QuoteCustomerVersionsService | None
    files_application: QuoteFilesApplication | None
    evidence: QuotationEvidenceComposition
    lifecycle: QuotationRuntimeLifecycle


def build_need_unit_authorizer(
    factory: SessionFactory, settings: QuotationRuntimeSettings, *, tenant_id: TenantId
) -> NeedUnitAuthorizer:
    """按本进程显式参数调用共享机械装配。"""
    return common.build_need_unit_authorizer(factory, settings, tenant_id=tenant_id)


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
    """本进程新建独立来源registry、slot及parser，不在构造时执行IO。"""
    return common.build_quotation_evidence(
        factory,
        settings,
        tenant_id=tenant_id,
        raw=raw,
        uploads=uploads,
        need_authorizer=need_authorizer,
        fingerprints=fingerprints,
        lease_duration=lease_duration,
        lease_owner=lease_owner,
        now=now,
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
    """本进程新建独立域实例与一次发布闭包，不复用其他进程。"""
    return common.build_quotation_domains(
        factory,
        settings,
        tenant_id=tenant_id,
        evidence=evidence,
        run_reader=run_reader,
        artifact_reader=artifact_reader,
        now=now,
    )


def build_quotation_http(
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
) -> QuotationHttpComposition:
    """保留进程入口与显式文件lease owner，只包装共同装配结果。"""
    parts = common.build_quotation_runtime_parts(
        domain,
        factory=factory,
        approvals=approvals,
        engine=engine,
        settings=settings,
        evidence=evidence,
        generated=generated,
        metadata_only=metadata_only,
        fingerprints=fingerprints,
        lease_owner="api-quote-files",
        now=now,
    )
    return QuotationHttpComposition(
        domain,
        CurrentQuoteApprovalStarter(domain.quotations, engine, parts.actor_reader),
        parts.customer_versions,
        parts.files_application,
        evidence,
        parts.lifecycle,
    )
