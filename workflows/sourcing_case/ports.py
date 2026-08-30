"""Sourcing Case V2 的跨域只读窄端口。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from domains.sourcing.service import SourcingNeedSnapshot
from shared.schemas.identifiers import OpportunityId, TenantId, ValidatedNeedId


@runtime_checkable
class SourcingNeedReader(Protocol):
    """读取已验证需求的 tenant-bound、带 Provenance 冻结快照。"""

    async def read(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> SourcingNeedSnapshot:
        """不存在、跨租户或关键来源不可核实时必须失败关闭。"""
        ...


@runtime_checkable
class OpportunityLinkReader(Protocol):
    """按已验证需求读取真实、同租户的机会引用。"""

    async def find_for_need(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> OpportunityId | None:
        """没有真实机会时返回 ``None``，不得构造占位 ID。"""
        ...


__all__ = ("OpportunityLinkReader", "SourcingNeedReader")
