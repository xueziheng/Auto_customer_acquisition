"""安全报价HTTP；请求身份来自现认证依赖，业务判断仍在公共服务。"""

from __future__ import annotations

import json
import re
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Header, Query, Request, Response
from pydantic import BaseModel, BeforeValidator, TypeAdapter

from domains.costing.schemas import (
    CostCoverageCreate,
    PriceEvidenceCreate,
    PricingPolicyCreate,
    QuoteFxCreate,
)
from domains.costing.service import (
    CalculationSnapshot,
    CostCalculationCommand,
    CostCoveragePublicView,
    CostingActor,
    CostingQuoteNotFoundError,
    CostingScope,
    CostScopeConfirmationCommand,
    CostScopePublicView,
    PriceEvidencePublicView,
    PricingPolicyPublicView,
    QuoteFxPublicView,
    project_coverage,
    project_policy,
    project_price_evidence,
    project_quote_fx,
    project_scope,
)
from domains.demand.service import (
    NeedUnitConfirmationCommand,
    NeedUnitConfirmationPublicView,
    NeedUnitPreparationView,
    project_need_unit_confirmation,
    project_need_unit_preparation,
)
from domains.quotations.schemas import (
    QuotationActor,
    QuoteCustomerVersionPage,
    QuoteDraftCommand,
    QuoteFileView,
    QuoteIssuerCreate,
)
from domains.quotations.service import (
    QuoteEmptyCommand,
    QuoteInternalPublicView,
    QuoteIssuerPublicView,
    QuotePreparationPublicView,
    project_internal_quote,
    project_issuer,
)
from shared.errors import PermissionDenied, TransientError, ValidationError
from shared.schemas.evidence_read import EvidenceLocateRequest, EvidencePreviewRequest
from shared.schemas.identifiers import (
    CostSheetId,
    OpportunityId,
    QuoteFileId,
    QuoteId,
    ValidatedNeedId,
)
from shared.schemas.quote_creation import QuoteKey
from shared.schemas.quote_facts import fact_identity
from shared.schemas.quote_files import QUOTE_PDF_TEMPLATE_VERSIONS
from workflows.quote_approval import http
from workflows.quote_approval.file_schemas import (
    QuoteFileApiError,
    QuoteFileRecoveryCommand,
    QuoteFileRecoveryResult,
)
from workflows.quote_approval.http_schemas import (
    EvidenceLocatorPublicView,
    EvidencePreviewPublicView,
    QuoteApprovalStartResult,
)

from ..composition.quotations import QuotationHttpComposition
from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_request_identity,
)
from ..identity import RequestIdentity
from ..middleware import ApiErrorResponse
from .quotation_errors import QuotationRoute


async def _queries(request: Request) -> None:
    """新路径未知查询拒绝；不能把actor/key/history当服务端控制字段。"""
    endpoint = request.scope["endpoint"].__name__
    allowed = {
        "get_policy": {"category"},
        "get_coverage": {"content_hash"},
        "customer_versions": {"before_version", "limit"},
    }.get(endpoint, set())
    if any(
        key not in allowed or len(request.query_params.getlist(key)) != 1
        for key in request.query_params
    ):
        raise ValidationError("报价查询参数无效")
    if request.method == "GET" and await request.body():
        raise ValidationError("报价读取不接受请求正文")


ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": ApiErrorResponse} for status in (400, 403, 404, 409, 429, 503)
}
FILE_ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": ApiErrorResponse | QuoteFileApiError}
    for status in (403, 404, 409, 429, 503)
}
PDF_RESPONSES: dict[int | str, dict[str, Any]] = {
    **FILE_ERRORS,
    200: {
        "content": {
            "application/pdf": {"schema": {"type": "string", "format": "binary"}}
        }
    },
}
router = APIRouter(
    route_class=QuotationRoute, dependencies=[Depends(_queries)], responses=ERRORS
)
Identity = Annotated[RequestIdentity, Depends(get_request_identity)]
Dependencies = Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)]


