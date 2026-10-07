"""Company Playbook tenant-bound PostgreSQL 仓储。"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import cast

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from domains.organization.models import CompanyPlaybookVersion, PlaybookActivation
from domains.organization.repository import (
    PlaybookActivationRepository,
    PlaybookVersionRepository,
)
from infra.db.tables import CompanyPlaybookActivationRow, CompanyPlaybookVersionRow
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import (
    ApprovalId,
    EmployeeId,
    IdempotencyKey,
    PlaybookActivationId,
    PlaybookVersionId,
    TenantId,
)
from shared.schemas.money import CurrencyCode, Money
from shared.schemas.provenance import Provenance, SourceType

_tenant_logger = logging.getLogger("security.tenant_isolation")


class _TenantBoundRepository:
    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _require_tenant(self, tenant_id: TenantId, action: str) -> None:
        if tenant_id == self._tenant_id:
            return
        _tenant_logger.critical(
            "检测到跨租户数据隔离违规",
            extra={"action": action, "tenant_id": str(self._tenant_id)},
        )
        raise TenantIsolationViolation("跨租户数据隔离违规")

    async def _lock_playbook(self, tenant_id: TenantId, action: str) -> None:
        self._require_tenant(tenant_id, action)
        await self._session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(:lock_name, 0))"
            ),
            {"lock_name": f"organization-playbook:{tenant_id}"},
        )


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValidationError(f"Playbook 持久化字段 {field_name} 无效")
    return tuple(cast(list[str], value))


def _to_version(row: CompanyPlaybookVersionRow) -> CompanyPlaybookVersion:
    provenance = Provenance(
        source_type=SourceType(row.source_type),
        source_id=row.source_id,
        extracted_by=row.extracted_by,
        extracted_at=row.extracted_at,
    )
    return CompanyPlaybookVersion(
        tenant_id=TenantId(row.tenant_id),
        playbook_version_id=PlaybookVersionId(row.playbook_version_id),
        version_number=row.version_number,
        content_hash=row.content_hash,
        company_type=row.company_type,
        minimum_deal_value=Money(
            Decimal(row.minimum_deal_amount),
            CurrencyCode(row.minimum_deal_currency),
        ),
        proposed_by=EmployeeId(row.proposed_by),
        proposed_at=row.proposed_at,
        idempotency_key=IdempotencyKey(row.idempotency_key),
        content_provenance=provenance,
        base_version_id=(
            PlaybookVersionId(row.base_version_id)
            if row.base_version_id is not None
            else None
        ),
        base_content_hash=row.base_content_hash,
        excluded_categories=_string_tuple(
            row.excluded_categories, "excluded_categories"
        ),
        sourcing_regions=_string_tuple(row.sourcing_regions, "sourcing_regions"),
        excluded_countries=_string_tuple(
            row.excluded_countries, "excluded_countries"
        ),
        monthly_budget_credits=row.monthly_budget_credits,
        approval_requirements=_string_tuple(
            row.approval_requirements, "approval_requirements"
        ),
        supply_capabilities_note=row.supply_capabilities_note,
    )


def _to_activation(row: CompanyPlaybookActivationRow) -> PlaybookActivation:
    return PlaybookActivation(
        tenant_id=TenantId(row.tenant_id),
        activation_id=PlaybookActivationId(row.activation_id),
        playbook_version_id=PlaybookVersionId(row.playbook_version_id),
        content_hash=row.content_hash,
        approval_id=ApprovalId(row.approval_id),
        change_set_ref=row.change_set_ref,
        approved_by=EmployeeId(row.approved_by),
        approved_at=row.approved_at,
        activated_by=row.activated_by,
        activated_at=row.activated_at,
    )


class PlaybookVersionRepositoryImpl(
    _TenantBoundRepository, PlaybookVersionRepository
):
    async def add(self, version: CompanyPlaybookVersion) -> None:
        self._require_tenant(version.tenant_id, "playbook_version.add")
        provenance = version.content_provenance
        self._session.add(
            CompanyPlaybookVersionRow(
                tenant_id=str(version.tenant_id),
                playbook_version_id=str(version.playbook_version_id),
                version_number=version.version_number,
                content_hash=version.content_hash,
                base_version_id=(
                    str(version.base_version_id)
                    if version.base_version_id is not None
                    else None
                ),
                base_content_hash=version.base_content_hash,
                company_type=version.company_type,
                minimum_deal_amount=version.minimum_deal_value.amount,
                minimum_deal_currency=str(version.minimum_deal_value.currency),
                excluded_categories=list(version.excluded_categories),
                sourcing_regions=list(version.sourcing_regions),
                excluded_countries=list(version.excluded_countries),
                monthly_budget_credits=version.monthly_budget_credits,
                approval_requirements=list(version.approval_requirements),
                supply_capabilities_note=version.supply_capabilities_note,
                proposed_by=str(version.proposed_by),
                proposed_at=version.proposed_at,
                idempotency_key=str(version.idempotency_key),
                source_type=provenance.source_type.value,
                source_id=provenance.source_id,
                extracted_by=provenance.extracted_by,
                extracted_at=provenance.extracted_at,
            )
        )
        await self._session.flush()

    async def get(
        self, tenant_id: TenantId, version_id: PlaybookVersionId
    ) -> CompanyPlaybookVersion | None:
        self._require_tenant(tenant_id, "playbook_version.get")
        row = await self._session.scalar(
            select(CompanyPlaybookVersionRow).where(
                CompanyPlaybookVersionRow.tenant_id == str(tenant_id),
                CompanyPlaybookVersionRow.playbook_version_id == str(version_id),
            )
        )
        return _to_version(row) if row is not None else None

    async def find_by_idempotency_key(
        self, tenant_id: TenantId, idempotency_key: IdempotencyKey
    ) -> CompanyPlaybookVersion | None:
        self._require_tenant(tenant_id, "playbook_version.find_by_idempotency")
        row = await self._session.scalar(
            select(CompanyPlaybookVersionRow).where(
                CompanyPlaybookVersionRow.tenant_id == str(tenant_id),
                CompanyPlaybookVersionRow.idempotency_key == str(idempotency_key),
            )
        )
        return _to_version(row) if row is not None else None

    async def next_version_number(self, tenant_id: TenantId) -> int:
        await self._lock_playbook(tenant_id, "playbook_version.next_number")
        latest = await self._session.scalar(
            select(func.max(CompanyPlaybookVersionRow.version_number)).where(
                CompanyPlaybookVersionRow.tenant_id == str(tenant_id)
            )
        )
        return int(latest or 0) + 1

    async def list(
        self, tenant_id: TenantId, limit: int
    ) -> list[CompanyPlaybookVersion]:
        self._require_tenant(tenant_id, "playbook_version.list")
        rows = (
            await self._session.scalars(
                select(CompanyPlaybookVersionRow)
                .where(CompanyPlaybookVersionRow.tenant_id == str(tenant_id))
                .order_by(
                    CompanyPlaybookVersionRow.version_number.desc(),
                    CompanyPlaybookVersionRow.playbook_version_id.desc(),
                )
                .limit(limit)
            )
        ).all()
        return [_to_version(row) for row in rows]


class PlaybookActivationRepositoryImpl(
    _TenantBoundRepository, PlaybookActivationRepository
):
    async def lock_tenant(self, tenant_id: TenantId) -> None:
        await self._lock_playbook(tenant_id, "playbook_activation.lock_tenant")

    async def get_current(self, tenant_id: TenantId) -> PlaybookActivation | None:
        self._require_tenant(tenant_id, "playbook_activation.get_current")
        row = await self._session.scalar(
            select(CompanyPlaybookActivationRow)
            .where(CompanyPlaybookActivationRow.tenant_id == str(tenant_id))
            .order_by(
                CompanyPlaybookActivationRow.activated_at.desc(),
                CompanyPlaybookActivationRow.activation_id.desc(),
            )
            .limit(1)
        )
        return _to_activation(row) if row is not None else None

    async def get_by_version(
        self, tenant_id: TenantId, version_id: PlaybookVersionId
    ) -> PlaybookActivation | None:
        self._require_tenant(tenant_id, "playbook_activation.get_by_version")
        row = await self._session.scalar(
            select(CompanyPlaybookActivationRow).where(
                CompanyPlaybookActivationRow.tenant_id == str(tenant_id),
                CompanyPlaybookActivationRow.playbook_version_id == str(version_id),
            )
        )
        return _to_activation(row) if row is not None else None

    async def get_by_approval(
        self, tenant_id: TenantId, approval_id: ApprovalId
    ) -> PlaybookActivation | None:
        self._require_tenant(tenant_id, "playbook_activation.get_by_approval")
        row = await self._session.scalar(
            select(CompanyPlaybookActivationRow).where(
                CompanyPlaybookActivationRow.tenant_id == str(tenant_id),
                CompanyPlaybookActivationRow.approval_id == str(approval_id),
            )
        )
        return _to_activation(row) if row is not None else None

    async def add(self, activation: PlaybookActivation) -> None:
        self._require_tenant(activation.tenant_id, "playbook_activation.add")
        self._session.add(
            CompanyPlaybookActivationRow(
                tenant_id=str(activation.tenant_id),
                activation_id=str(activation.activation_id),
                playbook_version_id=str(activation.playbook_version_id),
                content_hash=activation.content_hash,
                approval_id=str(activation.approval_id),
                change_set_ref=activation.change_set_ref,
                approved_by=str(activation.approved_by),
                approved_at=activation.approved_at,
                activated_by=activation.activated_by,
                activated_at=activation.activated_at,
            )
        )


__all__ = (
    "PlaybookActivationRepositoryImpl",
    "PlaybookVersionRepositoryImpl",
)
