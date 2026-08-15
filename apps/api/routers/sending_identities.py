"""Internal Sending Identity API（仅 boss 可访问）。

GET    /sending-identities                            可用发件身份列表
GET    /sending-identities/{identity_id}              单个身份状态
POST   /sending-identities/{identity_id}/authentication-checks
                                                      只接受幂等键，发起认证检查
"""

from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict

from domains.sending_identity.permissions import SendingIdentityAction
from domains.sending_identity.schemas import IdentityView
from domains.sending_identity.service import AuthenticationCheckRequestView
from shared.errors import ValidationError
from shared.schemas.identifiers import IdempotencyKey, SendingIdentityId

from ..dependencies import (
    ConfiguredApiDependencies,
    get_api_dependencies,
    get_request_identity,
    resolve_sending_identity_access,
)
from ..identity import RequestIdentity
from ..middleware import ApiErrorResponse

router = APIRouter()

_IDENTITY_ID_RE = re.compile(r"sid_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_BOSS_ROLES = frozenset({"boss"})


class AuthenticationCheckRequest(BaseModel):
    """认证检查只接受一个幂等键；绝不接受 SPF/DKIM/DMARC 结果。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    request_key: str


@router.get(
    "/sending-identities",
    response_model=list[IdentityView],
    responses={
        400: {"model": ApiErrorResponse},
        403: {"model": ApiErrorResponse},
    },
)
async def list_sending_identities(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[
        ConfiguredApiDependencies, Depends(get_api_dependencies)
    ],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[IdentityView]:
    """列出当前可参与 Campaign 的发件身份及其认证/预热/信誉状态。"""
    actor = await resolve_sending_identity_access(
        identity,
        dependencies,
        SendingIdentityAction.IDENTITY_LIST,
        allowed_roles=_BOSS_ROLES,
    )
    return await dependencies.sending_identities.list_available_for_campaign(
        identity.tenant_id, limit=limit, actor=actor
    )


@router.get(
    "/sending-identities/{identity_id}",
    response_model=IdentityView,
    responses={
        400: {"model": ApiErrorResponse},
        403: {"model": ApiErrorResponse},
    },
)
async def get_sending_identity(
    identity_id: str,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[
        ConfiguredApiDependencies, Depends(get_api_dependencies)
    ],
) -> IdentityView:
    if _IDENTITY_ID_RE.fullmatch(identity_id) is None:
        raise ValidationError("sending identity id 无效")
    actor = await resolve_sending_identity_access(
        identity,
        dependencies,
        SendingIdentityAction.IDENTITY_READ,
        allowed_roles=_BOSS_ROLES,
    )
    return await dependencies.sending_identities.get(
        identity.tenant_id, SendingIdentityId(identity_id), actor=actor
    )


@router.post(
    "/sending-identities/{identity_id}/authentication-checks",
    response_model=AuthenticationCheckRequestView,
    responses={
        400: {"model": ApiErrorResponse},
        403: {"model": ApiErrorResponse},
    },
)
async def request_authentication_check(
    identity_id: str,
    body: AuthenticationCheckRequest,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[
        ConfiguredApiDependencies, Depends(get_api_dependencies)
    ],
) -> AuthenticationCheckRequestView:
    if _IDENTITY_ID_RE.fullmatch(identity_id) is None:
        raise ValidationError("sending identity id 无效")
    actor = await resolve_sending_identity_access(
        identity,
        dependencies,
        SendingIdentityAction.AUTH_CHECK_BEGIN,
        allowed_roles=_BOSS_ROLES,
    )
    return await dependencies.sending_identities.request_authentication_check(
        identity.tenant_id,
        SendingIdentityId(identity_id),
        IdempotencyKey(body.request_key),
        actor=actor,
    )
