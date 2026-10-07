"""员工工作上传版本链的 tenant-bound 持久化契约。"""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from shared.schemas.identifiers import (
    EmployeeId,
    TenantId,
    WorkExtractionId,
    WorkUploadId,
)
from workflows.employee_work_intake.schemas import (
    EmployeeConfirmationView,
    WorkExtractionView,
    WorkUploadView,
)


@runtime_checkable
class WorkIntakeRepository(Protocol):
    """不提供删除或覆盖提取/确认版本的方法。"""

    async def add_upload(self, upload: WorkUploadView) -> None: ...

    async def get_upload(
        self, tenant_id: TenantId, upload_id: WorkUploadId
    ) -> WorkUploadView | None: ...

    async def update_upload(self, upload: WorkUploadView) -> None: ...

    async def add_extraction(
        self, tenant_id: TenantId, extraction: WorkExtractionView
    ) -> None: ...

    async def get_latest_extraction(
        self, tenant_id: TenantId, upload_id: WorkUploadId
    ) -> WorkExtractionView | None: ...

    async def append_confirmation(
        self,
        tenant_id: TenantId,
        extraction_id: WorkExtractionId,
        confirmation: EmployeeConfirmationView,
    ) -> None: ...

    async def list_for_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId, limit: int
    ) -> list[WorkUploadView]: ...


@runtime_checkable
class WorkIntakeUnitOfWork(Protocol):
    work_intake: WorkIntakeRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


@runtime_checkable
class WorkIntakeUnitOfWorkFactory(Protocol):
    def __call__(self, tenant_id: TenantId) -> WorkIntakeUnitOfWork: ...


__all__ = (
    "WorkIntakeRepository",
    "WorkIntakeUnitOfWork",
    "WorkIntakeUnitOfWorkFactory",
)
