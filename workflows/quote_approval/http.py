"""HTTP用途的最窄流程适配；只调用域公共端口，不直查存储或冻结。"""

from typing import Protocol

from domains.costing.service import (
    CalculationSnapshot,
    CostCalculationCommand,
    CostCoverageCreate,
    CostCoveragePublicView,
    CostingActor,
    CostingQuoteService,
    CostingService,
    CostScopeConfirmationCommand,
    CostScopePublicView,
    PricingOptions,
    project_coverage,
    project_scope,
)
from domains.quotations.errors import (
    QuotationPermissionError,
    QuotationUnavailableError,
)
from domains.quotations.schemas import QuotationActor, QuoteDraftCommand
from domains.quotations.service import (
    QuotationActorReader,
    QuotationVersionService,
    QuoteInternalPublicView,
    project_internal_quote,
)
from shared.errors import ValidationError
from shared.schemas.evidence_read import EvidenceTextResult, QuoteEvidenceError
from shared.schemas.identifiers import (
    CostSheetId,
    EmployeeId,
    OpportunityId,
    QuoteId,
    TenantId,
)
from workflows.engine.runner import WorkflowEngine
from workflows.quote_approval.application import (
    QuoteApplicationService,
    QuotePreparationApplication,
)
from workflows.quote_approval.flow import start_quote_approval
from workflows.quote_approval.http_schemas import (
    EvidenceLocatorPublicView,
    EvidencePreviewPublicView,
    QuoteApprovalStartResult,
)


class QuoteApprovalStarter(Protocol):
    """受信当前员工启动入口；实现须在T5绑定核验后返回。"""

    async def start(
        self, tenant_id: TenantId, quote_id: QuoteId, *, actor_id: EmployeeId
    ) -> QuoteApprovalStartResult: ...


class CurrentQuoteApprovalStarter:
    """从真实当前事实构造actor，再委托唯一T5启动与持久run绑定校验。"""

    def __init__(
        self,
        quotations: QuotationVersionService,
        engine: WorkflowEngine,
        actors: QuotationActorReader,
    ) -> None:
        self._quotes, self._engine, self._actors = quotations, engine, actors

    async def start(
        self, tenant_id: TenantId, quote_id: QuoteId, *, actor_id: EmployeeId
    ) -> QuoteApprovalStartResult:
        try:
            fact = await self._actors.read_current(tenant_id, actor_id)
        except Exception:  # noqa: BLE001 - 身份故障不泄露存储原文
            raise QuotationUnavailableError("dependency_unavailable") from None
        if fact is None or (fact.tenant_id, fact.employee_id, fact.is_active) != (
            tenant_id,
            actor_id,
            True,
        ):
            raise QuotationPermissionError("permission_denied")
        run_id = await start_quote_approval(
            self._engine,
            self._quotes,
            tenant_id,
            quote_id,
            actor=QuotationActor(employee_id=fact.employee_id, role=fact.role),
        )
        return QuoteApprovalStartResult(quote_id=quote_id, run_id=run_id)


async def confirm_coverage(
    service: CostingQuoteService,
    tenant: TenantId,
    sheet: CostSheetId,
    command: CostCoverageCreate,
    *,
    actor: CostingActor,
    key: str,
) -> CostCoveragePublicView:
    """按本次确认返回hash精确恢复，不能被并发latest替换。"""
    digest = await service.confirm_coverage(
        tenant, sheet, command, actor=actor, idempotency_key=key
    )
    value = await service.get_coverage(tenant, sheet, actor=actor, content_hash=digest)
    if value is None or value.cost_sheet_id != sheet or value.content_hash != digest:
        from domains.costing.errors import CostFreezeUnavailableError

        raise CostFreezeUnavailableError("storage_unknown")
    return project_coverage(value)


async def confirm_scope(
    costing: CostingService,
    preparation: QuotePreparationApplication,
    tenant: TenantId,
    sheet: CostSheetId,
    command: CostScopeConfirmationCommand,
    *,
    actor: CostingActor,
    actor_id: EmployeeId,
    key: str,
) -> CostScopePublicView:
    """机会只能取当前成本表，来源授权与锁顺序由既有应用负责。"""
    value = await costing.get_sheet(tenant, sheet, actor=actor)
    scope = await preparation.confirm_scope(
        tenant,
        OpportunityId(value.opportunity_id),
        sheet,
        command,
        actor_id=actor_id,
        idempotency_key=key,
    )
    return project_scope(scope)


async def calculate(
    costing: CostingService,
    preparation: QuotePreparationApplication,
    tenant: TenantId,
    sheet: CostSheetId,
    command: CostCalculationCommand,
    *,
    actor: CostingActor,
    actor_id: EmployeeId,
) -> CalculationSnapshot:
    """只传真实FX引用，不接受客户端构造FxRate或冻结回执。"""
    value = await costing.get_sheet(tenant, sheet, actor=actor)
    options = PricingOptions(
        mode=command.mode,
        unit_price=command.unit_price,
        rounding=command.rounding,
        quote_fx=None,
        algorithm_version=command.algorithm_version,
    )
    return await preparation.calculate(
        tenant,
        OpportunityId(value.opportunity_id),
        sheet,
        options,
        quote_fx_ref=command.quote_fx_ref,
        actor_id=actor_id,
    )


async def create_quote(
    application: QuoteApplicationService,
    tenant: TenantId,
    command: QuoteDraftCommand,
    *,
    actor_id: EmployeeId,
    key: str,
    opportunity_id: OpportunityId | None = None,
    replaces_quote_id: QuoteId | None = None,
) -> QuoteInternalPublicView:
    """路径不是第二个业务输入，必须与完整原始CAS命令相同。"""
    if (opportunity_id is not None and command.opportunity_id != opportunity_id) or (
        replaces_quote_id is not None
        and (
            command.replaces_quote_id != replaces_quote_id
            or command.expected_quote_version is None
        )
    ):
        raise ValidationError("报价路径绑定无效")
    value = await application.create(
        tenant, command, actor_id=actor_id, idempotency_key=key
    )
    return project_internal_quote(value)


def project_preview(value: EvidenceTextResult) -> EvidencePreviewPublicView:
    """verify根用途无正文，不能伪造预览成功。"""
    if value.profile is None or value.text is None or value.text_hash is None:
        raise QuoteEvidenceError("source_integrity_failed")
    return EvidencePreviewPublicView(
        source_ref=value.reference.source_ref,
        scope=value.reference.scope,
        artifact_id=value.reference.raw.artifact_id,
        raw_hash=value.reference.raw.content_hash,
        profile=value.profile,
        page=value.page,
        text=value.text,
        text_hash=value.text_hash,
    )


def project_locator(value: EvidenceTextResult) -> EvidenceLocatorPublicView:
    """逐字段投影本次选区，原文不经普通内部DTO序列化。"""
    preview = project_preview(value)
    if value.selection is None or value.excerpt is None or value.locator is None:
        raise QuoteEvidenceError("source_integrity_failed")
    return EvidenceLocatorPublicView(
        source_ref=preview.source_ref,
        scope=preview.scope,
        artifact_id=preview.artifact_id,
        raw_hash=preview.raw_hash,
        profile=preview.profile,
        page=preview.page,
        text=preview.text,
        text_hash=preview.text_hash,
        start=value.selection.start,
        end=value.selection.end,
        excerpt_hash=value.selection.excerpt_hash,
        excerpt=value.excerpt,
        locator=value.locator,
    )
