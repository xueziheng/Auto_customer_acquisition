"""员工工作提取只有本人最终确认后才能进入 confirmed。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Self

import pytest

from shared.errors import InvalidStateTransition, PermissionDenied
from shared.schemas.identifiers import (
    ArtifactId,
    EmployeeId,
    TenantId,
    WorkUploadId,
)
from workflows.employee_work_intake.repository import WorkIntakeRepository
from workflows.employee_work_intake.schemas import (
    ExtractedFact,
    ExtractionPayload,
    WorkSourceKind,
    WorkUploadStatus,
    WorkUploadView,
)
from workflows.employee_work_intake.service_impl import WorkIntakeServiceImpl

NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
TENANT = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0X")
OWNER = EmployeeId("emp_01K39P9M5D6K4A91YEQ80EJZ0X")
OTHER = EmployeeId("emp_01K39P9M5D6K4A91YEQ80EJZ0Y")


def _payload(value: str = "Customer needs 500 units") -> ExtractionPayload:
    return ExtractionPayload(
        facts=(
            ExtractedFact(
                fact_type="customer_statement",
                value=value,
                evidence_quote="We need 500 units",
            ),
        ),
    )


class _Repository(WorkIntakeRepository):
    def __init__(self) -> None:
        self.uploads: dict[str, WorkUploadView] = {}
        self.extractions: dict[str, object] = {}

    async def add_upload(self, upload: WorkUploadView) -> None:
        self.uploads[str(upload.upload_id)] = upload

    async def get_upload(self, tenant_id: TenantId, upload_id: WorkUploadId):
        upload = self.uploads.get(str(upload_id))
        return upload if upload is not None and upload.tenant_id == tenant_id else None

    async def update_upload(self, upload: WorkUploadView) -> None:
        self.uploads[str(upload.upload_id)] = upload

    async def add_extraction(self, tenant_id: TenantId, extraction) -> None:
        self.extractions[str(extraction.upload_id)] = extraction

    async def get_latest_extraction(
        self, tenant_id: TenantId, upload_id: WorkUploadId
    ):
        del tenant_id
        return self.extractions.get(str(upload_id))

    async def append_confirmation(
        self, tenant_id: TenantId, extraction_id, confirmation
    ) -> None:
        del tenant_id, extraction_id, confirmation

    async def list_for_employee(
        self, tenant_id: TenantId, employee_id: EmployeeId, limit: int
    ) -> list[WorkUploadView]:
        return [
            upload
            for upload in self.uploads.values()
            if upload.tenant_id == tenant_id and upload.employee_id == employee_id
        ][:limit]


class _Uow:
    def __init__(self, repository: _Repository) -> None:
        self.work_intake = repository

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb


def _service(repository: _Repository) -> WorkIntakeServiceImpl:
    counter = iter(
        (
            "upl_01K39P9M5D6K4A91YEQ80EJZ0X",
            "wex_01K39P9M5D6K4A91YEQ80EJZ0X",
            "wcf_01K39P9M5D6K4A91YEQ80EJZ0X",
        )
    )
    return WorkIntakeServiceImpl(
        lambda tenant_id: _Uow(repository),
        now=lambda: NOW,
        id_generator=lambda prefix: next(counter),
    )


@pytest.mark.asyncio
async def test_extraction_stays_pending_until_owner_confirms() -> None:
    repository = _Repository()
    service = _service(repository)

    upload = await service.register_upload(
        TENANT,
        ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0X"),
        OWNER,
        WorkSourceKind.PDF_TEXT,
        occurred_at=NOW,
        customer_timezone="Asia/Shanghai",
    )
    extraction = await service.record_extraction(
        TENANT, upload.upload_id, _payload(), extracted_by="team-operations-v1"
    )

    assert extraction.is_confirmed is False
    assert repository.uploads[str(upload.upload_id)].status is (
        WorkUploadStatus.AWAITING_CONFIRMATION
    )


@pytest.mark.asyncio
async def test_non_owner_cannot_confirm_and_original_payload_is_not_overwritten() -> None:
    repository = _Repository()
    service = _service(repository)
    upload = await service.register_upload(
        TENANT,
        ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0X"),
        OWNER,
        WorkSourceKind.PDF_TEXT,
        occurred_at=NOW,
        customer_timezone="Asia/Shanghai",
    )
    extraction = await service.record_extraction(
        TENANT, upload.upload_id, _payload(), extracted_by="team-operations-v1"
    )

    with pytest.raises(PermissionDenied):
        await service.confirm(TENANT, upload.upload_id, OTHER, _payload("Corrected"))

    confirmed = await service.confirm(
        TENANT, upload.upload_id, OWNER, _payload("Corrected by employee")
    )

    assert extraction.payload.facts[0].value == "Customer needs 500 units"
    assert confirmed.confirmation is not None
    assert confirmed.confirmation.payload.facts[0].value == "Corrected by employee"
    assert repository.uploads[str(upload.upload_id)].status is WorkUploadStatus.CONFIRMED


@pytest.mark.asyncio
async def test_confirm_is_single_final_transition() -> None:
    repository = _Repository()
    service = _service(repository)
    upload = await service.register_upload(
        TENANT,
        ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0X"),
        OWNER,
        WorkSourceKind.PDF_TEXT,
        occurred_at=NOW,
        customer_timezone="Asia/Shanghai",
    )
    await service.record_extraction(
        TENANT, upload.upload_id, _payload(), extracted_by="team-operations-v1"
    )
    await service.confirm(TENANT, upload.upload_id, OWNER, _payload())

    with pytest.raises(InvalidStateTransition):
        await service.confirm(TENANT, upload.upload_id, OWNER, _payload("Changed again"))


@pytest.mark.asyncio
async def test_owner_can_read_upload_and_extraction_but_other_employee_cannot() -> None:
    repository = _Repository()
    service = _service(repository)
    upload = await service.register_upload(
        TENANT,
        ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0X"),
        OWNER,
        WorkSourceKind.PDF_TEXT,
        occurred_at=NOW,
        customer_timezone="Asia/Shanghai",
    )
    extraction = await service.record_extraction(
        TENANT, upload.upload_id, _payload(), extracted_by="team-operations-v1"
    )

    assert await service.get_upload(TENANT, upload.upload_id, OWNER) == upload.model_copy(
        update={"status": WorkUploadStatus.AWAITING_CONFIRMATION}
    )
    assert await service.get_extraction(TENANT, upload.upload_id, OWNER) == extraction

    with pytest.raises(PermissionDenied):
        await service.get_upload(TENANT, upload.upload_id, OTHER)
    with pytest.raises(PermissionDenied):
        await service.get_extraction(TENANT, upload.upload_id, OTHER)
