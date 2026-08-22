"""员工工作上传的 Artifact Store 与版本链 API 编排。"""

from __future__ import annotations

from datetime import datetime

from artifact_store.store import RawArtifactKind, RawArtifactStore
from shared.schemas.identifiers import EmployeeId, TenantId, UserId, WorkUploadId
from workflows.employee_work_intake.schemas import (
    ExtractionPayload,
    WorkExtractionView,
    WorkSourceKind,
    WorkUploadView,
)
from workflows.employee_work_intake.service import WorkIntakeService


class WorkUploadApplicationServiceImpl:
    """原件先不可变落库，再登记待提取上传批次。"""

    def __init__(
        self,
        artifacts: RawArtifactStore,
        work_intake: WorkIntakeService,
        maximum_upload_bytes: int,
    ) -> None:
        if (
            not isinstance(maximum_upload_bytes, int)
            or isinstance(maximum_upload_bytes, bool)
            or maximum_upload_bytes <= 0
        ):
            raise ValueError("员工工作上传大小上限无效")
        self._artifacts = artifacts
        self._work_intake = work_intake
        self._maximum_upload_bytes = maximum_upload_bytes

    @property
    def maximum_upload_bytes(self) -> int:
        return self._maximum_upload_bytes

    async def create_upload(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        *,
        uploaded_by: UserId | None,
        artifact_kind: RawArtifactKind,
        source_kind: WorkSourceKind,
        content: bytes,
        mime_type: str,
        occurred_at: datetime,
        customer_timezone: str,
    ) -> WorkUploadView:
        artifact = await self._artifacts.put(
            tenant_id,
            artifact_kind,
            content,
            mime_type,
            uploaded_by,
        )
        return await self._work_intake.register_upload(
            tenant_id,
            artifact.artifact_id,
            employee_id,
            source_kind,
            occurred_at=occurred_at,
            customer_timezone=customer_timezone,
        )

    async def list_for_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId, limit: int
    ) -> list[WorkUploadView]:
        return await self._work_intake.list_for_employee(
            tenant_id, employee_id, limit
        )

    async def get_extraction(
        self,
        tenant_id: TenantId,
        upload_id: WorkUploadId,
        employee_id: EmployeeId,
    ) -> WorkExtractionView | None:
        return await self._work_intake.get_extraction(
            tenant_id, upload_id, employee_id
        )

    async def confirm(
        self,
        tenant_id: TenantId,
        upload_id: WorkUploadId,
        employee_id: EmployeeId,
        payload: ExtractionPayload,
    ) -> WorkExtractionView:
        return await self._work_intake.confirm(
            tenant_id, upload_id, employee_id, payload
        )


__all__ = ("WorkUploadApplicationServiceImpl",)
