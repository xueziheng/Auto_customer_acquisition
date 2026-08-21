"""Customer Discovery —— 潜在企业、联系人、评分。

GET /prospects/accounts          企业列表（按 ABAC 范围过滤）
GET /prospects/accounts/{id}     详情：来源信号、假设、联系人、归属
GET /prospects/accounts/{id}/score   打分快照 + 门槛/因子解释
POST /prospects/accounts/{id}/assign 手动分配（走 employees 归属锁）
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from domains.employees.permissions import EmployeeAction
from domains.prospecting.schemas import (
    ProspectAccountDetailView,
    ProspectAccountView,
    ProspectContactDetailView,
)
from domains.prospecting.service import ProspectingService
from shared.errors import TransientError, ValidationError
from shared.schemas.identifiers import ProspectAccountId

from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_request_identity,
    require_employee_action,
)
from ..identity import RequestIdentity

router = APIRouter()

_ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
_ACCOUNT_RE = re.compile(rf"acc_{_ULID}")
_HYPOTHESIS_RE = re.compile(rf"hyp_{_ULID}")
_CAMPAIGN_RE = re.compile(rf"cmp_{_ULID}")
_BOSS_ONLY = frozenset({"boss"})


class AccountDiscoveryStartBody(BaseModel):
    """账户发现唯一可提交输入；身份与租户必须来自服务端。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    hypothesis_id: str
    campaign_id: str
    role_hints: tuple[str, ...] = Field(max_length=10)
    assessment_ref: str


class AccountDiscoveryStartResponse(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    run_id: str
    workflow_type: str
    subject_ref: str


def _prospecting(
    dependencies: ConfiguredApiDependencies,
) -> ProspectingService:
    if dependencies.prospecting is None:
        raise TransientError("潜客服务尚未配置")
    return dependencies.prospecting


@router.get(
    "/accounts",
    response_model=list[ProspectAccountView],
    dependencies=[
        Depends(
            require_employee_action(
                EmployeeAction.OWNERSHIP_READ, allowed_roles=_BOSS_ONLY
            )
        )
    ],
)
async def list_accounts(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[
        ConfiguredApiDependencies, Depends(get_api_dependencies)
    ],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[ProspectAccountView]:
    return await _prospecting(dependencies).list_accounts(
        identity.tenant_id, limit=limit
    )


@router.get(
    "/accounts/{account_id}",
    response_model=ProspectAccountDetailView,
    dependencies=[
        Depends(
            require_employee_action(
                EmployeeAction.OWNERSHIP_READ, allowed_roles=_BOSS_ONLY
            )
        )
    ],
)
async def get_account(
    account_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[
        ConfiguredApiDependencies, Depends(get_api_dependencies)
    ],
) -> ProspectAccountDetailView:
    if _ACCOUNT_RE.fullmatch(account_id) is None:
        raise ValidationError("潜在企业标识无效")
    return await _prospecting(dependencies).get_account_detail(
        identity.tenant_id, ProspectAccountId(account_id)
    )


@router.get(
    "/accounts/{account_id}/contacts",
    response_model=list[ProspectContactDetailView],
    dependencies=[
        Depends(
            require_employee_action(
                EmployeeAction.OWNERSHIP_READ, allowed_roles=_BOSS_ONLY
            )
        )
    ],
)
async def list_contacts(
    account_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[
        ConfiguredApiDependencies, Depends(get_api_dependencies)
    ],
) -> list[ProspectContactDetailView]:
    if _ACCOUNT_RE.fullmatch(account_id) is None:
        raise ValidationError("潜在企业标识无效")
    return await _prospecting(dependencies).list_contacts_for_account(
        identity.tenant_id, ProspectAccountId(account_id)
    )


@router.post(
    "/discoveries",
    response_model=AccountDiscoveryStartResponse,
    dependencies=[
        Depends(
            require_employee_action(
                EmployeeAction.OWNERSHIP_LOCK, allowed_roles=_BOSS_ONLY
            )
        )
    ],
)
async def start_discovery(
    body: AccountDiscoveryStartBody,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[
        ConfiguredApiDependencies, Depends(get_api_dependencies)
    ],
) -> AccountDiscoveryStartResponse:
    if (
        _HYPOTHESIS_RE.fullmatch(body.hypothesis_id) is None
        or _CAMPAIGN_RE.fullmatch(body.campaign_id) is None
        or not body.assessment_ref
        or body.assessment_ref != body.assessment_ref.strip()
        or any(
            not value or value != value.strip() or len(value) > 200
            for value in body.role_hints
        )
    ):
        raise ValidationError("账户发现请求无效")
    run_id = await dependencies.workflow_engine.start(
        identity.tenant_id,
        "account_discovery",
        body.hypothesis_id,
        {
            "hypothesis_id": body.hypothesis_id,
            "campaign_id": body.campaign_id,
            "acting_user_id": str(identity.employee.employee_id),
            "role_hints": list(dict.fromkeys(body.role_hints)),
            "assessment_ref": body.assessment_ref,
        },
        f"account-discovery:{body.hypothesis_id}:{body.campaign_id}",
    )
    return AccountDiscoveryStartResponse(
        run_id=str(run_id),
        workflow_type="account_discovery",
        subject_ref=body.hypothesis_id,
    )