def _json_wire(model: Any) -> BeforeValidator:
    """恢复JSON语义以无损接收数组/日期；数字保持原类型，金额float不转字符串。"""
    adapter = TypeAdapter(model)

    def parse(value: Any) -> Any:
        if isinstance(value, BaseModel):
            return value
        return adapter.validate_json(
            json.dumps(value, ensure_ascii=False, allow_nan=False)
        )

    return BeforeValidator(parse)


async def _key(
    request: Request, value: Annotated[QuoteKey, Header(alias="Idempotency-Key")]
) -> str:
    values = request.headers.getlist("Idempotency-Key")
    if len(values) != 1:
        raise ValidationError("报价幂等键缺失或重复")
    return value


Key = Annotated[str, Depends(_key)]
_EMPTY_COMMAND = QuoteEmptyCommand()


async def _empty(request: Request) -> None:
    """null不是空命令；没有body或严格零字段对象才可调用。"""
    content = await request.body()
    if content:
        try:
            QuoteEmptyCommand.model_validate_json(content)
        except ValueError:
            raise ValidationError("报价空命令无效") from None


def _composition(deps: ConfiguredApiDependencies) -> QuotationHttpComposition:
    if deps.quotation is None:
        raise TransientError("报价依赖不可用")
    return deps.quotation


def _file_composition(deps: ConfiguredApiDependencies) -> QuotationHttpComposition:
    """文件能力整组发布，列表不能绕过生成或metadata-only依赖缺失。"""
    composition = _composition(deps)
    _required(composition.domain.files)
    _required(composition.files_application)
    _required(composition.customer_versions)
    return composition


def _cost_actor(identity: RequestIdentity, *, boss: bool = False) -> CostingActor:
    role = identity.employee.role
    if role not in ({"boss"} if boss else {"boss", "product", "sourcing", "finance"}):
        raise PermissionDenied("报价用途权限拒绝")
    return CostingActor(
        actor_id=str(identity.employee.employee_id),
        role=role,
        scope=CostingScope.TENANT,
    )


def _quote_actor(identity: RequestIdentity, *, boss: bool = False) -> QuotationActor:
    _cost_actor(identity, boss=boss)
    return QuotationActor.model_validate(
        {"employee_id": identity.employee.employee_id, "role": identity.employee.role}
    )


def _required[T](value: T | None) -> T:
    if value is None:
        raise TransientError("报价依赖不可用")
    return value


def _identifier(value: str) -> str:
    try:
        fact_identity(value)
    except ValueError:
        raise ValidationError("报价标识无效") from None
    return value


def _file_identifier(value: str, field: str) -> str:
    """复用文件公开DTO的原canonical规则，不另造资源ID契约。"""
    adapter = TypeAdapter(QuoteFileView.model_fields[field].rebuild_annotation())
    try:
        return str(adapter.validate_python(value, strict=True))
    except ValueError:
        raise ValidationError("报价文件标识无效") from None


@router.get("/policies", response_model=PricingPolicyPublicView)
async def get_policy(
    identity: Identity, deps: Dependencies, category: str | None = None
) -> PricingPolicyPublicView:
    actor = _cost_actor(identity)
    return project_policy(
        await _composition(deps).domain.costing_quotes.get_policy(
            identity.tenant_id, category, actor=actor
        )
    )


@router.post("/policies", response_model=PricingPolicyPublicView)
async def confirm_policy(
    body: Annotated[PricingPolicyCreate, _json_wire(PricingPolicyCreate)],
    identity: Identity,
    deps: Dependencies,
    key: Key,
) -> PricingPolicyPublicView:
    actor = _cost_actor(identity, boss=True)
    return project_policy(
        await _composition(deps).domain.costing_quotes.confirm_policy(
            identity.tenant_id, body, actor=actor, idempotency_key=key
        )
    )


@router.get("/issuer", response_model=QuoteIssuerPublicView | None)
async def get_issuer(
    identity: Identity, deps: Dependencies
) -> QuoteIssuerPublicView | None:
    actor = _quote_actor(identity)
    return await _composition(deps).domain.quotations.get_issuer(
        identity.tenant_id, actor=actor
    )


