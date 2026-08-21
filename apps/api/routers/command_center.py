"""老板需求探索指令：自然语言提案、确认、否决与启动。"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from agent_runtime.trade_manager import TradeManagerAgent
from domains.directives.schemas import ProposalView
from domains.directives.service import DirectiveService
from domains.employees.permissions import EmployeeAction
from shared.errors import InvalidStateTransition, TransientError, ValidationError

from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_request_identity,
    require_employee_action,
)
from ..identity import RequestIdentity

router = APIRouter()

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_PROPOSAL_RE = re.compile(rf"dpr_{_ULID}")
_BOSS_ONLY = frozenset({"boss"})


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


def _services(
    dependencies: ConfiguredApiDependencies,
) -> tuple[DirectiveService, TradeManagerAgent]:
    if dependencies.directives is None or dependencies.trade_manager is None:
        raise TransientError("老板指令服务尚未配置")
    return dependencies.directives, dependencies.trade_manager


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
    "/discovery-proposals",
    response_model=ProposalView,
    dependencies=[_write_gate],
)
async def create_discovery_proposal(
    body: DiscoveryProposalBody,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[
        ConfiguredApiDependencies, Depends(get_api_dependencies)
    ],
) -> ProposalView:
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
    return await directives.get_proposal(identity.tenant_id, proposal_id)


@router.get(
    "/discovery-proposals/{proposal_id}",
    response_model=ProposalView,
    dependencies=[_write_gate],
)
async def get_discovery_proposal(
    proposal_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[
        ConfiguredApiDependencies, Depends(get_api_dependencies)
    ],
) -> ProposalView:
    directives, _trade_manager = _services(dependencies)
    return await directives.get_proposal(
        identity.tenant_id, _proposal_id(proposal_id)
    )


@router.post(
    "/discovery-proposals/{proposal_id}/confirm",
    response_model=DiscoveryConfirmationResponse,
    dependencies=[_write_gate],
)
async def confirm_discovery_proposal(
    proposal_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[
        ConfiguredApiDependencies, Depends(get_api_dependencies)
    ],
) -> DiscoveryConfirmationResponse:
    proposal_id = _proposal_id(proposal_id)
    directives, _trade_manager = _services(dependencies)
    proposal = await directives.get_proposal(identity.tenant_id, proposal_id)
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
    run_id = await dependencies.workflow_engine.start(
        identity.tenant_id,
        "demand_discovery",
        proposal_id,
        {
            "proposal_id": proposal_id,
            "acting_user_id": confirmed.decided_by_id,
        },
        f"demand-discovery:{proposal_id}",
    )
    return DiscoveryConfirmationResponse(
        proposal_id=proposal_id,
        directive_id=active.directive_id,
        run_id=str(run_id),
        workflow_type="demand_discovery",
    )


@router.post(
    "/discovery-proposals/{proposal_id}/reject",
    response_model=DiscoveryRejectionResponse,
    dependencies=[_write_gate],
)
async def reject_discovery_proposal(
    proposal_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[
        ConfiguredApiDependencies, Depends(get_api_dependencies)
    ],
) -> DiscoveryRejectionResponse:
    proposal_id = _proposal_id(proposal_id)
    directives, _trade_manager = _services(dependencies)
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
