"""本人 Gmail 网页连接及固定邮件诊断的公开契约。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from shared.authentication import AuthenticationInputInvalid, normalize_login_username
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId, TenantId, UserId


class GmailActor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: TenantId
    employee_id: EmployeeId
    user_id: UserId


class GmailAddress(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def normalize(cls, value: str) -> str:
        try:
            normalized = normalize_login_username(value.strip().lower())
            if "@" not in normalized:
                raise AuthenticationInputInvalid()
            return normalized
        except AuthenticationInputInvalid:
            raise ValueError("邮箱地址无效") from None


class GmailConnectionStatus(BaseModel):
    configured: bool
    email: str | None = None
    mailbox_id: str | None = None
    can_test: bool = False


class GmailAuthorizationStart(BaseModel):
    authorization_url: str = Field(repr=False)


class GmailAuthorizationComplete(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    code: SecretStr = Field(min_length=1, max_length=4096, repr=False)
    state: SecretStr = Field(min_length=32, max_length=128, repr=False)


class GmailTestCommand(GmailAddress):
    request_id: UUID
    confirm_my_mailbox: Literal[True]


class GmailTestResult(BaseModel):
    request_id: UUID
    sender: str
    recipient: str
    subject: str
    body: str
    status: str
    tool_call_id: str | None = None
    provider_ref: str | None = None
    created_at: datetime


def require_gmail_test_role(role: str) -> None:
    """诊断仅限在职企业管理员；不借用正式 Campaign 的许可。"""
    if role != "boss":
        raise PermissionDenied("仅企业管理员可以执行本人邮箱诊断")


class GmailTestTemplate(BaseModel):
    subject: str
    body: str
