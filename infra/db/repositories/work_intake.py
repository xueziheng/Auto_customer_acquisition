"""员工工作上传版本链的 tenant-bound PostgreSQL 仓储。"""

from __future__ import annotations

import json
import logging
from typing import cast

from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.tables import (
    EmployeeConfirmationRow,
    ExtractedFactRow,
    WorkUploadRow,
)
from shared.errors import InvalidStateTransition, TenantIsolationViolation
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeConfirmationId,
    EmployeeId,
    OpportunityId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
    WorkExtractionId,
    WorkUploadId,
)
from workflows.employee_work_intake.repository import WorkIntakeRepository
from workflows.employee_work_intake.schemas import (
    EmployeeConfirmationView,
    ExtractionPayload,
    WorkExtractionView,
    WorkSourceKind,
    WorkUploadStatus,
    WorkUploadView,
)

_tenant_logger = logging.getLogger("security.tenant_isolation")


def _payload(value: dict[str, object]) -> ExtractionPayload:
    return ExtractionPayload.model_validate_json(json.dumps(value))


def _upload_view(row: WorkUploadRow) -> WorkUploadView:
    return WorkUploadView(
        upload_id=WorkUploadId(row.upload_id),
        tenant_id=TenantId(row.tenant_id),
        artifact_id=ArtifactId(row.artifact_id),
        employee_id=EmployeeId(row.employee_id),
        source_kind=WorkSourceKind(row.source_kind),
        status=WorkUploadStatus(row.status),
        occurred_at=row.occurred_at,
        customer_timezone=row.customer_timezone,
        created_at=row.created_at,
        account_id=ProspectAccountId(row.account_id) if row.account_id else None,
        opportunity_id=(
            OpportunityId(row.opportunity_id) if row.opportunity_id else None
        ),
        need_id=ValidatedNeedId(row.need_id) if row.need_id else None,
    )


def _confirmation_view(row: EmployeeConfirmationRow) -> EmployeeConfirmationView:
    return EmployeeConfirmationView(
        confirmation_id=EmployeeConfirmationId(row.confirmation_id),
        revision=row.revision,
        payload=_payload(row.payload),
        confirmed_by=EmployeeId(row.confirmed_by),
        confirmed_at=row.confirmed_at,
    )


def _extraction_view(
    row: ExtractedFactRow,
    confirmation: EmployeeConfirmationRow | None,
) -> WorkExtractionView:
    return WorkExtractionView(
        extraction_id=WorkExtractionId(row.extraction_id),
        upload_id=WorkUploadId(row.upload_id),
        payload=_payload(row.payload),
        extracted_by=row.extracted_by,
        created_at=row.created_at,
        confirmation=(
            _confirmation_view(confirmation) if confirmation is not None else None
        ),
    )


