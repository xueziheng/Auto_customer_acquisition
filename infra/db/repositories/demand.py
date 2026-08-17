"""demand 域仓储实现（规格 2026-08-16 §6）。"""

from __future__ import annotations

import logging
from typing import cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from domains.demand.models import DemandSignal, SignalStatus, SignalType
from domains.demand.repository import DemandSignalRepository
from infra.db.tables import DemandSignalRow
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import (
    DemandSignalId,
    EmployeeId,
    ProspectAccountId,
    TenantId,
)
from shared.schemas.provenance import Provenance, SourceType

_tenant_logger = logging.getLogger("infra.db.repositories.demand")


class _DemandRepository:
    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _tenant_matches(self, tenant_id: TenantId, action: str) -> bool:
        if tenant_id == self._tenant_id:
            return True
        _tenant_logger.critical(
            "检测到跨租户数据隔离违规",
            extra={"action": action, "tenant_id": str(self._tenant_id)},
        )
        return False

    def _require_tenant(self, tenant_id: TenantId, action: str) -> None:
        if not self._tenant_matches(tenant_id, action):
            raise TenantIsolationViolation("跨租户数据隔离违规")


def _signal_to_row(signal: DemandSignal) -> DemandSignalRow:
    return DemandSignalRow(
        tenant_id=str(signal.tenant_id),
        signal_id=str(signal.signal_id),
        signal_type=signal.signal_type.value,
        entity_name=signal.entity_name,
        raw_observation=signal.raw_observation,
        observed_at=signal.observed_at,
        status=signal.status.value,
        possible_need=signal.possible_need,
        account_id=(
            str(signal.account_id) if signal.account_id is not None else None
        ),
        discard_reason=signal.discard_reason,
        source_type=signal.provenance.source_type.value,
        source_id=signal.provenance.source_id,
        extracted_by=signal.provenance.extracted_by,
        extracted_at=signal.provenance.extracted_at,
        confirmed_by=signal.provenance.confirmed_by,
        confirmed_at=signal.provenance.confirmed_at,
        source_url=signal.provenance.source_url,
        page_hash=signal.provenance.page_hash,
    )


def _row_to_signal(row: DemandSignalRow) -> DemandSignal:
    return DemandSignal(
        signal_id=DemandSignalId(row.signal_id),
        tenant_id=TenantId(row.tenant_id),
        signal_type=SignalType(row.signal_type),
        entity_name=row.entity_name,
        raw_observation=row.raw_observation,
        observed_at=row.observed_at,
        status=SignalStatus(row.status),
        possible_need=row.possible_need,
        account_id=(
            ProspectAccountId(row.account_id) if row.account_id is not None else None
        ),
        discard_reason=row.discard_reason,
        provenance=Provenance(
            source_type=SourceType(row.source_type),
            source_id=row.source_id,
            extracted_by=row.extracted_by,
            extracted_at=row.extracted_at,
            confirmed_by=(
                EmployeeId(row.confirmed_by) if row.confirmed_by is not None else None
            ),
            confirmed_at=row.confirmed_at,
            source_url=row.source_url,
            page_hash=row.page_hash,
        ),
    )


class DemandSignalRepositoryImpl(_DemandRepository, DemandSignalRepository):
    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        super().__init__(session, tenant_id)

    async def add(self, signal: DemandSignal) -> bool:
        """来源身份冲突返回 False；True=新插入（ON CONFLICT DO NOTHING）。"""
        self._require_tenant(signal.tenant_id, "demand_signal_add")
        result = await self._session.execute(
            pg_insert(DemandSignalRow)
            .values(
                tenant_id=str(signal.tenant_id),
                signal_id=str(signal.signal_id),
                signal_type=signal.signal_type.value,
                entity_name=signal.entity_name,
                raw_observation=signal.raw_observation,
                observed_at=signal.observed_at,
                status=signal.status.value,
                possible_need=signal.possible_need,
                account_id=(
                    str(signal.account_id) if signal.account_id is not None else None
                ),
                discard_reason=signal.discard_reason,
                source_type=signal.provenance.source_type.value,
                source_id=signal.provenance.source_id,
                extracted_by=signal.provenance.extracted_by,
                extracted_at=signal.provenance.extracted_at,
                confirmed_by=signal.provenance.confirmed_by,
                confirmed_at=signal.provenance.confirmed_at,
                source_url=signal.provenance.source_url,
                page_hash=signal.provenance.page_hash,
            )
            .on_conflict_do_nothing(constraint="uq_demand_signals_source_identity")
        )
        return cast(CursorResult, result).rowcount > 0

    async def get(
        self, tenant_id: TenantId, signal_id: DemandSignalId
    ) -> DemandSignal | None:
        self._require_tenant(tenant_id, "demand_signal_get")
        row = (
            await self._session.execute(
                select(DemandSignalRow).where(
                    DemandSignalRow.tenant_id == str(self._tenant_id),
                    DemandSignalRow.signal_id == str(signal_id),
                )
            )
        ).scalar_one_or_none()
        return _row_to_signal(row) if row is not None else None

    async def find_duplicate(
        self,
        tenant_id: TenantId,
        entity_name: str,
        signal_type: str,
        source_type: str,
        source_id: str,
    ) -> DemandSignal | None:
        self._require_tenant(tenant_id, "demand_signal_find_duplicate")
        row = (
            await self._session.execute(
                select(DemandSignalRow).where(
                    DemandSignalRow.tenant_id == str(self._tenant_id),
                    DemandSignalRow.entity_name == entity_name,
                    DemandSignalRow.signal_type == signal_type,
                    DemandSignalRow.source_type == source_type,
                    DemandSignalRow.source_id == source_id,
                )
            )
        ).scalar_one_or_none()
        return _row_to_signal(row) if row is not None else None

    async def list_unlinked(
        self, tenant_id: TenantId, limit: int
    ) -> list[DemandSignal]:
        """本切片不实现：无假设消费者，不发明排序/limit 语义（规格 §6.1）。"""
        raise NotImplementedError

    async def discard(
        self,
        tenant_id: TenantId,
        signal_id: DemandSignalId,
        reason: str,
    ) -> DemandSignal | None:
        """Task 4 实现；此处为满足 Protocol 的显式桩（同 list_unlinked 先例）。"""
        raise NotImplementedError
