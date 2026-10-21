"""已验收私有会话的持续收信配置；不接受任意游标或邮件正文。"""
from pydantic import BaseModel, ConfigDict, Field

from shared.schemas.email_inbound import InboundRoute


class QQContinuousSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    route: InboundRoute
    source_trial_id: str = Field(pattern=r"^qqt_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    alias_evidence_id: str = Field(pattern=r"^qae_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    authorization_ref: str = Field(min_length=1, max_length=200)
    poll_interval_seconds: int = Field(default=60, strict=True, ge=60, le=3600)