@router.post("/issuer", response_model=QuoteIssuerPublicView)
async def confirm_issuer(
    body: Annotated[QuoteIssuerCreate, _json_wire(QuoteIssuerCreate)],
    identity: Identity,
    deps: Dependencies,
    key: Key,
) -> QuoteIssuerPublicView:
    actor = _quote_actor(identity, boss=True)
    return project_issuer(
        await _composition(deps).domain.quotations.confirm_issuer(
            identity.tenant_id, body, actor=actor, idempotency_key=key
        )
    )


@router.post("/quote-fx", response_model=QuoteFxPublicView)
async def confirm_fx(
    body: Annotated[QuoteFxCreate, _json_wire(QuoteFxCreate)],
    identity: Identity,
    deps: Dependencies,
    key: Key,
) -> QuoteFxPublicView:
    actor = _cost_actor(identity)
    return project_quote_fx(
        await _composition(deps).domain.costing_quotes.confirm_quote_fx(
            identity.tenant_id, body, actor=actor, idempotency_key=key
        )
    )


@router.get("/quote-fx/{fx_id}", response_model=QuoteFxPublicView)
async def get_fx(
    fx_id: str, identity: Identity, deps: Dependencies
) -> QuoteFxPublicView:
    actor = _cost_actor(identity)
    return project_quote_fx(
        await _composition(deps).domain.costing_quotes.get_quote_fx(
            identity.tenant_id, _identifier(fx_id), actor=actor
        )
    )


@router.post("/price-evidence", response_model=PriceEvidencePublicView)
async def confirm_price(
    body: Annotated[PriceEvidenceCreate, _json_wire(PriceEvidenceCreate), Body()],
    identity: Identity,
    deps: Dependencies,
    key: Key,
) -> PriceEvidencePublicView:
    actor = _cost_actor(identity)
    return project_price_evidence(
        await _composition(deps).domain.costing_quotes.confirm_price(
            identity.tenant_id, body, actor=actor, idempotency_key=key
        )
    )


@router.get(
    "/opportunities/{opportunity_id}/price-evidence",
    response_model=tuple[PriceEvidencePublicView, ...],
)
async def list_prices(
    opportunity_id: str, identity: Identity, deps: Dependencies
) -> tuple[PriceEvidencePublicView, ...]:
    actor = _cost_actor(identity)
    values = await _composition(deps).domain.costing_quotes.list_price_evidence(
        identity.tenant_id, OpportunityId(_identifier(opportunity_id)), actor=actor
    )
    return tuple(project_price_evidence(value) for value in values)


@router.get(
    "/cost-sheets/{sheet_id}/coverage", response_model=CostCoveragePublicView | None
)
async def get_coverage(
    sheet_id: str,
    identity: Identity,
    deps: Dependencies,
    content_hash: Annotated[str | None, Query(pattern=r"^[0-9a-f]{64}$")] = None,
) -> CostCoveragePublicView | None:
    actor = _cost_actor(identity)
    value = await _composition(deps).domain.costing_quotes.get_coverage(
        identity.tenant_id,
        CostSheetId(_identifier(sheet_id)),
        actor=actor,
        content_hash=content_hash,
    )
    if value is None:
        if content_hash is not None:
            raise CostingQuoteNotFoundError()
        return None
    return project_coverage(value)


@router.post("/cost-sheets/{sheet_id}/coverage", response_model=CostCoveragePublicView)
async def confirm_coverage(
    sheet_id: str,
    body: Annotated[CostCoverageCreate, _json_wire(CostCoverageCreate)],
    identity: Identity,
    deps: Dependencies,
    key: Key,
) -> CostCoveragePublicView:
    actor = _cost_actor(identity)
    return await http.confirm_coverage(
        _composition(deps).domain.costing_quotes,
        identity.tenant_id,
        CostSheetId(_identifier(sheet_id)),
        body,
        actor=actor,
        key=key,
    )


@router.get(
    "/cost-sheets/{sheet_id}/scope-confirmations",
    response_model=tuple[CostScopePublicView, ...],
)
async def list_scopes(
    sheet_id: str, identity: Identity, deps: Dependencies
) -> tuple[CostScopePublicView, ...]:
    actor = _cost_actor(identity)
    values = await _composition(deps).domain.costing_freeze.list_scopes(
        identity.tenant_id, CostSheetId(_identifier(sheet_id)), actor=actor
    )
    return tuple(project_scope(value) for value in values)


