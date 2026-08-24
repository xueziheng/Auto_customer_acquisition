"""Company Playbook 当前配置、版本审批状态与提案入口。"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.routing import APIRoute
from pydantic import BaseModel, BeforeValidator, ConfigDict, model_validator
from starlette.responses import Response

from domains.approvals.service import ApprovalService
from domains.compliance.permissions import ComplianceActor, ComplianceScope
from domains.compliance.schemas import (
    CountryPolicyActiveView,
    CountryPolicyCoverage,
    CountryPolicyField,
    CountryPolicyProposalCreate,
    CountryPolicyVersionView,
)
from domains.compliance.service import ComplianceService
from domains.organization.errors import PlaybookNotConfiguredError
from domains.organization.permissions import (
    OrganizationActor,
    OrganizationScope,
    OrganizationScopeLevel,
)
from domains.organization.schemas import (
    PlaybookActivationView,
    PlaybookProposalCreate,
    PlaybookVersionView,
)
from domains.organization.service import OrganizationService
from shared.errors import PermissionDenied, TransientError
from shared.schemas.identifiers import (
    ApprovalId,
    CountryPolicyVersionId,
    EmployeeId,
    IdempotencyKey,
    PlaybookVersionId,
)
from shared.schemas.provenance import SourceType
from workflows.country_policy_change import country_policy_change_idempotency_key
from workflows.country_policy_change.flow import COUNTRY_POLICY_CHANGE_WORKFLOW_TYPE

from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_request_identity,
)
from ..identity import RequestIdentity
from ..middleware import ApiErrorResponse


class _SettingsRoute(APIRoute):
    """仅为新增国家政策 POST 保留显式 422 合同，不改全局 400 映射。"""

    def get_route_handler(
        self,
    ) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()
        if self.path != "/country-policies/proposals":
            return handler

        async def task7_validation_handler(request: Request) -> Response:
            try:
                return await handler(request)
            except RequestValidationError:
                raise HTTPException(status_code=422) from None

        return task7_validation_handler


router = APIRouter(route_class=_SettingsRoute)

_SAFE_APPLICATION_ERROR_CODES = frozenset(
    {"PLAYBOOK_BASE_VERSION_CONFLICT", "PLAYBOOK_APPROVAL_FACT_INVALID"}
)
_APPROVAL_STATES = frozenset(
    {"pending", "approved", "rejected", "expired", "applied", "apply_failed"}
)
_DECIDED_APPROVAL_STATES = frozenset(
    {"approved", "rejected", "applied", "apply_failed"}
)
_COUNTRY_POLICY_SAFE_APPLICATION_ERROR_CODES = frozenset(
    {
        "COUNTRY_POLICY_BASE_VERSION_CONFLICT",
        "COUNTRY_POLICY_APPROVAL_FACT_INVALID",
    }
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


ContactEnrichmentReason = Literal[
    "COUNTRY_POLICY_NOT_CONFIGURED",
    "CONTACT_ENRICHMENT_NOT_ALLOWED",
    "CONTACT_ENRICHMENT_NOT_COMPOSED",
]


class ContactEnrichmentReadiness(_FrozenModel):
    state: Literal["blocked", "ready"]
    reason_code: ContactEnrichmentReason | None

    @model_validator(mode="after")
    def validate_state_reason(self) -> ContactEnrichmentReadiness:
        if (self.state == "ready") is not (self.reason_code is None):
            raise ValueError("contact enrichment 状态与原因必须唯一一致")
        return self


class PlaybookActiveView(_FrozenModel):
    version: PlaybookVersionView
    activation: PlaybookActivationView


class PlaybookOverview(_FrozenModel):
    configured: bool
    active_version: PlaybookActiveView | None
    contact_enrichment: ContactEnrichmentReadiness

    @model_validator(mode="after")
    def validate_configured_pair(self) -> PlaybookOverview:
        if self.configured is not (self.active_version is not None):
            raise ValueError("configured 与 active_version 必须一致")
        return self


ApprovalStateValue = Literal[
    "proposal_pending_submission",
    "pending",
    "approved",
    "rejected",
    "expired",
    "applied",
    "apply_failed",
]
ApplicationErrorCode = Literal[
    "PLAYBOOK_BASE_VERSION_CONFLICT", "PLAYBOOK_APPROVAL_FACT_INVALID"
]


class PlaybookVersionStatusView(_FrozenModel):
    version: PlaybookVersionView
    approval_id: ApprovalId | None
    application_error_code: ApplicationErrorCode | None
    approval_state: ApprovalStateValue


class PlaybookProposalAccepted(_FrozenModel):
    playbook_version_id: PlaybookVersionId
    run_id: str
    change_set_ref: str


class CountryPolicyOverview(_FrozenModel):
    active_policies: list[CountryPolicyActiveView]
    coverage: CountryPolicyCoverage
    contact_enrichment: ContactEnrichmentReadiness


CountryPolicyApprovalStateValue = Literal[
    "proposal_pending_submission",
    "pending",
    "approved",
    "rejected",
    "expired",
    "applied",
    "apply_failed",
]
CountryPolicyApplicationErrorCode = Literal[
    "COUNTRY_POLICY_BASE_VERSION_CONFLICT",
    "COUNTRY_POLICY_APPROVAL_FACT_INVALID",
]


class CountryPolicyVersionStatusView(_FrozenModel):
    version: CountryPolicyVersionView
    approval_id: ApprovalId | None
    application_error_code: CountryPolicyApplicationErrorCode | None
    approval_state: CountryPolicyApprovalStateValue
    approval_decided_by: EmployeeId | None = None
    approval_decided_at: datetime | None = None

    @model_validator(mode="after")
    def validate_approval_facts(self) -> CountryPolicyVersionStatusView:
        has_decider = self.approval_decided_by is not None
        has_decided_at = self.approval_decided_at is not None
        if has_decider != has_decided_at:
            raise ValueError("审批决定人与时间必须成对")
        has_decision = has_decider and has_decided_at
        if self.approval_state == "proposal_pending_submission":
            if self.approval_id is not None or has_decision:
                raise ValueError("未提交审批的版本不得携带审批事实")
        elif self.approval_id is None:
            raise ValueError("已提交审批的版本必须携带 approval_id")
        if (self.approval_state in _DECIDED_APPROVAL_STATES) is not has_decision:
            raise ValueError("审批状态与决定事实不一致")
        if (
            self.application_error_code is not None
            and self.approval_state != "apply_failed"
        ):
            raise ValueError("只有应用失败状态可以携带错误码")
        return self


class CountryPolicyProposalAccepted(_FrozenModel):
    country_policy_version_id: CountryPolicyVersionId
    run_id: str
    change_set_ref: str


def _adapt_country_policy_json(value: Any) -> Any:
    """把 JSON enum 文本适配为 strict 公共 schema 所需枚举实例。"""
    if not isinstance(value, dict):
        return value
    sources = value.get("field_sources")
    if not isinstance(sources, dict):
        return value
    adapted_sources: dict[object, object] = {}
    for raw_field, raw_source in sources.items():
        try:
            field = (
                raw_field
                if isinstance(raw_field, CountryPolicyField)
                else CountryPolicyField(raw_field)
            )
        except (TypeError, ValueError):
            field = raw_field
        source = raw_source
        if isinstance(raw_source, dict):
            source = dict(raw_source)
            raw_source_type = source.get("source_type")
            try:
                source["source_type"] = (
                    raw_source_type
                    if isinstance(raw_source_type, SourceType)
                    else SourceType(raw_source_type)
                )
            except (TypeError, ValueError):
                pass
        adapted_sources[field] = source
    adapted = dict(value)
    adapted["field_sources"] = adapted_sources
    return adapted


CountryPolicyProposalBody = Annotated[
    CountryPolicyProposalCreate, BeforeValidator(_adapt_country_policy_json)
]


@dataclass(frozen=True)
class SettingsAccess:
    """Settings router 所需的最小组织域身份。"""

    organization_actor: OrganizationActor
    compliance_actor: ComplianceActor


def resolve_settings_access(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
) -> SettingsAccess:
    if (
        identity.employee.role != "boss"
        or not identity.employee.is_active
        or identity.employee.tenant_id != identity.tenant_id
    ):
        raise PermissionDenied("只有老板可以访问 Company Playbook")
    return SettingsAccess(
        organization_actor=OrganizationActor(
            str(identity.employee.employee_id),
            OrganizationScope(
                OrganizationScopeLevel.TENANT,
                identity.tenant_id,
            ),
            "boss",
        ),
        compliance_actor=ComplianceActor(
            actor_id=str(identity.employee.employee_id),
            tenant_id=identity.tenant_id,
            scope=ComplianceScope.TENANT,
            role="boss",
        ),
    )


def _organization_service(
    dependencies: ConfiguredApiDependencies,
) -> OrganizationService:
    service = dependencies.organization
    if service is None:
        raise TransientError("Settings 组织服务暂不可用")
    return service


def _approval_service(
    dependencies: ConfiguredApiDependencies,
) -> ApprovalService:
    service = dependencies.approvals
    if service is None:
        raise TransientError("Settings 审批服务暂不可用")
    return service


def _compliance_service(
    dependencies: ConfiguredApiDependencies,
) -> ComplianceService:
    service = dependencies.compliance
    if service is None:
        raise TransientError("Settings 合规服务暂不可用")
    return service


def _contact_enrichment_readiness(
    coverage: CountryPolicyCoverage,
    *,
    composed: bool,
) -> ContactEnrichmentReadiness:
    if type(composed) is not bool:
        raise TransientError("联系人补全组合状态无效")
    if coverage.active_policy_count == 0:
        return ContactEnrichmentReadiness(
            state="blocked", reason_code="COUNTRY_POLICY_NOT_CONFIGURED"
        )
    if coverage.contact_enrichment_allowed_count == 0:
        return ContactEnrichmentReadiness(
            state="blocked", reason_code="CONTACT_ENRICHMENT_NOT_ALLOWED"
        )
    if not composed:
        return ContactEnrichmentReadiness(
            state="blocked", reason_code="CONTACT_ENRICHMENT_NOT_COMPOSED"
        )
    return ContactEnrichmentReadiness(state="ready", reason_code=None)


async def _read_contact_enrichment_readiness(
    dependencies: ConfiguredApiDependencies,
    identity: RequestIdentity,
    context: SettingsAccess,
) -> tuple[CountryPolicyCoverage, ContactEnrichmentReadiness]:
    coverage = await _compliance_service(dependencies).get_coverage(
        identity.tenant_id,
        actor=context.compliance_actor,
    )
    if not isinstance(coverage, CountryPolicyCoverage):
        raise TransientError("Settings 国家政策覆盖事实无效")
    return coverage, _contact_enrichment_readiness(
        coverage,
        composed=dependencies.contact_enrichment_composed,
    )


def _decimal_string(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _map_active(active: Any) -> PlaybookActiveView:
    version = PlaybookVersionView(
        playbook_version_id=active.playbook_version_id,
        version_number=active.version_number,
        content_hash=active.content_hash,
        base_version_id=active.base_version_id,
        base_content_hash=active.base_content_hash,
        company_type=active.company_type,
        minimum_deal_amount=_decimal_string(active.minimum_deal_value.amount),
        minimum_deal_currency=str(active.minimum_deal_value.currency),
        excluded_categories=active.excluded_categories,
        sourcing_regions=active.sourcing_regions,
        excluded_countries=active.excluded_countries,
        monthly_budget_credits=active.monthly_budget_credits,
        approval_requirements=active.approval_requirements,
        supply_capabilities_note=active.supply_capabilities_note,
        proposed_by=active.proposed_by,
        proposed_at=active.proposed_at,
        content_provenance=active.content_provenance,
        change_set_ref=active.change_set_ref,
    )
    activation = PlaybookActivationView(
        activation_id=active.activation_id,
        playbook_version_id=active.playbook_version_id,
        content_hash=active.content_hash,
        approval_id=active.approval_id,
        change_set_ref=active.change_set_ref,
        approved_by=active.approved_by,
        approved_at=active.approved_at,
        activated_by=active.activated_by,
        activated_at=active.activated_at,
    )
    return PlaybookActiveView(version=version, activation=activation)


@router.get("/playbook", response_model=PlaybookOverview)
async def get_playbook_overview(
    context: Annotated[SettingsAccess, Depends(resolve_settings_access)],
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[
        ConfiguredApiDependencies, Depends(get_api_dependencies)
    ],
) -> PlaybookOverview:
    organization = _organization_service(dependencies)
    _, contact_enrichment = await _read_contact_enrichment_readiness(
        dependencies, identity, context
    )
    try:
        active = await organization.get_playbook(
            identity.tenant_id, actor=context.organization_actor
        )
    except PlaybookNotConfiguredError:
        return PlaybookOverview(
            configured=False,
            active_version=None,
            contact_enrichment=contact_enrichment,
        )
    return PlaybookOverview(
        configured=True,
        active_version=_map_active(active),
        contact_enrichment=contact_enrichment,
    )


@router.get("/playbook/versions", response_model=list[PlaybookVersionStatusView])
async def list_playbook_versions(
    context: Annotated[SettingsAccess, Depends(resolve_settings_access)],
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[
        ConfiguredApiDependencies, Depends(get_api_dependencies)
    ],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[PlaybookVersionStatusView]:
    organization = _organization_service(dependencies)
    approvals = _approval_service(dependencies)
    versions = await organization.list_versions(
        identity.tenant_id,
        actor=context.organization_actor,
        limit=limit,
    )
    result: list[PlaybookVersionStatusView] = []
    for version in versions:
        approval = await approvals.get_by_change_set(
            identity.tenant_id, version.change_set_ref
        )
        if approval is None:
            result.append(
                PlaybookVersionStatusView(
                    version=version,
                    approval_id=None,
                    application_error_code=None,
                    approval_state="proposal_pending_submission",
                )
            )
            continue
        if (
            approval.approval_type != "playbook_change"
            or approval.change_set_ref != version.change_set_ref
            or approval.state not in _APPROVAL_STATES
        ):
            raise TransientError("Settings Playbook 审批关联事实不一致")
        safe_error = (
            approval.application_error_code
            if approval.application_error_code in _SAFE_APPLICATION_ERROR_CODES
            else None
        )
        result.append(
            PlaybookVersionStatusView(
                version=version,
                approval_id=ApprovalId(approval.approval_id),
                application_error_code=cast(ApplicationErrorCode | None, safe_error),
                approval_state=cast(ApprovalStateValue, approval.state),
            )
        )
    return result


@router.post(
    "/playbook/proposals",
    response_model=PlaybookProposalAccepted,
    status_code=202,
)
async def propose_playbook(
    body: PlaybookProposalCreate,
    idempotency_key_header: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ],
    context: Annotated[SettingsAccess, Depends(resolve_settings_access)],
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[
        ConfiguredApiDependencies, Depends(get_api_dependencies)
    ],
) -> PlaybookProposalAccepted:
    organization = _organization_service(dependencies)
    proposal = await organization.propose_playbook(
        identity.tenant_id,
        body,
        actor=context.organization_actor,
        idempotency_key=IdempotencyKey(idempotency_key_header),
    )
    run_id = await dependencies.workflow_engine.start(
        identity.tenant_id,
        "playbook_change",
        str(proposal.playbook_version_id),
        {
            "playbook_version_id": str(proposal.playbook_version_id),
            "content_hash": proposal.content_hash,
            "change_set_ref": proposal.change_set_ref,
            "proposed_by": str(identity.employee.employee_id),
        },
        f"playbook-change:{identity.tenant_id}:{idempotency_key_header}",
    )
    return PlaybookProposalAccepted(
        playbook_version_id=proposal.playbook_version_id,
        run_id=str(run_id),
        change_set_ref=proposal.change_set_ref,
    )


@router.get("/country-policies", response_model=CountryPolicyOverview)
async def get_country_policy_overview(
    context: Annotated[SettingsAccess, Depends(resolve_settings_access)],
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> CountryPolicyOverview:
    compliance = _compliance_service(dependencies)
    active_policies = await compliance.list_active_policies(
        identity.tenant_id,
        actor=context.compliance_actor,
        limit=200,
    )
    coverage, contact_enrichment = await _read_contact_enrichment_readiness(
        dependencies, identity, context
    )
    return CountryPolicyOverview(
        active_policies=active_policies,
        coverage=coverage,
        contact_enrichment=contact_enrichment,
    )


@router.get(
    "/country-policies/versions",
    response_model=list[CountryPolicyVersionStatusView],
)
async def list_country_policy_versions(
    context: Annotated[SettingsAccess, Depends(resolve_settings_access)],
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    country: Annotated[str, Query(min_length=1, max_length=64)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[CountryPolicyVersionStatusView]:
    versions = await _compliance_service(dependencies).list_versions(
        identity.tenant_id,
        country,
        actor=context.compliance_actor,
        limit=limit,
    )
    approvals = _approval_service(dependencies)
    result: list[CountryPolicyVersionStatusView] = []
    for version in versions:
        approval = await approvals.get_by_change_set(
            identity.tenant_id, version.change_set_ref
        )
        if approval is None:
            result.append(
                CountryPolicyVersionStatusView(
                    version=version,
                    approval_id=None,
                    application_error_code=None,
                    approval_state="proposal_pending_submission",
                )
            )
            continue
        if (
            approval.approval_type != COUNTRY_POLICY_CHANGE_WORKFLOW_TYPE
            or approval.change_set_ref != version.change_set_ref
            or approval.state not in _APPROVAL_STATES
        ):
            raise TransientError("Settings 国家政策审批关联事实不一致")
        safe_error = (
            approval.application_error_code
            if approval.application_error_code
            in _COUNTRY_POLICY_SAFE_APPLICATION_ERROR_CODES
            else None
        )
        decided_by = approval.decided_by_employee
        decided_at = approval.decided_at
        has_decision = decided_by is not None and decided_at is not None
        if (decided_by is None) != (decided_at is None) or (
            approval.state in _DECIDED_APPROVAL_STATES
        ) is not has_decision:
            raise TransientError("Settings 国家政策审批决定事实不一致")
        result.append(
            CountryPolicyVersionStatusView(
                version=version,
                approval_id=ApprovalId(approval.approval_id),
                application_error_code=cast(
                    CountryPolicyApplicationErrorCode | None, safe_error
                ),
                approval_state=cast(CountryPolicyApprovalStateValue, approval.state),
                approval_decided_by=decided_by,
                approval_decided_at=decided_at,
            )
        )
    return result


@router.post(
    "/country-policies/proposals",
    response_model=CountryPolicyProposalAccepted,
    status_code=202,
    responses={
        422: {
            "model": ApiErrorResponse,
            "description": "请求参数无效",
        }
    },
)
async def propose_country_policy(
    body: CountryPolicyProposalBody,
    idempotency_key_header: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=200),
    ],
    context: Annotated[SettingsAccess, Depends(resolve_settings_access)],
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> CountryPolicyProposalAccepted:
    proposal = await _compliance_service(dependencies).propose_country_policy(
        identity.tenant_id,
        body,
        actor=context.compliance_actor,
        idempotency_key=IdempotencyKey(idempotency_key_header),
    )
    run_id = await dependencies.workflow_engine.start(
        identity.tenant_id,
        COUNTRY_POLICY_CHANGE_WORKFLOW_TYPE,
        str(proposal.country_policy_version_id),
        {
            "country_policy_version_id": str(proposal.country_policy_version_id),
            "country_key": proposal.country_key,
            "content_hash": proposal.content_hash,
            "change_set_ref": proposal.change_set_ref,
            "proposed_by": str(identity.employee.employee_id),
        },
        country_policy_change_idempotency_key(
            identity.tenant_id, proposal.country_policy_version_id
        ),
    )
    return CountryPolicyProposalAccepted(
        country_policy_version_id=proposal.country_policy_version_id,
        run_id=str(run_id),
        change_set_ref=proposal.change_set_ref,
    )


__all__ = (
    "ContactEnrichmentReadiness",
    "CountryPolicyOverview",
    "CountryPolicyProposalAccepted",
    "CountryPolicyVersionStatusView",
    "PlaybookActiveView",
    "PlaybookOverview",
    "PlaybookProposalAccepted",
    "PlaybookVersionStatusView",
    "SettingsAccess",
    "router",
)
