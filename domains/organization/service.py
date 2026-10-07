"""组织域服务公共 API；不暴露直接更新或强制激活旁路。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.organization.models import CompanyPlaybook
from domains.organization.permissions import OrganizationActor
from domains.organization.schemas import (
    PlaybookActivationView,
    PlaybookApprovalFact,
    PlaybookChangeSnapshot,
    PlaybookProposalCreate,
    PlaybookProposalResult,
    PlaybookVersionView,
)
from shared.schemas.identifiers import IdempotencyKey, PlaybookVersionId, TenantId


@runtime_checkable
class OrganizationService(Protocol):
    """版本化 Playbook 的安全读、提交与系统激活契约。"""

    async def get_playbook(
        self, tenant_id: TenantId, *, actor: OrganizationActor
    ) -> CompanyPlaybook:
        """读取当前已激活 Playbook；未配置时抛错，不生成默认值。"""
        ...

    async def get_version(
        self,
        tenant_id: TenantId,
        version_id: PlaybookVersionId,
        *,
        actor: OrganizationActor,
    ) -> PlaybookVersionView: ...

    async def list_versions(
        self,
        tenant_id: TenantId,
        *,
        actor: OrganizationActor,
        limit: int = 50,
    ) -> list[PlaybookVersionView]: ...

    async def get_change_snapshot(
        self,
        tenant_id: TenantId,
        version_id: PlaybookVersionId,
        *,
        actor: OrganizationActor,
    ) -> PlaybookChangeSnapshot:
        """只供系统工作流临时读取 base/current/candidate，不持久化全文。"""
        ...

    async def propose_playbook(
        self,
        tenant_id: TenantId,
        command: PlaybookProposalCreate,
        *,
        actor: OrganizationActor,
        idempotency_key: IdempotencyKey,
    ) -> PlaybookProposalResult: ...

    async def activate_playbook(
        self,
        tenant_id: TenantId,
        version_id: PlaybookVersionId,
        approval: PlaybookApprovalFact,
        *,
        actor: OrganizationActor,
    ) -> PlaybookActivationView:
        """仅接受工作流从已批准记录构造的窄事实，不能由 API 直接调用。"""
        ...


__all__ = ("OrganizationService",)