@router.get(
    "/cost-sheets/{sheet_id}/scope-confirmations/{confirmation_id}",
    response_model=CostScopePublicView,
)
async def get_scope(
    sheet_id: str, confirmation_id: str, identity: Identity, deps: Dependencies
) -> CostScopePublicView:
    actor = _cost_actor(identity)
    value = await _composition(deps).domain.costing_freeze.get_scope(
        identity.tenant_id, _identifier(confirmation_id), actor=actor
    )
    if value.cost_sheet_id != _identifier(sheet_id):
        raise CostingQuoteNotFoundError()
    return project_scope(value)


@router.post(
    "/cost-sheets/{sheet_id}/scope-confirmations", response_model=CostScopePublicView
)
async def confirm_scope(
    sheet_id: str,
    body: Annotated[
        CostScopeConfirmationCommand, _json_wire(CostScopeConfirmationCommand)
    ],
    identity: Identity,
    deps: Dependencies,
    key: Key,
) -> CostScopePublicView:
    actor = _cost_actor(identity)
    composition = _composition(deps)
    return await http.confirm_scope(
        _required(deps.costing),
        composition.domain.preparation,
        identity.tenant_id,
        CostSheetId(_identifier(sheet_id)),
        body,
        actor=actor,
        actor_id=identity.employee.employee_id,
        key=key,
    )


@router.post("/cost-sheets/{sheet_id}/calculate", response_model=CalculationSnapshot)
async def calculate(
    sheet_id: str,
    body: Annotated[CostCalculationCommand, _json_wire(CostCalculationCommand)],
    identity: Identity,
    deps: Dependencies,
) -> CalculationSnapshot:
    actor = _cost_actor(identity)
    composition = _composition(deps)
    return await http.calculate(
        _required(deps.costing),
        composition.domain.preparation,
        identity.tenant_id,
        CostSheetId(_identifier(sheet_id)),
        body,
        actor=actor,
        actor_id=identity.employee.employee_id,
    )


@router.get(
    "/opportunities/{opportunity_id}/quote-context",
    response_model=QuotePreparationPublicView,
)
async def get_context(
    opportunity_id: str, identity: Identity, deps: Dependencies
) -> QuotePreparationPublicView:
    _cost_actor(identity)
    return await _composition(deps).domain.preparation_reads.get(
        identity.tenant_id,
        OpportunityId(_identifier(opportunity_id)),
        actor_id=identity.employee.employee_id,
    )


@router.get("/needs/{need_id}/unit", response_model=NeedUnitPreparationView)
async def get_unit(
    need_id: str, identity: Identity, deps: Dependencies
) -> NeedUnitPreparationView:
    value = await _composition(deps).domain.need_units.get_facts(
        identity.tenant_id,
        ValidatedNeedId(_identifier(need_id)),
        actor_id=identity.employee.employee_id,
    )
    return project_need_unit_preparation(value)


@router.post(
    "/needs/{need_id}/unit-confirmations", response_model=NeedUnitConfirmationPublicView
)
async def confirm_unit(
    need_id: str,
    body: Annotated[
        NeedUnitConfirmationCommand, _json_wire(NeedUnitConfirmationCommand)
    ],
    identity: Identity,
    deps: Dependencies,
    key: Key,
) -> NeedUnitConfirmationPublicView:
    value = await _composition(deps).domain.need_units.confirm(
        identity.tenant_id,
        ValidatedNeedId(_identifier(need_id)),
        body,
        actor_id=identity.employee.employee_id,
        idempotency_key=key,
    )
    return project_need_unit_confirmation(value)


@router.get(
    "/needs/{need_id}/unit-confirmations/{confirmation_id}",
    response_model=NeedUnitConfirmationPublicView,
)
async def get_unit_confirmation(
    need_id: str, confirmation_id: str, identity: Identity, deps: Dependencies
) -> NeedUnitConfirmationPublicView:
    value = await _composition(deps).domain.need_units.get_confirmation(
        identity.tenant_id,
        ValidatedNeedId(_identifier(need_id)),
        _identifier(confirmation_id),
        actor_id=identity.employee.employee_id,
    )
    return project_need_unit_confirmation(value)


