"""员工上传 API 编排必须先保存原件，再登记工作批次。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from apps.api.composition.work_uploads import WorkUploadApplicationServiceImpl
from artifact_store.store import RawArtifactKind, RawArtifactMeta
from shared.schemas.identifiers import ArtifactId, EmployeeId, TenantId
from workflows.employee_work_intake.schemas import WorkSourceKind

NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
TENANT = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0X")
EMPLOYEE = EmployeeId("emp_01K39P9M5D6K4A91YEQ80EJZ0X")
ARTIFACT = ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0X")


class _Artifacts:
    def __init__(self, order: list[str]) -> None:
        self._order = order

    async def put(self, tenant_id, kind, content, mime_type, uploaded_by=None):
        self._order.append("artifact")
        return RawArtifactMeta(
            tenant_id,
            ARTIFACT,
            kind,
            "a" * 64,
            len(content),
            mime_type,
            uploaded_by,
            NOW,
        )


class _WorkIntake:
    def __init__(self, order: list[str]) -> None:
        self._order = order
        self.registered: tuple[object, ...] | None = None

    async def register_upload(self, *args, **kwargs):
        self._order.append("register")
        self.registered = (*args, kwargs)
        return object()


@pytest.mark.asyncio
async def test_original_artifact_is_persisted_before_upload_registration() -> None:
    order: list[str] = []
    intake = _WorkIntake(order)
    service = WorkUploadApplicationServiceImpl(
        _Artifacts(order),  # type: ignore[arg-type]
        intake,  # type: ignore[arg-type]
        1024,
    )

    await service.create_upload(
        TENANT,
        EMPLOYEE,
        uploaded_by=None,
        artifact_kind=RawArtifactKind.PDF,
        source_kind=WorkSourceKind.PDF_TEXT,
        content=b"pdf",
        mime_type="application/pdf",
        occurred_at=NOW,
        customer_timezone="Asia/Shanghai",
    )

    assert order == ["artifact", "register"]
    assert intake.registered is not None
    assert intake.registered[:3] == (TENANT, ARTIFACT, EMPLOYEE)
