"""Company Playbook 当前配置、版本审批状态与提案入口。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any, Literal, cast

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, model_validator

from domains.approvals.service import ApprovalService
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
from shared.schemas.identifiers import ApprovalId, IdempotencyKey, PlaybookVersionId

from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_request_identity,
)
from ..identity import RequestIdentity

router = APIRouter()

_SAFE_APPLICATION_ERROR_CODES = frozenset(
    {"PLAYBOOK_BASE_VERSION_CONFLICT", "PLAYBOOK_APPROVAL_FACT_INVALID"}
)
_APPROVAL_STATES = frozenset(
    {"pending", "approved", "rejected", "expired", "applied", "apply_failed"}
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class ContactEnrichmentBlocker(_FrozenModel):
    state: Literal["blocked"] = "blocked"
    reason_code: Literal["COUNTRY_POLICY_NOT_CONFIGURED"] = (
        "COUNTRY_POLICY_NOT_CONFIGURED"
    )


class PlaybookActiveView(_FrozenModel):
    version: PlaybookVersionView
    activation: PlaybookActivationView


class PlaybookOverview(_FrozenModel):
    configured: bool
    active_version: PlaybookActiveView | None
    contact_enrichment: ContactEnrichmentBlocker = ContactEnrichmentBlocker()

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


@dataclass(frozen=True)
class SettingsAccess:
    """Settings router 所需的最小组织域身份。"""

    organization_actor: OrganizationActor


def resolve_settings_access(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
) -> SettingsAccess:
    if identity.employee.role != "boss":
        raise PermissionDenied("只有老板可以访问 Company Playbook")
    return SettingsAccess(
        organization_actor=OrganizationActor(
            str(identity.employee.employee_id),
            OrganizationScope(
                OrganizationScopeLevel.TENANT,
                identity.tenant_id,
            ),
            "boss",
        )
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
    try:
        active = await organization.get_playbook(
            identity.tenant_id, actor=context.organization_actor
        )
    except PlaybookNotConfiguredError:
        return PlaybookOverview(configured=False, active_version=None)
    return PlaybookOverview(configured=True, active_version=_map_active(active))


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


__all__ = (
    "PlaybookActiveView",
    "PlaybookOverview",
    "PlaybookProposalAccepted",
    "PlaybookVersionStatusView",
    "SettingsAccess",
    "router",
)
