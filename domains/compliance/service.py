"""合规域公共服务 Protocol；没有直接更新、默认或强制激活旁路。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.compliance.permissions import ComplianceActor
from domains.compliance.schemas import (
    CountryPolicyAction,
    CountryPolicyActivationView,
    CountryPolicyApprovalFact,
    CountryPolicyChangeSnapshot,
    CountryPolicyCoverage,
    CountryPolicyDecision,
    CountryPolicyProposalCreate,
    CountryPolicyProposalResult,
    CountryPolicyVersionView,
)
from shared.schemas.identifiers import CountryPolicyVersionId, IdempotencyKey, TenantId


@runtime_checkable
class ComplianceService(Protocol):
    async def get_active_policy(
        self, tenant_id: TenantId, country: str, *, actor: ComplianceActor
    ) -> CountryPolicyVersionView:
        """读取精确国家键的当前版本；无配置时不生成法律默认值。"""
        ...

    async def get_version(
        self,
        tenant_id: TenantId,
        version_id: CountryPolicyVersionId,
        *,
        actor: ComplianceActor,
    ) -> CountryPolicyVersionView: ...

    async def list_active_policies(
        self,
        tenant_id: TenantId,
        *,
        actor: ComplianceActor,
        limit: int = 50,
    ) -> list[CountryPolicyVersionView]: ...

    async def list_versions(
        self,
        tenant_id: TenantId,
        country: str,
        *,
        actor: ComplianceActor,
        limit: int = 50,
    ) -> list[CountryPolicyVersionView]: ...

    async def get_country_policy_decision(
        self,
        tenant_id: TenantId,
        country: str,
        action: CountryPolicyAction,
        *,
        actor: ComplianceActor,
    ) -> CountryPolicyDecision: ...

    async def get_coverage(
        self, tenant_id: TenantId, *, actor: ComplianceActor
    ) -> CountryPolicyCoverage: ...

    async def get_change_snapshot(
        self,
        tenant_id: TenantId,
        version_id: CountryPolicyVersionId,
        *,
        actor: ComplianceActor,
    ) -> CountryPolicyChangeSnapshot:
        """供工作流读取 base/current/candidate；不持久化快照全文。"""
        ...

    async def propose_country_policy(
        self,
        tenant_id: TenantId,
        command: CountryPolicyProposalCreate,
        *,
        actor: ComplianceActor,
        idempotency_key: IdempotencyKey,
    ) -> CountryPolicyProposalResult: ...

    async def activate_country_policy(
        self,
        tenant_id: TenantId,
        version_id: CountryPolicyVersionId,
        approval: CountryPolicyApprovalFact,
        *,
        actor: ComplianceActor,
    ) -> CountryPolicyActivationView:
        """只接受工作流从已批准记录构造的窄事实。"""
        ...


__all__ = ("ComplianceService",)
