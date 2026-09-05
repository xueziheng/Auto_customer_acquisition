"""老板需求探索指令：自然语言提案、确认、否决与启动。"""

from __future__ import annotations

import re
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.trade_manager import TradeManagerAgent
from domains.directives.errors import DirectiveProposalNotFoundError
from domains.directives.schemas import ProposalView, SourcingAdmissionConfigInput
from domains.directives.service import DirectiveService
from domains.employees.permissions import EmployeeAction
from shared.errors import (
    InvalidStateTransition,
    PermissionDenied,
    TransientError,
    ValidationError,
)
from shared.schemas.identifiers import EmployeeId

from ..dependencies import (
    ConfiguredApiDependencies,
    document_idempotency_header,
    get_api_dependencies,
    get_request_identity,
    raw_idempotency_key,
    require_employee_action,
)
from ..identity import RequestIdentity
from ..middleware import ApiErrorResponse
from ..research import ResearchAccessService, read_discovery_execution
from ..research_schemas import DiscoveryExecutionView, DiscoveryProposalView

router = APIRouter()

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_PROPOSAL_RE = re.compile(rf"dpr_{_ULID}")
_BOSS_ONLY = frozenset({"boss"})
_SOURCING_ADMISSION_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"model": ApiErrorResponse},
    403: {"model": ApiErrorResponse},
    404: {"model": ApiErrorResponse},
    409: {"model": ApiErrorResponse},
    503: {"model": ApiErrorResponse},
}


