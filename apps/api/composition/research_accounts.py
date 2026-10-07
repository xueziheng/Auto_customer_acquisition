"""在应用层关联企业与需求信号，不改变两个领域的依赖方向。"""

from dataclasses import fields
from typing import Protocol

from domains.demand.schemas import DemandSignalView
from domains.prospecting.schemas import ProspectAccountView
from shared.schemas.identifiers import TenantId

from ..research_schemas import ResearchProspectAccountView


class ResearchEvidenceReader(Protocol):
    """按当前租户与本批企业的信号引用读取证据。"""

    async def for_accounts(
        self, tenant_id: TenantId, accounts: list[ProspectAccountView]
    ) -> dict[str, tuple[DemandSignalView, ...]]: ...


async def research_accounts(
    tenant_id: TenantId,
    accounts: list[ProspectAccountView],
    reader: ResearchEvidenceReader | None,
) -> list[ResearchProspectAccountView]:
    """历史企业无关联研究证据时保留空集合，不补写事实。"""
    evidence = (
        await reader.for_accounts(tenant_id, accounts) if reader is not None else {}
    )
    return [
        ResearchProspectAccountView(
            **{
                field.name: getattr(account, field.name)
                for field in fields(ProspectAccountView)
            },
            research_signals=evidence.get(str(account.account_id), ()),
        )
        for account in accounts
    ]
