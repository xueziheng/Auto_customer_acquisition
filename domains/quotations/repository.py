"""报价域存储接口。

**内部实现，其他域不得导入。**
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from domains.quotations.models import Quote, QuoteState
from shared.schemas.identifiers import OpportunityId, QuoteId, TenantId


@runtime_checkable
class QuoteRepository(Protocol):
    async def add(self, quote: Quote) -> None: ...

    async def get(
        self, tenant_id: TenantId, quote_id: QuoteId
    ) -> Quote | None: ...

    async def update(self, quote: Quote) -> None:
        """更新报价。

        实现要求：终态（ACCEPTED / REJECTED / SUPERSEDED）与已发送
        （SENT）的报价，除状态转换外的字段一律拒绝修改——已发出的
        报价内容改了，审计链就断了。
        """
        ...

    async def next_version(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> int: ...

    async def find_active_for_opportunity(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> Quote | None:
        """查活跃报价（非终态、非 SUPERSEDED）。

        一个机会同时只能有一个活跃报价——客户手里有两份不同的
        有效报价是谈判事故。
        """
        ...

    async def list_versions(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> list[Quote]: ...

    async def list_expired_candidates(
        self, tenant_id: TenantId, now: datetime, limit: int
    ) -> list[Quote]:
        """SENT 且过了 ``valid_until`` 的报价。``scheduler_worker`` 扫描用。"""
        ...

    async def list_by_state(
        self, tenant_id: TenantId, state: QuoteState, limit: int
    ) -> list[Quote]: ...
