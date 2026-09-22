"""邮件技术读取与归档的窄接口，shared不依赖Provider或业务域。"""

from typing import Protocol

from shared.schemas.email_inbound import ArchivedInboundPage, ArchivedInboundRaw
from shared.schemas.identifiers import TenantId


class InboundRawArchiver(Protocol):
    async def archive(
        self, tenant_id: TenantId, raw_mime: bytes, *, maximum_bytes: int
    ) -> ArchivedInboundRaw:
        """Gateway调用栈内原样归档再有界读回，失败不得交付候选。"""
        ...


class InboundContentGuard(Protocol):
    def check(self, *, subject: str | None, body: str) -> None:
        """检查完整候选，命中凭证标记抛固定ValidationError。"""
        ...


class ToolEmailInboundReader(Protocol):
    async def fetch(
        self, tenant_id: TenantId, mailbox_alias: str, cursor: str, page_limit: int
    ) -> ArchivedInboundPage:
        """仅SUCCEEDED交付页；5b整页事务持久化后才能使用next_cursor。"""
        ...
