"""组织域服务 —— **本域的公共 API**。（浅域）"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.organization.models import CompanyPlaybook
from shared.schemas.identifiers import TenantId


@runtime_checkable
class OrganizationService(Protocol):
    """组织服务。"""

    async def get_playbook(self, tenant_id: TenantId) -> CompanyPlaybook:
        """读 Playbook。

        **没有配置时抛错，不返回默认 Playbook**——排除品类、金额底线
        是老板的商业决策，代码不能替他决定一个"合理默认"。
        系统初始化流程必须强制填写 Playbook 后才能启用探索。
        """
        ...

    async def update_playbook(
        self, tenant_id: TenantId, playbook: CompanyPlaybook, updated_by: str
    ) -> None:
        """更新 Playbook。走审批（配置变更类型）；历史版本保留。"""
        ...
