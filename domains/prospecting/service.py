"""潜客域服务 —— **本域的公共 API**。（浅域）"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.prospecting.repository import (
    ProspectingUnitOfWork as _ProspectingUnitOfWork,
)
from domains.prospecting.schemas import (
    AccountResolveRequest,
    ContactCreateRequest,
    ContactPointCreateRequest,
    ContactPointView,
    DiscoveredContactRequest,
    DiscoveredContactResult,
    ProspectAccountDetailView,
    ProspectAccountView,
    ProspectContactDetailView,
    VerificationRecordRequest,
)
from shared.schemas.identifiers import (
    ContactPointId,
    ProspectAccountId,
    ProspectContactId,
    TenantId,
)

ProspectingUnitOfWork = _ProspectingUnitOfWork


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
class ContactValueHasher(Protocol):
    """对 canonical 联系方式做稳定 HMAC 指纹；实现由组合根注入。"""

    def fingerprint(self, canonical_value: str) -> str:
        """返回 64 位小写十六进制；不得记录输入或密钥。"""
        ...


@runtime_checkable
class ProspectingService(Protocol):
    """潜客服务。"""

    async def resolve_account(
        self, tenant_id: TenantId, request: AccountResolveRequest
    ) -> ProspectAccountId:
        """企业消歧：返回既有 account 或新建。

        域名优先（域名相同即同一企业），名称相似度只做辅助。
        不消歧的后果：同一公司两个 account、两个员工、两套邮件。
        """
        ...

    async def create_contact(
        self, tenant_id: TenantId, request: ContactCreateRequest
    ) -> ProspectContactId:
        """在当前租户的既有企业下创建联系人。"""
        ...

    async def add_contact_point(
        self, tenant_id: TenantId, request: ContactPointCreateRequest
    ) -> ContactPointId:
        """录入联系方式。``legal_basis`` 缺失直接拒绝——
        入库即处理，处理必须有依据。"""
        ...

    async def record_discovered_contact(
        self, tenant_id: TenantId, request: DiscoveredContactRequest
    ) -> DiscoveredContactResult:
        """在一个事务中幂等录入 Provider 发现的联系人和联系方式。

        联系方式指纹已存在且属于同一企业时复用首次记录；属于其他企业时
        固定冲突，禁止模型或 workflow 猜测合并。并发竞争必须回滚本次新联系人，
        由 scheduler 安全重试后读取胜者。
        """
        ...

    async def record_verification(
        self,
        tenant_id: TenantId,
        request: VerificationRecordRequest,
    ) -> None:
        """落带时间与成本说明的验证观察。

        只有首次进入 VERIFIED 才发布 ``ContactPointVerified``；旧结果与
        同时间冲突必须拒绝，完整相同请求幂等返回。
        """
        ...

    async def get_contact_point(
        self, tenant_id: TenantId, contact_point_id: ContactPointId
    ) -> ContactPointView:
        """按租户读取联系方式；跨租户与不存在使用同一 NotFound。"""
        ...

    async def handle_erasure_request(
        self, tenant_id: TenantId, contact_point_value: str
    ) -> int:
        """数据主体删除请求。

        跨表清理个人数据，但**保留最小化的抑制记录**（哈希化的地址）
        ——否则删除之后系统会再次找到并联系这个人，恰好违背了
        删除请求的本意。
        """
        ...

    async def get_account(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> ProspectAccountView: ...

    async def list_accounts(
        self, tenant_id: TenantId, *, limit: int = 50
    ) -> list[ProspectAccountView]:
        """列出租户内潜在企业；limit 固定限制在 1..200。"""
        ...

    async def get_account_detail(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> ProspectAccountDetailView:
        """读取企业、联系人、联系方式与法律依据的同租户详情。"""
        ...

    async def list_contacts_for_account(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> list[ProspectContactDetailView]:
        """读取企业下联系人；企业不存在与跨租户使用同一 NotFound。"""
        ...

    async def list_verified_contact_points(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> list[ContactPointView]:
        """只返回当前租户、指定企业下 VERIFIED 的联系方式。"""
        ...
