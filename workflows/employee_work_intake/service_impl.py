"""员工工作上传的确定性状态与人工确认边界。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from shared.errors import InvalidStateTransition, PermissionDenied, ValidationError
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeConfirmationId,
    EmployeeId,
    TenantId,
    WorkExtractionId,
    WorkUploadId,
)
from workflows.employee_work_intake.repository import WorkIntakeUnitOfWorkFactory
from workflows.employee_work_intake.schemas import (
    EmployeeConfirmationView,
    ExtractionPayload,
    WorkExtractionView,
    WorkSourceKind,
    WorkUploadStatus,
    WorkUploadView,
)


class WorkIntakeServiceImpl:
    """未确认内容只保存在版本链，不调用任何业务域服务。"""

    def __init__(
        self,
        uow_factory: WorkIntakeUnitOfWorkFactory,
        *,
        now: Callable[[], datetime] | None = None,
        id_generator: Callable[[str], str],
    ) -> None:
        self._uow_factory = uow_factory
        self._now = now or (lambda: datetime.now(UTC))
        self._id_generator = id_generator

    def _clock(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValidationError("员工工作服务时间必须含时区")
        return value

    async def register_upload(
        self,
        tenant_id: TenantId,
        artifact_id: ArtifactId,
        employee_id: EmployeeId,
        source_kind: WorkSourceKind,
        *,
        occurred_at: datetime,
        customer_timezone: str,
    ) -> WorkUploadView:
        upload = WorkUploadView(
            upload_id=WorkUploadId(self._id_generator("upl")),
            tenant_id=tenant_id,
            artifact_id=artifact_id,
            employee_id=employee_id,
            source_kind=source_kind,
            status=WorkUploadStatus.UPLOADED,
            occurred_at=occurred_at,
            customer_timezone=customer_timezone,
            created_at=self._clock(),
        )
        async with self._uow_factory(tenant_id) as uow:
            await uow.work_intake.add_upload(upload)
        return upload

    async def record_extraction(
        self,
        tenant_id: TenantId,
        upload_id: WorkUploadId,
        payload: ExtractionPayload,
        *,
        extracted_by: str,
    ) -> WorkExtractionView:
        async with self._uow_factory(tenant_id) as uow:
            upload = await uow.work_intake.get_upload(tenant_id, upload_id)
            if upload is None:
                raise ValidationError("员工工作上传不存在")
            if upload.status is not WorkUploadStatus.UPLOADED:
                raise InvalidStateTransition("员工工作上传当前状态不可提取")
            extraction = WorkExtractionView(
                extraction_id=WorkExtractionId(self._id_generator("wex")),
                upload_id=upload_id,
                payload=payload,
                extracted_by=extracted_by,
                created_at=self._clock(),
                confirmation=None,
            )
            await uow.work_intake.add_extraction(tenant_id, extraction)
            await uow.work_intake.update_upload(
                upload.model_copy(
                    update={"status": WorkUploadStatus.AWAITING_CONFIRMATION}
                )
            )
        return extraction

    async def confirm(
        self,
        tenant_id: TenantId,
        upload_id: WorkUploadId,
        confirmed_by: EmployeeId,
        corrected_payload: ExtractionPayload,
    ) -> WorkExtractionView:
        async with self._uow_factory(tenant_id) as uow:
            upload = await uow.work_intake.get_upload(tenant_id, upload_id)
            if upload is None:
                raise ValidationError("员工工作上传不存在")
            if upload.employee_id != confirmed_by:
                raise PermissionDenied("只能确认本人上传的工作资料")
            if upload.status is not WorkUploadStatus.AWAITING_CONFIRMATION:
                raise InvalidStateTransition("员工工作上传当前状态不可确认")
            extraction = await uow.work_intake.get_latest_extraction(
                tenant_id, upload_id
            )
            if extraction is None or extraction.confirmation is not None:
                raise InvalidStateTransition("员工工作提取版本不可确认")
            confirmation = EmployeeConfirmationView(
                confirmation_id=EmployeeConfirmationId(
                    self._id_generator("wcf")
                ),
                revision=1,
                payload=corrected_payload,
                confirmed_by=confirmed_by,
                confirmed_at=self._clock(),
            )
            await uow.work_intake.append_confirmation(
                tenant_id, extraction.extraction_id, confirmation
            )
            await uow.work_intake.update_upload(
                upload.model_copy(update={"status": WorkUploadStatus.CONFIRMED})
            )
        return extraction.model_copy(update={"confirmation": confirmation})

    async def list_for_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId, limit: int
    ) -> list[WorkUploadView]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ValidationError("员工工作上传查询条数无效")
        async with self._uow_factory(tenant_id) as uow:
            return await uow.work_intake.list_for_employee(
                tenant_id, employee_id, limit
            )

    async def get_upload(
        self,
        tenant_id: TenantId,
        upload_id: WorkUploadId,
        employee_id: EmployeeId,
    ) -> WorkUploadView:
        async with self._uow_factory(tenant_id) as uow:
            upload = await uow.work_intake.get_upload(tenant_id, upload_id)
            if upload is None:
                raise ValidationError("员工工作上传不存在")
            if upload.employee_id != employee_id:
                raise PermissionDenied("只能查看本人上传的工作资料")
            return upload

    async def get_extraction(
        self,
        tenant_id: TenantId,
        upload_id: WorkUploadId,
        employee_id: EmployeeId,
    ) -> WorkExtractionView | None:
        async with self._uow_factory(tenant_id) as uow:
            upload = await uow.work_intake.get_upload(tenant_id, upload_id)
            if upload is None:
                raise ValidationError("员工工作上传不存在")
            if upload.employee_id != employee_id:
                raise PermissionDenied("只能查看本人上传的工作资料")
            return await uow.work_intake.get_latest_extraction(tenant_id, upload_id)


__all__ = ("WorkIntakeServiceImpl",)
