"""国家政策版本、逐字段 Provenance 与激活事实的 tenant-bound PostgreSQL 仓储。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import cast

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from domains.compliance.models import CountryPolicyActivation, CountryPolicyVersion
from domains.compliance.repository import (
    CountryPolicyActivationRepository,
    CountryPolicyFieldProvenanceRepository,
    CountryPolicyVersionRepository,
)
from domains.compliance.schemas import (
    DECISION_FIELDS,
    CountryPolicyField,
    normalize_country_key,
)
from infra.db.tables import (
    CountryPolicyActivationRow,
    CountryPolicyFieldProvenanceRow,
    CountryPolicyVersionRow,
)
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import (
    ApprovalId,
    CountryPolicyActivationId,
    CountryPolicyVersionId,
    EmployeeId,
    IdempotencyKey,
    TenantId,
)
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

    @staticmethod
    def _require_country_key(country_key: str) -> None:
        if country_key != normalize_country_key(country_key):
            raise ValidationError("country_key 必须已经精确规范化")

    async def _lock_country(
        self, tenant_id: TenantId, country_key: str, action: str
    ) -> None:
        self._require_tenant(tenant_id, action)
        self._require_country_key(country_key)
        await self._session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(:lock_name, 0))"
            ),
            {
                "lock_name": (
                    f"compliance-country-policy:{tenant_id}:{country_key}"
                )
            },
        )


def _string_tuple(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValidationError(f"国家政策持久化字段 {field_name} 无效")
    return tuple(cast(list[str], value))


def _to_provenance(row: CountryPolicyFieldProvenanceRow) -> Provenance:
    return Provenance(
        source_type=SourceType(row.source_type),
        source_id=row.source_id,
        extracted_by=row.extracted_by,
        extracted_at=row.extracted_at,
        confirmed_by=EmployeeId(row.confirmed_by),
        confirmed_at=row.confirmed_at,
        source_url=row.source_url,
        page_hash=row.page_hash,
    )


def _to_version(
    row: CountryPolicyVersionRow,
    provenance: Mapping[CountryPolicyField, Provenance],
) -> CountryPolicyVersion:
    return CountryPolicyVersion(
        tenant_id=TenantId(row.tenant_id),
        country_policy_version_id=CountryPolicyVersionId(
            row.country_policy_version_id
        ),
        country=row.country,
        country_key=row.country_key,
        version_number=row.version_number,
        content_hash=row.content_hash,
        base_version_id=(
            CountryPolicyVersionId(row.base_version_id)
            if row.base_version_id is not None
            else None
        ),
        base_content_hash=row.base_content_hash,
        public_research_allowed=row.public_research_allowed,
        contact_enrichment_allowed=row.contact_enrichment_allowed,
        cold_b2b_email_allowed=row.cold_b2b_email_allowed,
        personal_data_basis_required=row.personal_data_basis_required,
        subject_type_affects_judgment=row.subject_type_affects_judgment,
        contact_type_affects_judgment=row.contact_type_affects_judgment,
        opt_out_deadline_days=row.opt_out_deadline_days,
        local_representative_required=row.local_representative_required,
        requirements=_string_tuple(row.requirements, "requirements"),
        notes=row.notes,
        field_provenance=provenance,
        proposed_by=EmployeeId(row.proposed_by),
        proposed_at=row.proposed_at,
        idempotency_key=IdempotencyKey(row.idempotency_key),
    )


def _to_activation(row: CountryPolicyActivationRow) -> CountryPolicyActivation:
    return CountryPolicyActivation(
        tenant_id=TenantId(row.tenant_id),
        activation_id=CountryPolicyActivationId(
            row.country_policy_activation_id
        ),
        activation_sequence=row.activation_sequence,
        country_key=row.country_key,
        country_policy_version_id=CountryPolicyVersionId(
            row.country_policy_version_id
        ),
        content_hash=row.content_hash,
        approval_id=ApprovalId(row.approval_id),
        change_set_ref=row.change_set_ref,
        approved_by=EmployeeId(row.approved_by),
        approved_at=row.approved_at,
        activated_by=row.activated_by,
        activated_at=row.activated_at,
    )


class CountryPolicyFieldProvenanceRepositoryImpl(
    _TenantBoundRepository, CountryPolicyFieldProvenanceRepository
):
    async def add_for_version(
        self,
        tenant_id: TenantId,
        version_id: CountryPolicyVersionId,
        field_provenance: Mapping[CountryPolicyField, Provenance],
    ) -> None:
        self._require_tenant(tenant_id, "country_policy_provenance.add")
        if set(field_provenance) != DECISION_FIELDS:
            raise ValidationError("国家政策字段 Provenance 不完整")
        for field in sorted(field_provenance, key=lambda item: item.value):
            provenance = field_provenance[field]
            if (
                provenance.source_type
                not in {
                    SourceType.WEB_PAGE,
                    SourceType.UPLOAD,
                    SourceType.EMPLOYEE_INPUT,
                }
                or provenance.confirmed_by is None
                or provenance.confirmed_at is None
                or provenance.extracted_by
                != f"human:{provenance.confirmed_by}"
                or provenance.extracted_at != provenance.confirmed_at
            ):
                raise ValidationError("国家政策字段 Provenance 必须由人工确认")
            self._session.add(
                CountryPolicyFieldProvenanceRow(
                    tenant_id=str(tenant_id),
                    country_policy_version_id=str(version_id),
                    field_name=field.value,
                    source_type=provenance.source_type.value,
                    source_id=provenance.source_id,
                    extracted_by=provenance.extracted_by,
                    extracted_at=provenance.extracted_at,
                    confirmed_by=str(provenance.confirmed_by),
                    confirmed_at=provenance.confirmed_at,
                    source_url=provenance.source_url,
                    page_hash=provenance.page_hash,
                )
            )
        await self._session.flush()

    async def list_for_version(
        self, tenant_id: TenantId, version_id: CountryPolicyVersionId
    ) -> dict[CountryPolicyField, Provenance]:
        self._require_tenant(tenant_id, "country_policy_provenance.list")
        rows = (
            await self._session.scalars(
                select(CountryPolicyFieldProvenanceRow)
                .where(
                    CountryPolicyFieldProvenanceRow.tenant_id == str(tenant_id),
                    CountryPolicyFieldProvenanceRow.country_policy_version_id
                    == str(version_id),
                )
                .order_by(CountryPolicyFieldProvenanceRow.field_name)
            )
        ).all()
        return {CountryPolicyField(row.field_name): _to_provenance(row) for row in rows}


class CountryPolicyVersionRepositoryImpl(
    _TenantBoundRepository, CountryPolicyVersionRepository
):
    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        super().__init__(session, tenant_id)
        self._provenance = CountryPolicyFieldProvenanceRepositoryImpl(
            session, tenant_id
        )

    async def lock_idempotency_key(
        self, tenant_id: TenantId, idempotency_key: IdempotencyKey
    ) -> None:
        self._require_tenant(
            tenant_id, "country_policy_version.lock_idempotency_key"
        )
        await self._session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(:lock_name, 0))"
            ),
            {
                "lock_name": (
                    "compliance-country-policy-idempotency:"
                    f"{tenant_id}:{idempotency_key}"
                )
            },
        )

    async def lock_country(self, tenant_id: TenantId, country_key: str) -> None:
        await self._lock_country(
            tenant_id, country_key, "country_policy_version.lock_country"
        )

    async def add(
        self, tenant_id: TenantId, version: CountryPolicyVersion
    ) -> None:
        self._require_tenant(tenant_id, "country_policy_version.add")
        self._require_tenant(version.tenant_id, "country_policy_version.add_entity")
        if version.tenant_id != tenant_id:
            raise TenantIsolationViolation("跨租户数据隔离违规")
        self._session.add(
            CountryPolicyVersionRow(
                tenant_id=str(tenant_id),
                country_policy_version_id=str(version.country_policy_version_id),
                country=version.country,
                country_key=version.country_key,
                version_number=version.version_number,
                content_hash=version.content_hash,
                base_version_id=(
                    str(version.base_version_id)
                    if version.base_version_id is not None
                    else None
                ),
                base_content_hash=version.base_content_hash,
                public_research_allowed=version.public_research_allowed,
                contact_enrichment_allowed=version.contact_enrichment_allowed,
                cold_b2b_email_allowed=version.cold_b2b_email_allowed,
                personal_data_basis_required=version.personal_data_basis_required,
                subject_type_affects_judgment=version.subject_type_affects_judgment,
                contact_type_affects_judgment=version.contact_type_affects_judgment,
                opt_out_deadline_days=version.opt_out_deadline_days,
                local_representative_required=version.local_representative_required,
                requirements=list(version.requirements),
                notes=version.notes,
                proposed_by=str(version.proposed_by),
                proposed_at=version.proposed_at,
                idempotency_key=str(version.idempotency_key),
            )
        )
        await self._session.flush()

    async def _rehydrate(
        self, row: CountryPolicyVersionRow | None
    ) -> CountryPolicyVersion | None:
        if row is None:
            return None
        provenance = await self._provenance.list_for_version(
            TenantId(row.tenant_id),
            CountryPolicyVersionId(row.country_policy_version_id),
        )
        return _to_version(row, provenance)

    async def get(
        self, tenant_id: TenantId, version_id: CountryPolicyVersionId
    ) -> CountryPolicyVersion | None:
        self._require_tenant(tenant_id, "country_policy_version.get")
        row = await self._session.scalar(
            select(CountryPolicyVersionRow).where(
                CountryPolicyVersionRow.tenant_id == str(tenant_id),
                CountryPolicyVersionRow.country_policy_version_id == str(version_id),
            )
        )
        return await self._rehydrate(row)

    async def find_by_idempotency_key(
        self, tenant_id: TenantId, idempotency_key: IdempotencyKey
    ) -> CountryPolicyVersion | None:
        self._require_tenant(
            tenant_id, "country_policy_version.find_by_idempotency"
        )
        row = await self._session.scalar(
            select(CountryPolicyVersionRow).where(
                CountryPolicyVersionRow.tenant_id == str(tenant_id),
                CountryPolicyVersionRow.idempotency_key == str(idempotency_key),
            )
        )
        return await self._rehydrate(row)

    async def next_version_number(
        self, tenant_id: TenantId, country_key: str
    ) -> int:
        await self._lock_country(
            tenant_id, country_key, "country_policy_version.next_number"
        )
        latest = await self._session.scalar(
            select(func.max(CountryPolicyVersionRow.version_number)).where(
                CountryPolicyVersionRow.tenant_id == str(tenant_id),
                CountryPolicyVersionRow.country_key == country_key,
            )
        )
        return int(latest or 0) + 1

    async def list(
        self, tenant_id: TenantId, country_key: str, limit: int
    ) -> list[CountryPolicyVersion]:
        self._require_tenant(tenant_id, "country_policy_version.list")
        self._require_country_key(country_key)
        rows = (
            await self._session.scalars(
                select(CountryPolicyVersionRow)
                .where(
                    CountryPolicyVersionRow.tenant_id == str(tenant_id),
                    CountryPolicyVersionRow.country_key == country_key,
                )
                .order_by(
                    CountryPolicyVersionRow.version_number.desc(),
                    CountryPolicyVersionRow.country_policy_version_id.desc(),
                )
                .limit(limit)
            )
        ).all()
        result: list[CountryPolicyVersion] = []
        for row in rows:
            version = await self._rehydrate(row)
            if version is not None:
                result.append(version)
        return result


class CountryPolicyActivationRepositoryImpl(
    _TenantBoundRepository, CountryPolicyActivationRepository
):
    async def get_current(
        self, tenant_id: TenantId, country_key: str
    ) -> CountryPolicyActivation | None:
        self._require_tenant(tenant_id, "country_policy_activation.get_current")
        self._require_country_key(country_key)
        row = await self._session.scalar(
            select(CountryPolicyActivationRow)
            .where(
                CountryPolicyActivationRow.tenant_id == str(tenant_id),
                CountryPolicyActivationRow.country_key == country_key,
            )
            .order_by(
                CountryPolicyActivationRow.activation_sequence.desc(),
                CountryPolicyActivationRow.country_policy_activation_id.desc(),
            )
            .limit(1)
        )
        return _to_activation(row) if row is not None else None

    async def list_current(
        self, tenant_id: TenantId, limit: int
    ) -> list[CountryPolicyActivation]:
        self._require_tenant(tenant_id, "country_policy_activation.list_current")
        rows = (
            await self._session.scalars(
                select(CountryPolicyActivationRow)
                .where(CountryPolicyActivationRow.tenant_id == str(tenant_id))
                .distinct(CountryPolicyActivationRow.country_key)
                .order_by(
                    CountryPolicyActivationRow.country_key,
                    CountryPolicyActivationRow.activation_sequence.desc(),
                )
                .limit(limit)
            )
        ).all()
        return [_to_activation(row) for row in rows]

    async def get_by_version(
        self, tenant_id: TenantId, version_id: CountryPolicyVersionId
    ) -> CountryPolicyActivation | None:
        self._require_tenant(tenant_id, "country_policy_activation.get_by_version")
        row = await self._session.scalar(
            select(CountryPolicyActivationRow).where(
                CountryPolicyActivationRow.tenant_id == str(tenant_id),
                CountryPolicyActivationRow.country_policy_version_id
                == str(version_id),
            )
        )
        return _to_activation(row) if row is not None else None

    async def get_by_approval(
        self, tenant_id: TenantId, approval_id: ApprovalId
    ) -> CountryPolicyActivation | None:
        self._require_tenant(tenant_id, "country_policy_activation.get_by_approval")
        row = await self._session.scalar(
            select(CountryPolicyActivationRow).where(
                CountryPolicyActivationRow.tenant_id == str(tenant_id),
                CountryPolicyActivationRow.approval_id == str(approval_id),
            )
        )
        return _to_activation(row) if row is not None else None

    async def next_activation_sequence(
        self, tenant_id: TenantId, country_key: str
    ) -> int:
        await self._lock_country(
            tenant_id, country_key, "country_policy_activation.next_sequence"
        )
        latest = await self._session.scalar(
            select(func.max(CountryPolicyActivationRow.activation_sequence)).where(
                CountryPolicyActivationRow.tenant_id == str(tenant_id),
                CountryPolicyActivationRow.country_key == country_key,
            )
        )
        return int(latest or 0) + 1

    async def add(
        self, tenant_id: TenantId, activation: CountryPolicyActivation
    ) -> None:
        self._require_tenant(tenant_id, "country_policy_activation.add")
        self._require_tenant(
            activation.tenant_id, "country_policy_activation.add_entity"
        )
        if activation.tenant_id != tenant_id:
            raise TenantIsolationViolation("跨租户数据隔离违规")
        self._session.add(
            CountryPolicyActivationRow(
                tenant_id=str(tenant_id),
                country_policy_activation_id=str(activation.activation_id),
                country_key=activation.country_key,
                activation_sequence=activation.activation_sequence,
                country_policy_version_id=str(
                    activation.country_policy_version_id
                ),
                content_hash=activation.content_hash,
                approval_id=str(activation.approval_id),
                change_set_ref=activation.change_set_ref,
                approved_by=str(activation.approved_by),
                approved_at=activation.approved_at,
                activated_by=activation.activated_by,
                activated_at=activation.activated_at,
            )
        )
        await self._session.flush()


__all__ = (
    "CountryPolicyActivationRepositoryImpl",
    "CountryPolicyFieldProvenanceRepositoryImpl",
    "CountryPolicyVersionRepositoryImpl",
)
