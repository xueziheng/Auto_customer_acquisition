"""Internal Sending Identity API（仅 boss 可访问）。

GET    /sending-identities                            可用发件身份列表
GET    /sending-identities/{identity_id}              单个身份状态
POST   /sending-identities/{identity_id}/authentication-checks
                                                      只接受幂等键，发起认证检查
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from domains.sending_identity.permissions import SendingIdentityAction
from domains.sending_identity.schemas import (
    DomainRole,
    IdentityRegisterRequest,
    IdentityView,
)
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


class ConfirmedIdentityMutation(BaseModel):
    """本次确认必须来自JSON布尔true，数字或字符串不能冒充人工确认。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def require_boolean_confirmation(cls, value: object) -> object:
        if value is not True:
            raise ValueError("必须明确确认发件身份操作")
        return value


class IdentityRegistrationBody(ConfirmedIdentityMutation):
    """只接受安全登记字段和本次人工确认；不接受授权断言或认证结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    address: str = Field(min_length=1, max_length=320, strict=True)
    domain: str = Field(min_length=1, max_length=253, strict=True)
    role: DomainRole
    display_name: str | None = Field(default=None, max_length=200, strict=True)
    connector_ref: str | None = Field(default=None, max_length=200, strict=True)
    confirmed: Literal[True]


class IdentityWarmupBody(ConfirmedIdentityMutation):
    """预热目标由原域验证；不开放曲线或日期。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    target_daily_volume: int = Field(strict=True, ge=5, le=100)
    confirmed: Literal[True]


@router.get("/sending-identities/management", response_model=list[IdentityView])
async def list_managed_identities(
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[IdentityView]:
    """读取全部状态的有界管理列表，保持 Campaign 原列表语义。"""
    actor = await resolve_sending_identity_access(
        identity, dependencies, SendingIdentityAction.IDENTITY_LIST, allowed_roles=_BOSS_ROLES,
    )
    return await dependencies.sending_identities.list_for_management(
        identity.tenant_id, limit=limit, actor=actor,
    )


@router.post("/sending-identities", response_model=IdentityView)
async def register_identity(
    body: IdentityRegistrationBody,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> IdentityView:
    """人工确认登记并读取原域返回的精确 winner，不查询列表猜身份。"""
    actor = await resolve_sending_identity_access(
        identity, dependencies, SendingIdentityAction.IDENTITY_REGISTER, allowed_roles=_BOSS_ROLES,
    )
    identity_id = await dependencies.sending_identities.register(
        identity.tenant_id,
        IdentityRegisterRequest(
            address=body.address, domain=body.domain, role=body.role,
            display_name=body.display_name, connector_ref=body.connector_ref,
        ),
        actor=actor,
    )
    return await dependencies.sending_identities.get(identity.tenant_id, identity_id, actor=actor)


@router.post("/sending-identities/{identity_id}/warmup", response_model=IdentityView)
async def start_identity_warmup(
    identity_id: str,
    body: IdentityWarmupBody,
    identity: Annotated[RequestIdentity, Depends(get_request_identity)],
    dependencies: Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)],
) -> IdentityView:
    """启动固定真实日期曲线；读取失败不撤销服务端已经提交的动作。"""
    if _IDENTITY_ID_RE.fullmatch(identity_id) is None:
        raise ValidationError("sending identity id 无效")
    actor = await resolve_sending_identity_access(
        identity, dependencies, SendingIdentityAction.WARMUP_START, allowed_roles=_BOSS_ROLES,
    )
    sid = SendingIdentityId(identity_id)
    await dependencies.sending_identities.start_warmup(
        identity.tenant_id, sid, body.target_daily_volume, actor=actor,
    )
    return await dependencies.sending_identities.get(identity.tenant_id, sid, actor=actor)


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
