"""员工工作上传的公共编排接口。"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from shared.schemas.identifiers import ArtifactId, EmployeeId, TenantId, WorkUploadId
from workflows.employee_work_intake.schemas import (
    ExtractionPayload,
    WorkExtractionView,
    WorkSourceKind,
    WorkUploadView,
)


@runtime_checkable
class WorkIntakeService(Protocol):
    async def register_upload(
        self,
        tenant_id: TenantId,
        artifact_id: ArtifactId,
        employee_id: EmployeeId,
        source_kind: WorkSourceKind,
        *,
        occurred_at: datetime,
        customer_timezone: str,
    ) -> WorkUploadView: ...

    async def record_extraction(
        self,
        tenant_id: TenantId,
        upload_id: WorkUploadId,
        payload: ExtractionPayload,
        *,
        extracted_by: str,
    ) -> WorkExtractionView: ...

    async def confirm(
        self,
        tenant_id: TenantId,
        upload_id: WorkUploadId,
        confirmed_by: EmployeeId,
        corrected_payload: ExtractionPayload,
    ) -> WorkExtractionView: ...

    async def list_for_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId, limit: int
    ) -> list[WorkUploadView]: ...

    async def get_upload(
        self,
        tenant_id: TenantId,
        upload_id: WorkUploadId,
        employee_id: EmployeeId,
    ) -> WorkUploadView: ...

    async def get_extraction(
        self,
        tenant_id: TenantId,
        upload_id: WorkUploadId,
        employee_id: EmployeeId,
    ) -> WorkExtractionView | None: ...


__all__ = ("WorkIntakeService",)