class DiscoveryProposalBody(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    message: str = Field(min_length=1, max_length=10_000)


class DiscoveryConfirmationResponse(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    proposal_id: str
    directive_id: str
    run_id: str
    workflow_type: str


class DiscoveryRejectionResponse(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    proposal_id: str
    state: str


class SourcingAdmissionProposalBody(BaseModel):
    """老板显式提交的完整准入配置；不从文本猜测授权字段。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    message: str = Field(min_length=1, max_length=10_000)
    mode: str = Field(pattern="^cluster_ranked$")
    automatic_admission_enabled: bool
    batch_limit: int = Field(ge=1, le=50)


class SourcingAdmissionConfirmationResponse(BaseModel):
    """确认后返回实际生效版本，不包含或启动 Workflow。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    proposal_id: str
    directive_id: str
    directive_version: int = Field(ge=1)
    mode: str = Field(pattern="^cluster_ranked$")
    automatic_admission_enabled: bool
    batch_limit: int = Field(ge=1, le=50)


def _services(
    dependencies: ConfiguredApiDependencies,
) -> tuple[DirectiveService, TradeManagerAgent]:
    if dependencies.directives is None or dependencies.trade_manager is None:
        raise TransientError("老板指令服务尚未配置")
    return dependencies.directives, dependencies.trade_manager


def _directives(dependencies: ConfiguredApiDependencies) -> DirectiveService:
    if dependencies.directives is None:
        raise TransientError("老板指令服务尚未配置")
    return dependencies.directives


def _research(
    dependencies: ConfiguredApiDependencies, identity: RequestIdentity
) -> ResearchAccessService:
    return dependencies.research_access or ResearchAccessService(
        identity.tenant_id, None, configured=False
    )


def _proposal_id(value: str) -> str:
    if _PROPOSAL_RE.fullmatch(value) is None:
        raise ValidationError("指令提案标识无效")
    return value


_write_gate = Depends(
    require_employee_action(
        EmployeeAction.OWNERSHIP_LOCK,
        allowed_roles=_BOSS_ONLY,
    )
)


@router.post(
    "/sourcing-admission-proposals",
    response_model=ProposalView,
    dependencies=[_write_gate],
    responses=_SOURCING_ADMISSION_ERRORS,
)
async def create_sourcing_admission_proposal(
    body: SourcingAdmissionProposalBody,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> ProposalView:
    if body.message != body.message.strip():
        raise ValidationError("老板指令原话无效")
    directives = _directives(dependencies)
    enabled_text = "启用" if body.automatic_admission_enabled else "关闭"
    config = SourcingAdmissionConfigInput(
        mode=body.mode,
        automatic_admission_enabled=body.automatic_admission_enabled,
        batch_limit=body.batch_limit,
    )
    try:
        proposal_id = await directives.submit_sourcing_admission_proposal(
            identity.tenant_id,
            body.message,
            config,
            f"按需求簇规模排序；自动寻源准入将{enabled_text}；每轮上限为 {body.batch_limit} 个案例",
            [
                f"自动寻源准入：{enabled_text}",
                f"每轮最多启动 {body.batch_limit} 个寻源案例",
            ],
            "api:sourcing-admission-v1",
            submitted_by=identity.employee.employee_id,
        )
        proposal = await directives.get_proposal(identity.tenant_id, proposal_id)
    except (PermissionDenied, InvalidStateTransition):
        raise
    except Exception:  # noqa: BLE001 - Directive 存储异常不得进入 HTTP
        raise TransientError("寻源准入提案状态暂不可用") from None
    if not isinstance(proposal, ProposalView):
        raise TransientError("寻源准入提案状态暂不可用")
    return proposal


@router.post(
    "/sourcing-admission-proposals/{proposal_id}/confirm",
    response_model=SourcingAdmissionConfirmationResponse,
    dependencies=[_write_gate, Depends(document_idempotency_header)],
    responses=_SOURCING_ADMISSION_ERRORS,
)
async def confirm_sourcing_admission_proposal(
    proposal_id: str,
    request: Request,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> SourcingAdmissionConfirmationResponse:
    raw_idempotency_key(request)
    normalized = _proposal_id(proposal_id)
    directives = _directives(dependencies)
    try:
        proposal = await directives.get_proposal(identity.tenant_id, normalized)
    except DirectiveProposalNotFoundError:
        raise HTTPException(status_code=404) from None
    except Exception:  # noqa: BLE001 - Directive 存储异常不得进入 HTTP
        raise TransientError("寻源准入提案状态暂不可用") from None
    if proposal is None:
        raise HTTPException(status_code=404)
    if not isinstance(proposal, ProposalView):
        raise TransientError("寻源准入提案状态暂不可用")
    expected_section = (
        proposal.sourcing_admission_mode,
        proposal.automatic_sourcing_admission_enabled,
        proposal.sourcing_admission_batch_limit,
    )
    if (
        expected_section[0] != "cluster_ranked"
        or type(expected_section[1]) is not bool
        or type(expected_section[2]) is not int
        or not 1 <= expected_section[2] <= 50
    ):
        raise TransientError("寻源准入提案状态暂不可用")
    try:
        await directives.confirm_proposal(
            identity.tenant_id, normalized, identity.employee.employee_id
        )
        active = await directives.get_active(identity.tenant_id)
    except DirectiveProposalNotFoundError:
        raise HTTPException(status_code=404) from None
    except (PermissionDenied, InvalidStateTransition):
        raise
    except Exception:  # noqa: BLE001 - Directive 存储异常不得进入 HTTP
        raise TransientError("寻源准入生效版本暂不可确认") from None
    if (
        active is None
        or active.source_proposal_id != normalized
        or active.sourcing_admission_mode != "cluster_ranked"
        or type(active.automatic_sourcing_admission_enabled) is not bool
        or type(active.sourcing_admission_batch_limit) is not int
        or not 1 <= active.sourcing_admission_batch_limit <= 50
        or (
            active.sourcing_admission_mode,
            active.automatic_sourcing_admission_enabled,
            active.sourcing_admission_batch_limit,
        )
        != expected_section
    ):
        raise TransientError("寻源准入生效版本暂不可确认")
    return SourcingAdmissionConfirmationResponse(
        proposal_id=normalized,
        directive_id=active.directive_id,
        directive_version=active.version,
        mode=active.sourcing_admission_mode,
        automatic_admission_enabled=active.automatic_sourcing_admission_enabled,
        batch_limit=active.sourcing_admission_batch_limit,
    )


@router.post(
    "/discovery-proposals",
    response_model=DiscoveryProposalView,
    dependencies=[_write_gate],
)
async def create_discovery_proposal(
    body: DiscoveryProposalBody,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> DiscoveryProposalView:
    if body.message != body.message.strip():
        raise ValidationError("老板指令原话无效")
    directives, trade_manager = _services(dependencies)
    draft = await trade_manager.propose_discovery(body.message)
    proposal_id = await directives.submit_discovery_proposal(
        identity.tenant_id,
        body.message,
        draft.plan,
        draft.interpretation_summary,
        list(draft.expected_behavior_changes),
        trade_manager.model,
    )
    proposal = await directives.get_proposal(identity.tenant_id, proposal_id)
    return await _research(dependencies, identity).proposal(
        identity.tenant_id, proposal
    )


@router.get(
    "/discovery-proposals/{proposal_id}",
    response_model=DiscoveryProposalView,
    dependencies=[_write_gate],
)
async def get_discovery_proposal(
    proposal_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> DiscoveryProposalView:
    directives = _directives(dependencies)
    proposal = await directives.get_proposal(
        identity.tenant_id, _proposal_id(proposal_id)
    )
    return await _research(dependencies, identity).proposal(
        identity.tenant_id, proposal
    )


@router.post(
    "/discovery-proposals/{proposal_id}/confirm",
    response_model=DiscoveryConfirmationResponse,
    dependencies=[_write_gate],
)
async def confirm_discovery_proposal(
    proposal_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> DiscoveryConfirmationResponse:
    proposal_id = _proposal_id(proposal_id)
    directives = _directives(dependencies)
    proposal = await directives.get_proposal(identity.tenant_id, proposal_id)
    projected = await _research(dependencies, identity).proposal(
        identity.tenant_id, proposal
    )
    if proposal.state == "confirmed" and projected.execution_mode == "research_only":
        execution = await read_discovery_execution(
            identity.tenant_id,
            proposal,
            directives,
            _research(dependencies, identity),
            dependencies.research_execution,
        )
        if execution.state == "unknown":
            raise TransientError("无法核实工作流启动状态，请先只读刷新")
        if execution.state == "not_started" and not execution.can_resume:
            raise InvalidStateTransition(
                "当前提案不能恢复启动，请检查配置、预算与生效状态"
            )
    if proposal.state == "pending_confirmation" and not projected.can_confirm:
        raise InvalidStateTransition(
            f"提案暂不能确认：{projected.confirmation_blocked_reason}"
        )
    if proposal.state == "pending_confirmation":
        await directives.confirm_proposal(
            identity.tenant_id,
            proposal_id,
            identity.employee.employee_id,
        )
    elif proposal.state != "confirmed":
        raise InvalidStateTransition("只有待确认或已确认提案可以启动需求探索")
    active = await directives.get_active(identity.tenant_id)
    if active is None or active.source_proposal_id != proposal_id:
        raise InvalidStateTransition("该提案已不是当前生效指令，不能启动需求探索")
    confirmed = await directives.get_proposal(identity.tenant_id, proposal_id)
    if confirmed.decided_by_id is None:
        raise InvalidStateTransition("需求探索提案缺少确认人")
    async with dependencies.employees(identity.tenant_id) as employees:
        confirmer = await employees.get_employee(
            identity.tenant_id,
            EmployeeId(confirmed.decided_by_id),
            actor=dependencies.employee_lookup_actor,
        )
    if not confirmer.is_active or confirmer.role != "boss" or confirmer.user_id is None:
        raise InvalidStateTransition("需求探索确认人缺少当前用户映射")
    run_id = await dependencies.workflow_engine.start(
        identity.tenant_id,
        "demand_discovery",
        proposal_id,
        {
            "proposal_id": proposal_id,
            "acting_user_id": str(confirmer.user_id),
        },
        f"demand-discovery:{proposal_id}",
    )
    return DiscoveryConfirmationResponse(
        proposal_id=proposal_id,
        directive_id=active.directive_id,
        run_id=str(run_id),
        workflow_type="demand_discovery",
    )


@router.get(
    "/discovery-proposals/{proposal_id}/execution",
    response_model=DiscoveryExecutionView,
    dependencies=[_write_gate],
)
async def get_discovery_execution(
    proposal_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> DiscoveryExecutionView:
    """读取原提案对应 Run；仅返回恢复所需的安全元数据。"""
    directives = _directives(dependencies)
    proposal = await directives.get_proposal(
        identity.tenant_id, _proposal_id(proposal_id)
    )
    return await read_discovery_execution(
        identity.tenant_id,
        proposal,
        directives,
        _research(dependencies, identity),
        dependencies.research_execution,
    )


@router.post(
    "/discovery-proposals/{proposal_id}/reject",
    response_model=DiscoveryRejectionResponse,
    dependencies=[_write_gate],
)
async def reject_discovery_proposal(
    proposal_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> DiscoveryRejectionResponse:
    proposal_id = _proposal_id(proposal_id)
    directives = _directives(dependencies)
    await directives.reject_proposal(
        identity.tenant_id,
        proposal_id,
        identity.employee.employee_id,
    )
    return DiscoveryRejectionResponse(
        proposal_id=proposal_id,
        state="rejected",
    )


__all__ = ("router",)
