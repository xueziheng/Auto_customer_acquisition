"""阿里企业邮箱本人连接契约；凭证输入不进入业务资料和模型上下文。"""
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from shared.schemas.identifiers import EmployeeId, TenantId, UserId


class AliyunActor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: TenantId
    employee_id: EmployeeId
    user_id: UserId


class AliyunConnectCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    email: str = Field(min_length=3, max_length=254)
    password: SecretStr = Field(min_length=1, max_length=512, repr=False)
    confirm_connect: Literal[True]

    @field_validator("confirm_connect", mode="before")
    @classmethod
    def explicit_confirmation(cls, value: object) -> object:
        if value is not True:
            raise ValueError("必须明确确认邮箱连接")
        return value

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", value) is None:
            raise ValueError("邮箱地址无效")
        return value.casefold()


class AliyunConnectionStatus(BaseModel):
    configured: bool
    email: str | None = None
    connected_at: datetime | None = None
    smtp_authenticated: bool = False
    imap_authenticated: bool = False
    automatic_reply_enabled: bool = False
