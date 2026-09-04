"""Product & Supply Center 的安全视图；列表不含供应商、成本或客户报价。"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict

from domains.approvals.schemas import ApprovalReaderIdentity, CatalogApprovalLinkState
from domains.products.errors import ProductNotFoundError
from domains.products.permissions import ProductActor, ProductRole
from domains.products.schemas import (
    CatalogCultivationCaseView,
    CatalogProductProposalView,
    CatalogProposalEvaluationView,
    CatalogProposalPolicyContent,
    CatalogProposalPolicyView,
    ProductSupplyCardView,
)
from domains.products.service import (
    CatalogProposalService,
    ProductCustomerView,
    ProductInternalView,
    ProductSalesView,
    ProductService,
)
from shared.errors import (
    IdempotencyConflict,
    PermissionDenied,
    TransientError,
    ValidationError,
)
from shared.schemas.identifiers import (
    ApprovalId,
    CatalogCultivationCaseId,
    CatalogProductProposalId,
    CatalogProposalEvaluationId,
    ProductId,
    TenantId,
)
from workflows.catalog_product_proposal.application import CatalogProductApplication

from ..dependencies import (
    ConfiguredApiDependencies,
    document_idempotency_header,
    get_api_dependencies,
    get_request_identity,
    raw_idempotency_key,
)
from ..identity import RequestIdentity
from ..middleware import ApiErrorResponse

router = APIRouter()

_PRODUCT_ID = re.compile(r"prd_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_EVALUATION_ID = re.compile(r"cpe_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_PROPOSAL_ID = re.compile(r"cpr_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_CASE_ID = re.compile(r"ccc_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_INTERNAL_ROLES = frozenset({"boss", "product", "sourcing", "finance"})
_POLICY_PROPOSER_ROLES = frozenset({"product", "sourcing"})
_SALES_ROLES = frozenset({"boss", "product", "sourcing", "sales"})
_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ApiErrorResponse},
    403: {"model": ApiErrorResponse},
    404: {"model": ApiErrorResponse},
    503: {"model": ApiErrorResponse},
}


class _CatalogResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CatalogPolicyApiView(_CatalogResponse):
    policy: CatalogProposalPolicyView
    approval: CatalogApprovalLinkState | None


class CatalogProposalApiView(_CatalogResponse):
    proposal: CatalogProductProposalView
    approval: CatalogApprovalLinkState | None


class CatalogCultivationApiView(_CatalogResponse):
    cultivation_case: CatalogCultivationCaseView
    approval: CatalogApprovalLinkState


def _product_id(value: str) -> ProductId:
    if _PRODUCT_ID.fullmatch(value) is None:
        raise ValidationError("产品标识无效")
    return ProductId(value)


def _actor(identity: RequestIdentity, *, allowed_roles: frozenset[str]) -> ProductActor:
    """产品 actor 严格从已解析员工身份派生，body/query/header 无权表达角色。"""

    if identity.employee.role not in allowed_roles:
        raise PermissionDenied("API role gate 默认拒绝")
    return ProductActor(
        actor_id=str(identity.employee.employee_id),
        role=ProductRole(identity.employee.role),
        tenant_id=identity.tenant_id,
    )


def _products(dependencies: ConfiguredApiDependencies) -> ProductService:
    if dependencies.products is None:
        raise TransientError("产品服务尚未配置")
    return dependencies.products


def _catalog_products(dependencies: ConfiguredApiDependencies) -> CatalogProposalService:
    if dependencies.catalog_products is None:
        raise TransientError("目录产品服务尚未配置")
    return dependencies.catalog_products


def _catalog_application(
    dependencies: ConfiguredApiDependencies,
) -> CatalogProductApplication:
    if dependencies.catalog_product_application is None:
        raise TransientError("目录产品应用服务尚未配置")
    return dependencies.catalog_product_application


def _catalog_id(value: str, pattern: re.Pattern[str], kind: type[str]):
    if pattern.fullmatch(value) is None:
        raise ValidationError("目录对象标识无效")
    return kind(value)


def _reader(identity: RequestIdentity) -> ApprovalReaderIdentity:
    return ApprovalReaderIdentity.model_validate(
        {
            "employee_id": identity.employee.employee_id,
            "role": identity.employee.role,
        }
    )


async def _approval_link(
    dependencies: ConfiguredApiDependencies,
    identity: RequestIdentity,
    approval_id: ApprovalId | None,
    expected_type: str,
) -> CatalogApprovalLinkState | None:
    if approval_id is None:
        return None
    if dependencies.approvals is None:
        raise TransientError("目录审批关联暂不可用")
    try:
        linked = await dependencies.approvals.get_catalog_link_state_for_reader(
            identity.tenant_id,
            approval_id,
            reader=_reader(identity),
        )
        result = CatalogApprovalLinkState.model_validate(
            linked.model_dump(mode="python")
        )
        if result.approval_id != approval_id or result.approval_type != expected_type:
            raise ValueError("catalog approval subject mismatch")
        return result
    except Exception:  # noqa: BLE001 -- 联结失败不得伪装为空或泄露审批包
        raise TransientError("目录审批关联暂不可用") from None


async def _catalog_get(
    awaitable,
    model,
    *,
    expected_field: str,
    expected_id: str,
    tenant_id: TenantId | None = None,
):
    try:
        value = await awaitable
        result = model.model_validate(value.model_dump(mode="python"))
        if getattr(result, expected_field) != expected_id:
            raise ValueError("catalog detail subject mismatch")
        if tenant_id is not None and result.facts.tenant_id != tenant_id:
            raise ValueError("catalog evaluation tenant mismatch")
        return result
    except ProductNotFoundError as error:
        raise HTTPException(status_code=404) from error
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001 -- 读取未知态固定脱敏为可重试失败
        raise TransientError("目录产品读取暂不可用") from None


async def _required_approval_link(
    dependencies: ConfiguredApiDependencies,
    identity: RequestIdentity,
    approval_id: ApprovalId,
) -> CatalogApprovalLinkState:
    approval = await _approval_link(
        dependencies,
        identity,
        approval_id,
        "catalog_product_cultivation",
    )
    if approval is None:
        raise TransientError("目录审批关联暂不可用")
    return approval


_POLICY_BODY_SCHEMA = CatalogProposalPolicyContent.model_json_schema()


@router.post(
    "/catalog-policies",
    response_model=CatalogPolicyApiView,
    status_code=status.HTTP_202_ACCEPTED,
    responses={**_ERRORS, 409: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": _POLICY_BODY_SCHEMA}},
        }
    },
)
async def submit_catalog_policy(
    request: Request,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    _document_header: Annotated[None, Depends(document_idempotency_header)],
) -> CatalogPolicyApiView:
    actor = _actor(identity, allowed_roles=_POLICY_PROPOSER_ROLES)
    idempotency_key = raw_idempotency_key(request)
    try:
        content = CatalogProposalPolicyContent.model_validate(await request.json())
    except Exception:  # noqa: BLE001 -- body 契约失败固定为安全 422
        raise HTTPException(status_code=422) from None
    try:
        candidate = await _catalog_application(
            dependencies
        ).submit_policy_candidate(
            identity.tenant_id,
            content,
            idempotency_key=idempotency_key,
            actor=actor,
        )
    except IdempotencyConflict as error:
        raise HTTPException(status_code=409) from error
    try:
        policy = CatalogProposalPolicyView.model_validate(
            candidate.model_dump(mode="python")
        )
    except Exception:  # noqa: BLE001 -- 应用边界未知态固定脱敏为可重试失败
        raise TransientError("目录策略候选读取暂不可用")
    return CatalogPolicyApiView(
        policy=policy,
        approval=await _approval_link(
            dependencies,
            identity,
            policy.approval_id,
            "catalog_proposal_policy_change",
        ),
    )


@router.get(
    "/catalog-policies/active",
    response_model=CatalogPolicyApiView | None,
    responses=_ERRORS,
)
async def get_active_catalog_policy(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> CatalogPolicyApiView | None:
    actor = _actor(identity, allowed_roles=_INTERNAL_ROLES)
    try:
        value = await _catalog_products(dependencies).get_active_policy(
            identity.tenant_id, actor=actor
        )
        policy = (
            None
            if value is None
            else CatalogProposalPolicyView.model_validate(
                value.model_dump(mode="python")
            )
        )
    except Exception:  # noqa: BLE001
        raise TransientError("目录产品读取暂不可用") from None
    if policy is None:
        return None
    return CatalogPolicyApiView(
        policy=policy,
        approval=await _approval_link(
            dependencies,
            identity,
            policy.approval_id,
            "catalog_proposal_policy_change",
        ),
    )


@router.get(
    "/catalog-policies",
    response_model=list[CatalogPolicyApiView],
    responses=_ERRORS,
)
async def list_catalog_policies(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[CatalogPolicyApiView]:
    actor = _actor(identity, allowed_roles=_INTERNAL_ROLES)
    policies = await _catalog_list(
        _catalog_products(dependencies).list_policy_versions(
            identity.tenant_id, actor=actor, limit=limit
        ),
        CatalogProposalPolicyView,
        limit=limit,
    )
    return [
        CatalogPolicyApiView(
            policy=policy,
            approval=await _approval_link(
                dependencies,
                identity,
                policy.approval_id,
                "catalog_proposal_policy_change",
            ),
        )
        for policy in policies
    ]


@router.get(
    "/catalog-evaluations",
    response_model=list[CatalogProposalEvaluationView],
    responses=_ERRORS,
)
async def list_catalog_evaluations(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[CatalogProposalEvaluationView]:
    actor = _actor(identity, allowed_roles=_INTERNAL_ROLES)
    return list(
        await _catalog_list(
            _catalog_products(dependencies).list_evaluations(
                identity.tenant_id, actor=actor, limit=limit
            ),
            CatalogProposalEvaluationView,
            limit=limit,
            tenant_id=identity.tenant_id,
        )
    )


@router.get(
    "/catalog-evaluations/{evaluation_id}",
    response_model=CatalogProposalEvaluationView,
    responses=_ERRORS,
)
async def get_catalog_evaluation(
    evaluation_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> CatalogProposalEvaluationView:
    actor = _actor(identity, allowed_roles=_INTERNAL_ROLES)
    checked_evaluation_id = _catalog_id(
        evaluation_id, _EVALUATION_ID, CatalogProposalEvaluationId
    )
    return await _catalog_get(
        _catalog_products(dependencies).get_evaluation(
            identity.tenant_id,
            checked_evaluation_id,
            actor=actor,
        ),
        CatalogProposalEvaluationView,
        expected_field="evaluation_id",
        expected_id=checked_evaluation_id,
        tenant_id=identity.tenant_id,
    )


@router.get(
    "/catalog-proposals",
    response_model=list[CatalogProposalApiView],
    responses=_ERRORS,
)
async def list_catalog_proposals(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[CatalogProposalApiView]:
    actor = _actor(identity, allowed_roles=_INTERNAL_ROLES)
    proposals = await _catalog_list(
        _catalog_products(dependencies).list_proposals(
            identity.tenant_id, actor=actor, limit=limit
        ),
        CatalogProductProposalView,
        limit=limit,
    )
    return [
        CatalogProposalApiView(
            proposal=proposal,
            approval=await _approval_link(
                dependencies,
                identity,
                proposal.approval_id,
                "catalog_product_cultivation",
            ),
        )
        for proposal in proposals
    ]


@router.get(
    "/catalog-proposals/{proposal_id}",
    response_model=CatalogProposalApiView,
    responses=_ERRORS,
)
async def get_catalog_proposal(
    proposal_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> CatalogProposalApiView:
    actor = _actor(identity, allowed_roles=_INTERNAL_ROLES)
    checked_proposal_id = _catalog_id(
        proposal_id, _PROPOSAL_ID, CatalogProductProposalId
    )
    proposal = await _catalog_get(
        _catalog_products(dependencies).get_proposal(
            identity.tenant_id,
            checked_proposal_id,
            actor=actor,
        ),
        CatalogProductProposalView,
        expected_field="proposal_id",
        expected_id=checked_proposal_id,
    )
    return CatalogProposalApiView(
        proposal=proposal,
        approval=await _approval_link(
            dependencies,
            identity,
            proposal.approval_id,
            "catalog_product_cultivation",
        ),
    )


@router.get(
    "/catalog-cultivation-cases",
    response_model=list[CatalogCultivationApiView],
    responses=_ERRORS,
)
async def list_catalog_cultivation_cases(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[CatalogCultivationApiView]:
    actor = _actor(identity, allowed_roles=_INTERNAL_ROLES)
    cases = await _catalog_list(
        _catalog_products(dependencies).list_cultivation_cases(
            identity.tenant_id, actor=actor, limit=limit
        ),
        CatalogCultivationCaseView,
        limit=limit,
    )
    return [
        CatalogCultivationApiView(
            cultivation_case=case,
            approval=await _required_approval_link(
                dependencies, identity, case.approval_id
            ),
        )
        for case in cases
    ]


@router.get(
    "/catalog-cultivation-cases/{case_id}",
    response_model=CatalogCultivationApiView,
    responses=_ERRORS,
)
async def get_catalog_cultivation_case(
    case_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> CatalogCultivationApiView:
    actor = _actor(identity, allowed_roles=_INTERNAL_ROLES)
    checked_case_id = _catalog_id(case_id, _CASE_ID, CatalogCultivationCaseId)
    case = await _catalog_get(
        _catalog_products(dependencies).get_cultivation_case(
            identity.tenant_id,
            checked_case_id,
            actor=actor,
        ),
        CatalogCultivationCaseView,
        expected_field="cultivation_case_id",
        expected_id=checked_case_id,
    )
    approval = await _required_approval_link(
        dependencies, identity, case.approval_id
    )
    return CatalogCultivationApiView(cultivation_case=case, approval=approval)


async def _catalog_list(
    awaitable,
    model,
    *,
    limit: int,
    tenant_id: TenantId | None = None,
):
    try:
        values = await awaitable
        if len(values) > limit:
            raise ValueError("catalog list limit overrun")
        results = tuple(
            model.model_validate(value.model_dump(mode="python")) for value in values
        )
        if tenant_id is not None and any(
            result.facts.tenant_id != tenant_id for result in results
        ):
            raise ValueError("catalog evaluation tenant mismatch")
        return results
    except Exception:  # noqa: BLE001 -- 真实空集合之外不得伪装为空
        raise TransientError("目录产品读取暂不可用") from None


@router.get("", response_model=list[ProductSupplyCardView], responses=_ERRORS)
async def list_supply_cards(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    source_only: bool | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 50,
) -> list[ProductSupplyCardView]:
    return list(
        await _products(dependencies).list_supply_cards(
            identity.tenant_id,
            actor=_actor(identity, allowed_roles=_INTERNAL_ROLES),
            source_only=source_only,
            limit=limit,
        )
    )


@router.get(
    "/{product_id}/internal", response_model=ProductInternalView, responses=_ERRORS
)
async def get_internal_view(
    product_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> ProductInternalView:
    try:
        return await _products(dependencies).get_internal_view(
            identity.tenant_id,
            _product_id(product_id),
            actor=_actor(identity, allowed_roles=_INTERNAL_ROLES),
        )
    except ProductNotFoundError as error:
        raise HTTPException(status_code=404) from error


@router.get("/{product_id}/sales", response_model=ProductSalesView, responses=_ERRORS)
async def get_sales_view(
    product_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> ProductSalesView:
    try:
        return await _products(dependencies).get_sales_view(
            identity.tenant_id,
            _product_id(product_id),
            actor=_actor(identity, allowed_roles=_SALES_ROLES),
        )
    except ProductNotFoundError as error:
        raise HTTPException(status_code=404) from error


@router.get(
    "/{product_id}/customer", response_model=ProductCustomerView, responses=_ERRORS
)
async def get_customer_view(
    product_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> ProductCustomerView:
    try:
        return await _products(dependencies).get_customer_view(
            identity.tenant_id,
            _product_id(product_id),
            actor=_actor(identity, allowed_roles=_SALES_ROLES),
        )
    except ProductNotFoundError as error:
        raise HTTPException(status_code=404) from error


__all__ = ("router",)
