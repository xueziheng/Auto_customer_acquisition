"""本人客户资料准备契约；公开观察与员工适配推断分离，邮箱仍未验证。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Field

from domains.prospecting.customer_followup import CustomerHypothesisOption
from domains.prospecting.schemas import (
    ContactType,
    LegalBasisInput,
    ProspectAccountView,
    ProspectContactDetailView,
    SubjectType,
)
from shared.errors import ValidationError
from shared.schemas.provenance import Provenance


def exact_text(value: str) -> str:
    """拒绝空白、首尾空白与隐藏控制符，不改写员工提交的来源文字。"""
    if (
        not value
        or value != value.strip()
        or any(ord(c) < 32 for c in value if c != "\n")
    ):
        raise ValueError("字段必须非空且不含首尾空白或控制符")
    return value


def public_url(value: str) -> str:
    """只接受无凭证的HTTP公开来源URL；此入口不发网络请求。"""
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("需要无凭证的公开来源URL")
    return value


Text = Annotated[str, Field(min_length=1, max_length=2000), AfterValidator(exact_text)]
Url = Annotated[Text, AfterValidator(public_url)]


@dataclass(frozen=True)
class AccountProfilePatch:
    """已确认来源的空字段补充；同值复用首次来源，冲突不得覆盖。"""

    entity_type: str
    industry: str
    provenance: Provenance


class CustomerPreparationCommand(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    entity_type: Annotated[Text, Field(max_length=100)]
    industry: Annotated[Text, Field(max_length=200)]
    source_url: Url
    page_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_artifact_ref: str = Field(pattern=r"^art_[0-7][0-9A-HJKMNP-TV-Z]{25}$")
    observed_at: AwareDatetime
    public_observation: Text
    product_category: Annotated[Text, Field(max_length=200)]
    fit_reasoning: Text
    public_email: Annotated[Text, Field(max_length=320)] | None = None
    legal_basis: LegalBasisInput | None = None
    legal_basis_confirmed: bool = False

    def require_contact_basis(self) -> None:
        """缺真实依据来源/评估引用或人工确认时拒绝，绝不默认为同意或客户关系。"""
        basis = self.legal_basis
        if self.public_email is None:
            if basis is not None or self.legal_basis_confirmed:
                raise ValidationError("没有邮箱时不能提交联系方式处理依据")
            return
        if (
            basis is None
            or not self.legal_basis_confirmed
            or not basis.source
            or basis.source != basis.source.strip()
            or not basis.assessment_ref
            or basis.assessment_ref != basis.assessment_ref.strip()
            or basis.source_url != self.source_url
            or basis.subject_type is not SubjectType.LEGAL_ENTITY
            or basis.contact_type is not ContactType.ROLE_BASED
        ):
            raise ValidationError("公开邮箱需要人工确认的真实处理依据及评估记录引用")
        if basis.collected_at.tzinfo is None:
            raise ValidationError("法律依据采集时间必须带时区")
        if basis.collected_at.astimezone(UTC) > datetime.now(UTC):
            raise ValidationError("法律依据采集时间不能在未来")


class CustomerPreparationView(BaseModel):
    allocation_id: str
    account: ProspectAccountView
    hypotheses: tuple[CustomerHypothesisOption, ...]
    contacts: tuple[ProspectContactDetailView, ...]
    can_prepare: bool
