"""潜客域服务 —— **本域的公共 API**。（浅域）"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.prospecting.models import ContactPoint, ProspectAccount
from shared.schemas.identifiers import (
    ContactPointId,
    ProspectAccountId,
    TenantId,
)


@runtime_checkable
class EnrichmentProvider(Protocol):
    """联系人补全数据源。Phase 1 一家实现；Phase 2 多家时在上层加
    瀑布编排（按单价升序、命中即停），本 Protocol 不变。

    实现在 ``connectors/contact_enrichment``，经 tool_gateway 调用。
    """

    async def find_contacts(
        self, company_domain: str, role_hints: list[str]
    ) -> list[dict]:
        """按公司域名找业务联系人。返回原始候选（含来源与单价），
        由服务层落库并包上法律依据。"""
        ...


@runtime_checkable
class ProspectingService(Protocol):
    """潜客服务。"""

    async def resolve_account(
        self, tenant_id: TenantId, entity_name: str, website_domain: str | None,
        country: str,
    ) -> ProspectAccountId:
        """企业消歧：返回既有 account 或新建。

        域名优先（域名相同即同一企业），名称相似度只做辅助。
        不消歧的后果：同一公司两个 account、两个员工、两套邮件。
        """
        ...

    async def add_contact_point(
        self, tenant_id: TenantId, contact_point: ContactPoint
    ) -> ContactPointId:
        """录入联系方式。``legal_basis`` 缺失直接拒绝——
        入库即处理，处理必须有依据。"""
        ...

    async def record_verification(
        self,
        tenant_id: TenantId,
        contact_point_id: ContactPointId,
        result: str,
        provider: str,
    ) -> None:
        """落验证结果。VERIFIED 时发布 ``ContactPointVerified``
        （outreach 域的入组前提）。"""
        ...

    async def handle_erasure_request(
        self, tenant_id: TenantId, contact_point_value: str
    ) -> None:
        """数据主体删除请求。

        跨表清理个人数据，但**保留最小化的抑制记录**（哈希化的地址）
        ——否则删除之后系统会再次找到并联系这个人，恰好违背了
        删除请求的本意。
        """
        ...

    async def get_account(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> ProspectAccount: ...
