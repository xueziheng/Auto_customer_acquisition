"""私有回信测试命令：只用于当前部署允许的两个本人控制邮箱。"""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from shared.schemas.identifiers import (
    CampaignId,
    ContactPointId,
    EnrollmentId,
    ProspectAccountId,
    SendingIdentityId,
)


class PrivateReplyTestPrepare(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    campaign_id: CampaignId = Field(pattern=r"^cmp_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    sending_identity_id: SendingIdentityId = Field(pattern=r"^sid_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    confirm_controlled_recipient: Literal[True]

    @field_validator("confirm_controlled_recipient", mode="before")
    @classmethod
    def explicit(cls, value: object) -> Literal[True]:
        if value is not True:
            raise ValueError("须明确确认收件邮箱由本人控制且本次仅为测试")
        return value


class PrivateReplyTestPrepared(BaseModel):
    account_id: ProspectAccountId
    contact_point_id: ContactPointId
    enrollment_id: EnrollmentId
    purpose: Literal["private_reply_test"] = "private_reply_test"
    sent: Literal[False] = False


class PrivateReplyTestOption(BaseModel):
    campaign_id: CampaignId
    sending_identity_id: SendingIdentityId
    name: str


class PrivateReplyTestOptions(BaseModel):
    enabled: bool = False
    options: tuple[PrivateReplyTestOption, ...] = ()