class WorkIntakeRepositoryImpl(WorkIntakeRepository):
    """所有查询绑定构造时的租户，提取和确认只提供追加操作。"""

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

    async def add_upload(self, upload: WorkUploadView) -> None:
        self._require_tenant(upload.tenant_id, "work_intake_upload_add")
        self._session.add(
            WorkUploadRow(
                tenant_id=str(upload.tenant_id),
                upload_id=str(upload.upload_id),
                artifact_id=str(upload.artifact_id),
                employee_id=str(upload.employee_id),
                source_kind=upload.source_kind.value,
                status=upload.status.value,
                occurred_at=upload.occurred_at,
                customer_timezone=upload.customer_timezone,
                account_id=str(upload.account_id) if upload.account_id else None,
                opportunity_id=(
                    str(upload.opportunity_id) if upload.opportunity_id else None
                ),
                need_id=str(upload.need_id) if upload.need_id else None,
                created_at=upload.created_at,
            )
        )
        await self._session.flush()

    async def get_upload(
        self, tenant_id: TenantId, upload_id: WorkUploadId
    ) -> WorkUploadView | None:
        self._require_tenant(tenant_id, "work_intake_upload_get")
        row = (
            await self._session.execute(
                select(WorkUploadRow).where(
                    WorkUploadRow.tenant_id == str(self._tenant_id),
                    WorkUploadRow.upload_id == str(upload_id),
                )
            )
        ).scalar_one_or_none()
        return _upload_view(row) if row is not None else None

    async def update_upload(self, upload: WorkUploadView) -> None:
        self._require_tenant(upload.tenant_id, "work_intake_upload_update")
        result = await self._session.execute(
            sa_update(WorkUploadRow)
            .where(
                WorkUploadRow.tenant_id == str(self._tenant_id),
                WorkUploadRow.upload_id == str(upload.upload_id),
            )
            .values(
                status=upload.status.value,
                account_id=str(upload.account_id) if upload.account_id else None,
                opportunity_id=(
                    str(upload.opportunity_id) if upload.opportunity_id else None
                ),
                need_id=str(upload.need_id) if upload.need_id else None,
            )
        )
        if cast(CursorResult[object], result).rowcount != 1:
            raise InvalidStateTransition("员工工作上传状态已变化")

    async def add_extraction(
        self, tenant_id: TenantId, extraction: WorkExtractionView
    ) -> None:
        self._require_tenant(tenant_id, "work_intake_extraction_add")
        if extraction.confirmation is not None:
            raise InvalidStateTransition("原始提取不得携带人工确认版本")
        self._session.add(
            ExtractedFactRow(
                tenant_id=str(self._tenant_id),
                extraction_id=str(extraction.extraction_id),
                upload_id=str(extraction.upload_id),
                payload=extraction.payload.model_dump(mode="json"),
                extracted_by=extraction.extracted_by,
                created_at=extraction.created_at,
            )
        )
        await self._session.flush()

    async def get_latest_extraction(
        self, tenant_id: TenantId, upload_id: WorkUploadId
    ) -> WorkExtractionView | None:
        self._require_tenant(tenant_id, "work_intake_extraction_get")
        result = await self._session.execute(
            select(ExtractedFactRow, EmployeeConfirmationRow)
            .outerjoin(
                EmployeeConfirmationRow,
                (EmployeeConfirmationRow.tenant_id == ExtractedFactRow.tenant_id)
                & (
                    EmployeeConfirmationRow.extraction_id
                    == ExtractedFactRow.extraction_id
                ),
            )
            .where(
                ExtractedFactRow.tenant_id == str(self._tenant_id),
                ExtractedFactRow.upload_id == str(upload_id),
            )
            .order_by(
                ExtractedFactRow.created_at.desc(),
                ExtractedFactRow.extraction_id.desc(),
            )
            .limit(1)
        )
        pair = result.one_or_none()
        if pair is None:
            return None
        extraction, confirmation = pair
        return _extraction_view(extraction, confirmation)

    async def append_confirmation(
        self,
        tenant_id: TenantId,
        extraction_id: WorkExtractionId,
        confirmation: EmployeeConfirmationView,
    ) -> None:
        self._require_tenant(tenant_id, "work_intake_confirmation_add")
        self._session.add(
            EmployeeConfirmationRow(
                tenant_id=str(self._tenant_id),
                confirmation_id=str(confirmation.confirmation_id),
                extraction_id=str(extraction_id),
                revision=confirmation.revision,
                payload=confirmation.payload.model_dump(mode="json"),
                confirmed_by=str(confirmation.confirmed_by),
                confirmed_at=confirmation.confirmed_at,
            )
        )
        await self._session.flush()

    async def list_for_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId, limit: int
    ) -> list[WorkUploadView]:
        self._require_tenant(tenant_id, "work_intake_upload_list_employee")
        rows = (
            (
                await self._session.execute(
                    select(WorkUploadRow)
                    .where(
                        WorkUploadRow.tenant_id == str(self._tenant_id),
                        WorkUploadRow.employee_id == str(employee_id),
                    )
                    .order_by(
                        WorkUploadRow.created_at.desc(),
                        WorkUploadRow.upload_id.desc(),
                    )
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [_upload_view(row) for row in rows]


__all__ = ("WorkIntakeRepositoryImpl",)
