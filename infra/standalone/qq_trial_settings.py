"""显式单次启动配置；不包含采集后才知道的 QQ 编号，不扩大旧私有范围。"""
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.schemas.email_inbound import InboundRoute
from shared.schemas.identifiers import EmployeeId, UserId
from shared.schemas.qq_trial import QQTrialLimits


class QQSentReadWindow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    folder: Literal["Sent", "Sent Messages", "&XfJT0ZAB-"]
    uidvalidity: int = Field(strict=True, ge=1, le=4294967295)
    first_uid: int = Field(strict=True, ge=1, le=4294967295)
    last_uid: int = Field(strict=True, ge=1, le=4294967295)
    timeout_seconds: int = Field(strict=True, ge=1, le=60)
    header_bytes: int = Field(default=16384, strict=True, ge=1, le=16384)

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if not 0 <= self.last_uid - self.first_uid < 200:
            raise ValueError("Sent 采集必须限定明确的小范围 UID")
        return self


class QQDeliveryReadWindow(BaseModel):
    """显式授权的收件方单 UID 原件，不能使用文件导出或搜索结果充当证据。"""
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    employee_id: EmployeeId = Field(pattern=r"^emp_[A-Za-z0-9]+$", max_length=40)
    user_id: UserId = Field(min_length=1, max_length=128)
    authorization_source: Literal["explicit_user_request"]
    authorization_ref: str = Field(min_length=1, max_length=200)
    folder: Literal["INBOX"]
    uidvalidity: int = Field(strict=True, ge=1, le=4294967295)
    uid: int = Field(strict=True, ge=1, le=4294967295)
    timeout_seconds: int = Field(strict=True, ge=1, le=60)
    header_bytes: int = Field(default=16384, strict=True, ge=1, le=16384)


class QQTrialLaunchSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    enabled: Literal[True]
    trial_id: str = Field(pattern=r"^qqt_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    route: InboundRoute
    alias_evidence_id: str = Field(pattern=r"^qae_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    classifier_user_id: str = Field(min_length=1, max_length=128)
    authorization_ref: str = Field(min_length=1, max_length=200)
    authorization_source: Literal["explicit_user_request"]
    canonical_header: str = Field(repr=False,
        pattern=r"^<[a-z0-9-]{1,32}\.[0-9a-f]{64}@messages\.tradeos\.invalid>$")
    starts_at: datetime
    expires_at: datetime
    limits: QQTrialLimits
    sent: QQSentReadWindow
    delivery: QQDeliveryReadWindow | None = None

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.delivery is not None and (self.delivery.timeout_seconds > self.sent.timeout_seconds
            or self.delivery.header_bytes > self.sent.header_bytes):
            raise ValueError("收件方原件读取不能扩大准备阶段预算")
        if (self.starts_at.tzinfo is None or self.expires_at.tzinfo is None
            or not 0 < (self.expires_at-self.starts_at).total_seconds() <= self.limits.duration_seconds
            or self.route.mailbox_alias == "qq-project-reply"
            or not self.canonical_header.startswith("<"+self.route.route_id+".")
            or min(self.limits.sessions, self.limits.searches, self.limits.headers) < 2):
            raise ValueError("单次启动范围无效或不足以包含准备阶段")
        return self
