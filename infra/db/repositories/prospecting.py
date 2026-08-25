"""prospecting tenant-bound SQLAlchemy repositories。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from domains.prospecting.models import (
    ContactPoint,
    ContactPointKind,
    ContactType,
    LegalBasisRecord,
    LegalBasisType,
    ProspectAccount,
    ProspectContact,
    SubjectType,
    VerificationStatus,
)
from domains.prospecting.repository import AccountRepository, ContactRepository
from infra.db.tables import (
    ContactLegalBasisRow,
    ContactPointRow,
    ProspectAccountRow,
    ProspectContactRow,
    ProspectingErasureSuppressionRow,
)
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import (
    ContactPointId,
    EmployeeId,
    ProspectAccountId,
    ProspectContactId,
    TenantId,
)
from shared.schemas.provenance import Provenance, SourceType

_logger = logging.getLogger("infra.db.repositories.prospecting")


def _provenance_to_json(value: Provenance) -> dict[str, object]:
    return {
        "source_type": value.source_type.value,
        "source_id": value.source_id,
        "extracted_by": value.extracted_by,
        "extracted_at": value.extracted_at.isoformat(),
        "confirmed_by": str(value.confirmed_by) if value.confirmed_by else None,
        "confirmed_at": value.confirmed_at.isoformat() if value.confirmed_at else None,
        "source_url": value.source_url,
        "page_hash": value.page_hash,
        "source_quote": value.source_quote,
    }


def _provenance_from_json(value: object) -> Provenance:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValidationError("潜在企业关键字段来源损坏")
    payload = cast(dict[str, object], value)
    confirmed_by = payload.get("confirmed_by")
    confirmed_at = payload.get("confirmed_at")
    return Provenance(
        source_type=SourceType(str(payload["source_type"])),
        source_id=str(payload["source_id"]),
        extracted_by=str(payload["extracted_by"]),
        extracted_at=datetime.fromisoformat(str(payload["extracted_at"])),
        confirmed_by=EmployeeId(str(confirmed_by)) if confirmed_by else None,
        confirmed_at=(
            datetime.fromisoformat(str(confirmed_at)) if confirmed_at else None
        ),
        source_url=(str(payload["source_url"]) if payload.get("source_url") else None),
        page_hash=(str(payload["page_hash"]) if payload.get("page_hash") else None),
        source_quote=(
            str(payload["source_quote"]) if payload.get("source_quote") else None
        ),
    )


class _TenantBound:
    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _require_tenant(self, tenant_id: TenantId, action: str) -> None:
        if tenant_id == self._tenant_id:
            return
        _logger.critical(
            "检测到跨租户数据隔离违规",
            extra={"action": action, "tenant_id": str(self._tenant_id)},
        )
        raise TenantIsolationViolation("跨租户数据隔离违规")


def _row_to_account(row: ProspectAccountRow) -> ProspectAccount:
    return ProspectAccount(
        account_id=ProspectAccountId(row.account_id),
        tenant_id=TenantId(row.tenant_id),
        name=row.name,
        country=row.country,
        website_domain=row.website_domain,
        entity_type=row.entity_type,
        industry=row.industry,
        size_hint=row.size_hint,
        source_signal_refs=list(row.source_signal_refs),
        field_provenance={
            key: _provenance_from_json(value)
            for key, value in row.field_provenance.items()
        },
        created_at=row.created_at,
    )


def _row_to_contact(row: ProspectContactRow) -> ProspectContact:
    return ProspectContact(
        contact_id=ProspectContactId(row.contact_id),
        tenant_id=TenantId(row.tenant_id),
        account_id=ProspectAccountId(row.account_id),
        full_name=row.full_name,
        role_title=row.role_title,
        language=row.language,
        created_at=row.created_at,
    )


def _rows_to_point(row: ContactPointRow, basis: ContactLegalBasisRow) -> ContactPoint:
    return ContactPoint(
        contact_point_id=ContactPointId(row.contact_point_id),
        tenant_id=TenantId(row.tenant_id),
        contact_id=ProspectContactId(row.contact_id),
        kind=ContactPointKind(row.kind),
        value=row.value,
        value_hash=row.value_hash,
        legal_basis=LegalBasisRecord(
            basis=LegalBasisType(basis.basis),
            subject_type=SubjectType(basis.subject_type),
            contact_type=ContactType(basis.contact_type),
            source=basis.source,
            source_url=basis.source_url,
            collected_at=basis.collected_at,
            assessment_ref=basis.assessment_ref,
        ),
        created_at=row.created_at,
        verification=VerificationStatus(row.verification_status),
        verified_at=row.verified_at,
        verification_provider=row.verification_provider,
        verification_checked_at=row.verification_checked_at,
        verification_cost_note=row.verification_cost_note,
        enrichment_cost_note=row.enrichment_cost_note,
    )


class ProspectAccountRepositoryImpl(_TenantBound, AccountRepository):
    async def add(self, account: ProspectAccount) -> bool:
        self._require_tenant(account.tenant_id, "prospect_account_add")
        statement = pg_insert(ProspectAccountRow).values(
            tenant_id=str(account.tenant_id),
            account_id=str(account.account_id),
            name=account.name,
            country=account.country,
            website_domain=account.website_domain,
            entity_type=account.entity_type,
            industry=account.industry,
            size_hint=account.size_hint,
            source_signal_refs=list(account.source_signal_refs),
            field_provenance={
                key: _provenance_to_json(value)
                for key, value in account.field_provenance.items()
            },
            created_at=account.created_at,
        )
        if account.website_domain is None:
            statement = statement.on_conflict_do_nothing()
        else:
            statement = statement.on_conflict_do_nothing(
                index_elements=["tenant_id", "website_domain"],
                index_where=ProspectAccountRow.website_domain.is_not(None),
            )
        result = await self._session.execute(statement)
        return cast(CursorResult, result).rowcount > 0

    async def get(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> ProspectAccount | None:
        self._require_tenant(tenant_id, "prospect_account_get")
        row = (
            await self._session.execute(
                select(ProspectAccountRow).where(
                    ProspectAccountRow.tenant_id == str(self._tenant_id),
                    ProspectAccountRow.account_id == str(account_id),
                )
            )
        ).scalar_one_or_none()
        return _row_to_account(row) if row is not None else None

    async def find_by_domain(
        self, tenant_id: TenantId, website_domain: str
    ) -> ProspectAccount | None:
        self._require_tenant(tenant_id, "prospect_account_find_domain")
        row = (
            await self._session.execute(
                select(ProspectAccountRow).where(
                    ProspectAccountRow.tenant_id == str(self._tenant_id),
                    ProspectAccountRow.website_domain == website_domain,
                )
            )
        ).scalar_one_or_none()
        return _row_to_account(row) if row is not None else None

    async def list_accounts(
        self, tenant_id: TenantId, *, limit: int
    ) -> list[ProspectAccount]:
        self._require_tenant(tenant_id, "prospect_account_list")
        rows = (
            await self._session.execute(
                select(ProspectAccountRow)
                .where(ProspectAccountRow.tenant_id == str(self._tenant_id))
                .order_by(
                    ProspectAccountRow.created_at.desc(),
                    ProspectAccountRow.account_id.desc(),
                )
                .limit(limit)
            )
        ).scalars()
        return [_row_to_account(row) for row in rows]

    async def search_by_name(
        self, tenant_id: TenantId, name: str, country: str
    ) -> list[ProspectAccount]:
        self._require_tenant(tenant_id, "prospect_account_search_name")
        escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = (
            await self._session.execute(
                select(ProspectAccountRow)
                .where(
                    ProspectAccountRow.tenant_id == str(self._tenant_id),
                    ProspectAccountRow.country == country,
                    ProspectAccountRow.name.ilike(f"%{escaped}%", escape="\\"),
                )
                .order_by(ProspectAccountRow.created_at, ProspectAccountRow.account_id)
            )
        ).scalars()
        return [_row_to_account(row) for row in rows]

    async def merge_source_signal_refs(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
        source_signal_refs: tuple[str, ...],
        field_provenance: dict[str, Provenance],
    ) -> ProspectAccount | None:
        self._require_tenant(tenant_id, "prospect_account_merge_source_refs")
        row = (
            await self._session.execute(
                select(ProspectAccountRow)
                .where(
                    ProspectAccountRow.tenant_id == str(self._tenant_id),
                    ProspectAccountRow.account_id == str(account_id),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        row.source_signal_refs = list(
            dict.fromkeys([*row.source_signal_refs, *source_signal_refs])
        )
        stored = dict(row.field_provenance)
        for key, value in field_provenance.items():
            stored.setdefault(key, _provenance_to_json(value))
        row.field_provenance = stored
        await self._session.flush()
        return _row_to_account(row)


class ProspectContactRepositoryImpl(_TenantBound, ContactRepository):
    def __init__(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(session, tenant_id)
        self._now = now or (lambda: datetime.now(UTC))

    async def add_contact(self, contact: ProspectContact) -> None:
        self._require_tenant(contact.tenant_id, "prospect_contact_add")
        self._session.add(
            ProspectContactRow(
                tenant_id=str(contact.tenant_id),
                contact_id=str(contact.contact_id),
                account_id=str(contact.account_id),
                full_name=contact.full_name,
                role_title=contact.role_title,
                language=contact.language,
                created_at=contact.created_at,
            )
        )
        await self._session.flush()

    async def get_contact(
        self, tenant_id: TenantId, contact_id: ProspectContactId
    ) -> ProspectContact | None:
        self._require_tenant(tenant_id, "prospect_contact_get")
        row = (
            await self._session.execute(
                select(ProspectContactRow).where(
                    ProspectContactRow.tenant_id == str(self._tenant_id),
                    ProspectContactRow.contact_id == str(contact_id),
                )
            )
        ).scalar_one_or_none()
        return _row_to_contact(row) if row is not None else None

    async def list_for_account(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> list[ProspectContact]:
        self._require_tenant(tenant_id, "prospect_contact_list_account")
        rows = (
            await self._session.execute(
                select(ProspectContactRow)
                .where(
                    ProspectContactRow.tenant_id == str(self._tenant_id),
                    ProspectContactRow.account_id == str(account_id),
                )
                .order_by(
                    ProspectContactRow.created_at.desc(),
                    ProspectContactRow.contact_id.desc(),
                )
            )
        ).scalars()
        return [_row_to_contact(row) for row in rows]

    async def add_contact_point(self, cp: ContactPoint) -> bool:
        self._require_tenant(cp.tenant_id, "contact_point_add")
        result = await self._session.execute(
            pg_insert(ContactPointRow)
            .values(
                tenant_id=str(cp.tenant_id),
                contact_point_id=str(cp.contact_point_id),
                contact_id=str(cp.contact_id),
                kind=cp.kind.value,
                value=cp.value,
                value_hash=cp.value_hash,
                verification_status=cp.verification.value,
                verified_at=cp.verified_at,
                verification_provider=cp.verification_provider,
                verification_checked_at=cp.verification_checked_at,
                verification_cost_note=cp.verification_cost_note,
                enrichment_cost_note=cp.enrichment_cost_note,
                created_at=cp.created_at,
            )
            .on_conflict_do_nothing(constraint="uq_contact_points_value_hash")
        )
        if cast(CursorResult, result).rowcount == 0:
            return False
        basis = cp.legal_basis
        self._session.add(
            ContactLegalBasisRow(
                tenant_id=str(cp.tenant_id),
                contact_point_id=str(cp.contact_point_id),
                basis=basis.basis.value,
                subject_type=basis.subject_type.value,
                contact_type=basis.contact_type.value,
                source=basis.source,
                source_url=basis.source_url,
                collected_at=basis.collected_at,
                assessment_ref=basis.assessment_ref,
            )
        )
        await self._session.flush()
        return True

    async def _get_point(
        self, contact_point_id: ContactPointId, *, for_update: bool
    ) -> ContactPoint | None:
        statement = (
            select(ContactPointRow, ContactLegalBasisRow)
            .join(
                ContactLegalBasisRow,
                (ContactLegalBasisRow.tenant_id == ContactPointRow.tenant_id)
                & (
                    ContactLegalBasisRow.contact_point_id
                    == ContactPointRow.contact_point_id
                ),
            )
            .where(
                ContactPointRow.tenant_id == str(self._tenant_id),
                ContactPointRow.contact_point_id == str(contact_point_id),
            )
        )
        if for_update:
            statement = statement.with_for_update(of=ContactPointRow)
        pair = (await self._session.execute(statement)).one_or_none()
        return _rows_to_point(*pair) if pair is not None else None

    async def get_contact_point(
        self, tenant_id: TenantId, contact_point_id: ContactPointId
    ) -> ContactPoint | None:
        self._require_tenant(tenant_id, "contact_point_get")
        return await self._get_point(contact_point_id, for_update=False)

    async def list_for_contact(
        self, tenant_id: TenantId, contact_id: ProspectContactId
    ) -> list[ContactPoint]:
        self._require_tenant(tenant_id, "contact_point_list_contact")
        rows = await self._session.execute(
            select(ContactPointRow, ContactLegalBasisRow)
            .join(
                ContactLegalBasisRow,
                (ContactLegalBasisRow.tenant_id == ContactPointRow.tenant_id)
                & (
                    ContactLegalBasisRow.contact_point_id
                    == ContactPointRow.contact_point_id
                ),
            )
            .where(
                ContactPointRow.tenant_id == str(self._tenant_id),
                ContactPointRow.contact_id == str(contact_id),
            )
            .order_by(
                ContactPointRow.created_at.desc(),
                ContactPointRow.contact_point_id.desc(),
            )
        )
        return [_rows_to_point(point, basis) for point, basis in rows]

    async def get_contact_point_for_update(
        self, tenant_id: TenantId, contact_point_id: ContactPointId
    ) -> ContactPoint | None:
        self._require_tenant(tenant_id, "contact_point_get_for_update")
        return await self._get_point(contact_point_id, for_update=True)

    async def update_contact_point(self, cp: ContactPoint) -> None:
        self._require_tenant(cp.tenant_id, "contact_point_update")
        row = (
            await self._session.execute(
                select(ContactPointRow)
                .where(
                    ContactPointRow.tenant_id == str(self._tenant_id),
                    ContactPointRow.contact_point_id == str(cp.contact_point_id),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if row is None:
            raise ValidationError("联系方式不存在")
        row.verification_status = cp.verification.value
        row.verified_at = cp.verified_at
        row.verification_provider = cp.verification_provider
        row.verification_checked_at = cp.verification_checked_at
        row.verification_cost_note = cp.verification_cost_note
        await self._session.flush()

    async def find_by_value_hash(
        self, tenant_id: TenantId, kind: ContactPointKind, value_hash: str
    ) -> ContactPoint | None:
        self._require_tenant(tenant_id, "contact_point_find_hash")
        pair = (
            await self._session.execute(
                select(ContactPointRow, ContactLegalBasisRow)
                .join(
                    ContactLegalBasisRow,
                    (ContactLegalBasisRow.tenant_id == ContactPointRow.tenant_id)
                    & (
                        ContactLegalBasisRow.contact_point_id
                        == ContactPointRow.contact_point_id
                    ),
                )
                .where(
                    ContactPointRow.tenant_id == str(self._tenant_id),
                    ContactPointRow.kind == kind.value,
                    ContactPointRow.value_hash == value_hash,
                )
            )
        ).one_or_none()
        return _rows_to_point(*pair) if pair is not None else None

    async def list_verified_for_account(
        self, tenant_id: TenantId, account_id: ProspectAccountId
    ) -> list[ContactPoint]:
        self._require_tenant(tenant_id, "contact_point_list_verified")
        rows = await self._session.execute(
            select(ContactPointRow, ContactLegalBasisRow)
            .join(
                ProspectContactRow,
                (ProspectContactRow.tenant_id == ContactPointRow.tenant_id)
                & (ProspectContactRow.contact_id == ContactPointRow.contact_id),
            )
            .join(
                ContactLegalBasisRow,
                (ContactLegalBasisRow.tenant_id == ContactPointRow.tenant_id)
                & (
                    ContactLegalBasisRow.contact_point_id
                    == ContactPointRow.contact_point_id
                ),
            )
            .where(
                ContactPointRow.tenant_id == str(self._tenant_id),
                ProspectContactRow.account_id == str(account_id),
                ContactPointRow.verification_status
                == VerificationStatus.VERIFIED.value,
            )
            .order_by(ContactPointRow.created_at, ContactPointRow.contact_point_id)
        )
        return [_rows_to_point(point, basis) for point, basis in rows]

    async def erase_personal_data(
        self, tenant_id: TenantId, contact_point_value_hash: str
    ) -> int:
        self._require_tenant(tenant_id, "contact_point_erase")
        erased_at = self._now()
        if erased_at.tzinfo is None or erased_at.utcoffset() != timedelta(0):
            raise ValidationError("服务时钟必须为 UTC")
        await self._session.execute(
            pg_insert(ProspectingErasureSuppressionRow)
            .values(
                tenant_id=str(self._tenant_id),
                value_hash=contact_point_value_hash,
                erased_at=erased_at,
            )
            .on_conflict_do_nothing()
        )
        pairs = (
            await self._session.execute(
                select(ContactPointRow.contact_point_id, ContactPointRow.contact_id)
                .where(
                    ContactPointRow.tenant_id == str(self._tenant_id),
                    ContactPointRow.value_hash == contact_point_value_hash,
                )
                .with_for_update()
            )
        ).all()
        if not pairs:
            return 0
        contact_ids = {str(pair.contact_id) for pair in pairs}
        await self._session.execute(
            delete(ContactPointRow).where(
                ContactPointRow.tenant_id == str(self._tenant_id),
                ContactPointRow.value_hash == contact_point_value_hash,
            )
        )
        await self._session.flush()
        for contact_id in sorted(contact_ids):
            remaining = await self._session.scalar(
                select(func.count())
                .select_from(ContactPointRow)
                .where(
                    ContactPointRow.tenant_id == str(self._tenant_id),
                    ContactPointRow.contact_id == contact_id,
                )
            )
            if remaining == 0:
                await self._session.execute(
                    delete(ProspectContactRow).where(
                        ProspectContactRow.tenant_id == str(self._tenant_id),
                        ProspectContactRow.contact_id == contact_id,
                    )
                )
        await self._session.flush()
        return len(pairs)

    async def is_erasure_suppressed(
        self, tenant_id: TenantId, contact_point_value_hash: str
    ) -> bool:
        self._require_tenant(tenant_id, "contact_point_erasure_check")
        value = await self._session.scalar(
            select(ProspectingErasureSuppressionRow.value_hash).where(
                ProspectingErasureSuppressionRow.tenant_id == str(self._tenant_id),
                ProspectingErasureSuppressionRow.value_hash == contact_point_value_hash,
            )
        )
        return value is not None
