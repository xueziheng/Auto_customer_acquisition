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
