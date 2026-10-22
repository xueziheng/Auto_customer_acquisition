"""本人客户单联系点验证的安全命令与事实视图。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from domains.prospecting.schemas import ContactPointDetailView, VerificationStatus


class CustomerVerificationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    contact_point_id: str = Field(pattern=r"^cp_[0-7][0-9A-HJKMNP-TV-Z]{25}$")


class CustomerVerificationOption(BaseModel):
    contact: ContactPointDetailView
    can_verify: bool
    blocking_reasons: tuple[str, ...]


class CustomerVerificationView(BaseModel):
    allocation_id: str
    account_id: str
    contacts: tuple[CustomerVerificationOption, ...]
    can_verify: bool
    blocking_reasons: tuple[str, ...]


class CustomerVerificationResult(BaseModel):
    allocation_id: str
    contact_point_id: str
    result: VerificationStatus
    provider: str
    checked_at: datetime
    cache_hit: bool
    privacy_erased: bool = False
