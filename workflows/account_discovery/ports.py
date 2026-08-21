"""account_discovery 的窄端口；正文、联系人 PII 与凭证不得持久化到流程。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agent_runtime.base import AgentTask, ChangeSet
from connectors.contact_enrichment.client import ContactEnrichmentResult
from connectors.email_verification.client import EmailVerificationResult
from domains.employees.permissions import Actor as EmployeeActor
from domains.outreach.permissions import Actor as OutreachActor
from shared.schemas.identifiers import (
    ContactPointId,
    NeedHypothesisId,
    ProspectAccountId,
    TenantId,
    UserId,
)


@dataclass(frozen=True)
class AccountDiscoveryTaskInput:
    """从持久业务数据构造的模型安全输入；不含联系人信息。"""

    objective: str
    hypothesis: dict[str, object]
    allowed_countries: tuple[str, ...]


@runtime_checkable
class AccountDiscoveryTaskReader(Protocol):
    async def load(
        self,
        tenant_id: TenantId,
        hypothesis_id: NeedHypothesisId,
        acting_user: UserId,
    ) -> AccountDiscoveryTaskInput: ...


@runtime_checkable
class AccountDiscoveryCapability(Protocol):
    async def run(self, task: AgentTask, context: object) -> ChangeSet: ...


@runtime_checkable
class ContactEnricher(Protocol):
    async def find_contacts(
        self,
        tenant_id: TenantId,
        hypothesis_id: NeedHypothesisId,
        account_id: ProspectAccountId,
        role_hints: tuple[str, ...],
    ) -> ContactEnrichmentResult: ...


@runtime_checkable
class ContactVerifier(Protocol):
    async def verify(
        self, tenant_id: TenantId, contact_point_id: ContactPointId
    ) -> EmailVerificationResult: ...


@dataclass(frozen=True)
class AccountDiscoveryActors:
    """从持久员工身份推导的两域最小 actor；不得由 workflow context 自报。"""

    employee: EmployeeActor
    outreach: OutreachActor


@runtime_checkable
class AccountDiscoveryActorResolver(Protocol):
    async def resolve(
        self, tenant_id: TenantId, acting_user: UserId
    ) -> AccountDiscoveryActors: ...


__all__ = (
    "AccountDiscoveryActorResolver",
    "AccountDiscoveryActors",
    "AccountDiscoveryCapability",
    "AccountDiscoveryTaskInput",
    "AccountDiscoveryTaskReader",
    "ContactEnricher",
    "ContactVerifier",
)