@router.get(
    "/opportunities/{opportunity_id}/quotes",
    response_model=tuple[QuoteInternalPublicView, ...],
)
async def list_quotes(
    opportunity_id: str, identity: Identity, deps: Dependencies
) -> tuple[QuoteInternalPublicView, ...]:
    actor = _quote_actor(identity)
    values = await _composition(deps).domain.quotations.list_versions(
        identity.tenant_id, OpportunityId(_identifier(opportunity_id)), actor=actor
    )
    return tuple(project_internal_quote(value) for value in values)


@router.get("/quotes/{quote_id}", response_model=QuoteInternalPublicView)
async def get_quote(
    quote_id: str, identity: Identity, deps: Dependencies
) -> QuoteInternalPublicView:
    actor = _quote_actor(identity)
    return project_internal_quote(
        await _composition(deps).domain.quotations.get(
            identity.tenant_id, QuoteId(_identifier(quote_id)), actor=actor
        )
    )


@router.post(
    "/opportunities/{opportunity_id}/quotes", response_model=QuoteInternalPublicView
)
async def create_quote(
    opportunity_id: str,
    body: Annotated[QuoteDraftCommand, _json_wire(QuoteDraftCommand)],
    identity: Identity,
    deps: Dependencies,
    key: Key,
) -> QuoteInternalPublicView:
    _cost_actor(identity)
    return await http.create_quote(
        _composition(deps).domain.creation,
        identity.tenant_id,
        body,
        actor_id=identity.employee.employee_id,
        key=key,
        opportunity_id=OpportunityId(_identifier(opportunity_id)),
    )


@router.post("/quotes/{quote_id}/revisions", response_model=QuoteInternalPublicView)
async def revise_quote(
    quote_id: str,
    body: Annotated[QuoteDraftCommand, _json_wire(QuoteDraftCommand)],
    identity: Identity,
    deps: Dependencies,
    key: Key,
) -> QuoteInternalPublicView:
    _cost_actor(identity)
    return await http.create_quote(
        _composition(deps).domain.creation,
        identity.tenant_id,
        body,
        actor_id=identity.employee.employee_id,
        key=key,
        replaces_quote_id=QuoteId(_identifier(quote_id)),
    )


@router.post(
    "/quotes/{quote_id}/submit",
    response_model=QuoteApprovalStartResult,
    status_code=202,
    dependencies=[Depends(_empty)],
)
async def submit_quote(
    quote_id: str,
    identity: Identity,
    deps: Dependencies,
    body: Annotated[QuoteEmptyCommand, Body()] = _EMPTY_COMMAND,
) -> QuoteApprovalStartResult:
    _cost_actor(identity)
    return await _composition(deps).approval_starter.start(
        identity.tenant_id,
        QuoteId(_identifier(quote_id)),
        actor_id=identity.employee.employee_id,
    )


@router.get(
    "/opportunities/{opportunity_id}/customer-quote-versions",
    response_model=QuoteCustomerVersionPage,
    responses=FILE_ERRORS,
)
async def customer_versions(
    opportunity_id: str,
    identity: Identity,
    deps: Dependencies,
    limit: Annotated[int, Query(gt=0)],
    before_version: Annotated[int | None, Query(gt=0)] = None,
) -> QuoteCustomerVersionPage:
    return await _required(_file_composition(deps).customer_versions).list_versions(
        identity.tenant_id,
        OpportunityId(_identifier(opportunity_id)),
        actor_id=identity.employee.employee_id,
        before_version=before_version,
        limit=limit,
    )


@router.get(
    "/quotes/{quote_id}/files",
    response_model=tuple[QuoteFileView, ...],
    responses=FILE_ERRORS,
)
async def list_files(
    quote_id: str, identity: Identity, deps: Dependencies
) -> tuple[QuoteFileView, ...]:
    return await _required(_file_composition(deps).domain.files).list_files(
        identity.tenant_id,
        QuoteId(_file_identifier(quote_id, "quote_id")),
        actor_id=identity.employee.employee_id,
    )


