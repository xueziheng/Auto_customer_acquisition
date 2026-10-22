"""Agent 网页事实提取的独立契约，不接受人工确认字段或客户需求断言。"""
from datetime import datetime
from typing import Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from shared.schemas.identifiers import ArtifactId, TenantId


class AgentProfileFact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    value: str = Field(min_length=1, max_length=200)
    quote: str = Field(min_length=1, max_length=2000)


class AgentAccountProfilePatch(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    entity_type: AgentProfileFact
    industry: AgentProfileFact
    artifact_id: ArtifactId
    source_url: str
    page_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    extracted_by: str = Field(pattern=r"^agent:[a-zA-Z0-9:_./-]{1,180}$")
    extracted_at: AwareDatetime


class AgentProfileSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    tenant_id: TenantId
    artifact_id: ArtifactId
    source_url: str
    page_hash: str
    observed_at: datetime
    text: str = Field(repr=False)


class AgentProfileEvidenceReader(Protocol):
    async def read(self, tenant_id: TenantId, artifact_id: ArtifactId) -> AgentProfileSnapshot:
        """读取并核验Gateway实际返回的本企业不可变快照，不接受模型自报正文。"""
        ...


class AgentPreparationObservation(BaseModel):
    """模型只能提交带引文的事实分类和独立适配推断，无概率或人工确认。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    entity_type: AgentProfileFact
    industry: AgentProfileFact
    subject_type: str = Field(pattern=r"^(legal_entity|natural_person|unknown)$")
    legal_entity_quote: str = Field(min_length=1, max_length=2000)
    public_email: str = Field(min_length=3, max_length=320, repr=False)
    email_quote: str = Field(min_length=1, max_length=2000, repr=False)
    fit_quote: str = Field(min_length=1, max_length=2000)
    fit_reasoning: str = Field(min_length=1, max_length=4000)
    matched: bool
    category: str = Field(min_length=1, max_length=200)

    def require_evidence(self, text: str, domain: str, categories: tuple[str, ...]) -> None:
        """只接受官网明文法人分类和确定的职务地址；不猜个人或外域邮箱。"""
        from shared.errors import ValidationError
        quotes = (self.entity_type.quote, self.industry.quote, self.legal_entity_quote,
                  self.email_quote, self.fit_quote)
        if any(not quote.strip() or quote not in text for quote in quotes):
            raise ValidationError("客户准备缺少逐字公开证据")
        parts = self.public_email.split("@")
        role_names = {"info", "sales", "contact", "office", "enquiries", "inquiries",
                      "purchasing", "procurement", "purchase", "business", "export", "imports"}
        if (self.subject_type != "legal_entity" or len(parts) != 2 or parts[0].lower() not in role_names
            or parts[1].lower() != domain.lower().removeprefix("www.")
            or self.public_email not in self.email_quote):
            raise ValidationError("客户准备缺少企业法人职务邮箱依据")
        if not self.matched or self.category not in categories:
            raise ValidationError("客户准备缺少产品适配依据")
