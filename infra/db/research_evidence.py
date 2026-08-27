"""客户发现的 tenant-bound 跨域只读投影，保留来源与推断的原字段。"""

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.demand.schemas import DemandSignalView, ResearchEvidence
from domains.prospecting.schemas import ProspectAccountView
from shared.errors import PermissionDenied
from shared.schemas.identifiers import TenantId

from .tables import DemandSignalRow


class PostgresResearchEvidenceReader:
    """仅聚合当前返回企业的同租户证据，不靠前端自行匹配其他租户数据。"""

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def for_accounts(
        self,
        tenant_id: TenantId,
        accounts: list[ProspectAccountView],
    ) -> dict[str, tuple[DemandSignalView, ...]]:
        """保留跨线路多份信号；目录/未知来源仍沿用待核验身份。"""
        if any(account.tenant_id != tenant_id for account in accounts):
            raise PermissionDenied("研究企业不属于当前租户")
        if not accounts:
            return {}
        ids = [str(account.account_id) for account in accounts]
        refs = {ref for account in accounts for ref in account.source_signal_refs}
        statement = (
            select(DemandSignalRow)
            .where(
                DemandSignalRow.tenant_id == tenant_id,
                or_(
                    DemandSignalRow.account_id.in_(ids),
                    DemandSignalRow.signal_id.in_(refs),
                ),
                DemandSignalRow.research_evidence.is_not(None),
            )
            .order_by(DemandSignalRow.observed_at, DemandSignalRow.signal_id)
        )
        async with self._factory() as session:
            rows = (await session.execute(statement)).scalars().all()
        result: dict[str, list[DemandSignalView]] = {}
        for row in rows:
            if row.research_evidence is None:
                continue
            signal = DemandSignalView(
                signal_id=row.signal_id,
                signal_type=row.signal_type,
                entity_name=row.entity_name,
                raw_observation=row.raw_observation,
                possible_need=row.possible_need,
                status=row.status,
                observed_at=row.observed_at,
                source_type=row.source_type,
                source_ref=row.source_id,
                source_url=row.source_url,
                page_hash=row.page_hash,
                snapshot_artifact_ref=row.snapshot_artifact_ref,
                research_evidence=ResearchEvidence.model_validate(
                    row.research_evidence
                ),
            )
            for account in accounts:
                if (
                    row.account_id == account.account_id
                    or row.signal_id in account.source_signal_refs
                ):
                    result.setdefault(str(account.account_id), []).append(signal)
        return {key: tuple(signals) for key, signals in result.items()}