@router.post(
    "/quotes/{quote_id}/files",
    response_model=QuoteFileView,
    responses=FILE_ERRORS,
    dependencies=[Depends(_empty)],
)
async def generate_file(
    quote_id: str,
    identity: Identity,
    deps: Dependencies,
    body: Annotated[QuoteEmptyCommand, Body()] = _EMPTY_COMMAND,
) -> QuoteFileView:
    return await _required(_file_composition(deps).files_application).generate(
        identity.tenant_id,
        QuoteId(_file_identifier(quote_id, "quote_id")),
        actor_id=identity.employee.employee_id,
    )


@router.post(
    "/quotes/{quote_id}/files/reconcile",
    response_model=QuoteFileRecoveryResult,
    responses=FILE_ERRORS,
)
async def reconcile_file(
    quote_id: str,
    body: Annotated[QuoteFileRecoveryCommand, _json_wire(QuoteFileRecoveryCommand)],
    identity: Identity,
    deps: Dependencies,
) -> QuoteFileRecoveryResult:
    if body.quote_id != _identifier(quote_id):
        raise ValidationError("报价恢复路径绑定无效")
    return await _required(_file_composition(deps).files_application).reconcile(
        identity.tenant_id,
        QuoteId(quote_id),
        body.original_generation_call_id,
        actor_id=identity.employee.employee_id,
    )


def _pdf(value: tuple[QuoteFileView, bytes]) -> Response:
    """应用已经有界核hash/size及后置授权，HTTP不流式泄出未核字节。"""
    metadata, content = value
    if (
        re.fullmatch(r"quo_[0-7][0-9A-HJKMNP-TV-Z]{25}", metadata.quote_id) is None
        or metadata.template_version not in QUOTE_PDF_TEMPLATE_VERSIONS
    ):
        raise TransientError("报价文件绑定不可用")
    filename = (
        f"{metadata.quote_id}-v{metadata.quote_version}-{metadata.template_version}.pdf"
    )
    return Response(
        content=content,
        media_type="application/pdf",
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.get(
    "/quotes/{quote_id}/files/{file_id}",
    response_class=Response,
    responses=PDF_RESPONSES,
)
async def download_file(
    quote_id: str, file_id: str, identity: Identity, deps: Dependencies
) -> Response:
    return _pdf(
        await _required(_file_composition(deps).files_application).download(
            identity.tenant_id,
            QuoteId(_file_identifier(quote_id, "quote_id")),
            QuoteFileId(_file_identifier(file_id, "file_id")),
            actor_id=identity.employee.employee_id,
        )
    )


@router.get(
    "/quotes/{quote_id}/files/{file_id}/history",
    response_class=Response,
    responses=PDF_RESPONSES,
)
async def history_file(
    quote_id: str, file_id: str, identity: Identity, deps: Dependencies
) -> Response:
    return _pdf(
        await _required(_file_composition(deps).files_application).read_history(
            identity.tenant_id,
            QuoteId(_file_identifier(quote_id, "quote_id")),
            QuoteFileId(_file_identifier(file_id, "file_id")),
            actor_id=identity.employee.employee_id,
        )
    )


@router.post("/evidence/preview", response_model=EvidencePreviewPublicView)
async def preview(
    body: Annotated[EvidencePreviewRequest, _json_wire(EvidencePreviewRequest)],
    identity: Identity,
    deps: Dependencies,
) -> EvidencePreviewPublicView:
    value = await _composition(deps).evidence.preview_reader.read(
        identity.tenant_id, body, actor_id=identity.employee.employee_id
    )
    return http.project_preview(value)


@router.post("/evidence/locator", response_model=EvidenceLocatorPublicView)
async def locator(
    body: Annotated[EvidenceLocateRequest, _json_wire(EvidenceLocateRequest)],
    identity: Identity,
    deps: Dependencies,
) -> EvidenceLocatorPublicView:
    value = await _composition(deps).evidence.preview_reader.read(
        identity.tenant_id, body, actor_id=identity.employee.employee_id
    )
    return http.project_locator(value)
