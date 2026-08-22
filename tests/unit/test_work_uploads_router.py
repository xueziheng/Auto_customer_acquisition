"""Work Uploads API 必须绑定当前员工并限制原件请求体大小。"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient, Response

from apps.api.dependencies import get_api_dependencies, get_request_identity
from apps.api.identity import RequestIdentity
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.schemas import EmployeeView
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.permissions import OpportunityScope
from shared.schemas.identifiers import ArtifactId, EmployeeId, TenantId, WorkUploadId
from workflows.employee_work_intake.schemas import (
    ExtractedFact,
    ExtractionPayload,
    WorkExtractionView,
    WorkSourceKind,
    WorkUploadStatus,
    WorkUploadView,
)

NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
TENANT = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0X")
EMPLOYEE = EmployeeId("emp_01K39P9M5D6K4A91YEQ80EJZ0X")
UPLOAD = WorkUploadId("upl_01K39P9M5D6K4A91YEQ80EJZ0X")


def _payload() -> ExtractionPayload:
    return ExtractionPayload(
        facts=(
            ExtractedFact(
                fact_type="customer_statement",
                value="500 units",
                evidence_quote="We need 500 units",
            ),
        )
    )


class _WorkUploads:
    maximum_upload_bytes = 8

    def __init__(self) -> None:
        self.upload_calls: list[tuple[object, ...]] = []
        self.confirm_calls: list[tuple[object, ...]] = []

    async def create_upload(self, *args: object, **kwargs: object) -> WorkUploadView:
        self.upload_calls.append((*args, kwargs))
        return WorkUploadView(
            upload_id=UPLOAD,
            tenant_id=TENANT,
            artifact_id=ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0X"),
            employee_id=EMPLOYEE,
            source_kind=WorkSourceKind.PDF_TEXT,
            status=WorkUploadStatus.UPLOADED,
            occurred_at=NOW,
            customer_timezone="Asia/Shanghai",
            created_at=NOW,
        )

    async def list_for_employee(self, *args: object) -> list[WorkUploadView]:
        del args
        return []

    async def get_extraction(self, *args: object) -> WorkExtractionView:
        del args
        return WorkExtractionView(
            extraction_id="wex_01K39P9M5D6K4A91YEQ80EJZ0X",
            upload_id=UPLOAD,
            payload=_payload(),
            extracted_by="team-operations-v1",
            created_at=NOW,
            confirmation=None,
        )

    async def confirm(self, *args: object) -> WorkExtractionView:
        self.confirm_calls.append(args)
        return await self.get_extraction()


def _identity() -> RequestIdentity:
    employee = EmployeeView(
        employee_id=EMPLOYEE,
        tenant_id=TENANT,
        name="测试员工",
        role="sales",
    )
    return RequestIdentity(
        tenant_id=TENANT,
        employee=employee,
        employee_actor=EmployeeActor(str(EMPLOYEE), EmployeeScope.SELF, "sales"),
        opportunity_actor=OpportunityActor(
            str(EMPLOYEE), OpportunityScope(), "sales"
        ),
    )


def _app(service: _WorkUploads):
    app = create_app(
        settings=ApiSettings(
            tenant_id=str(TENANT), dev_mode=True, retry_after_seconds=17
        )
    )
    app.dependency_overrides[get_request_identity] = _identity
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        work_uploads=service
    )
    return app


def _request(
    app: object,
    method: str,
    path: str,
    *,
    content: bytes | None = None,
    json: dict[str, object] | None = None,
) -> Response:
    async def run() -> Response:
        async with AsyncClient(
            transport=ASGITransport(app=app),  # type: ignore[arg-type]
            base_url="http://test",
        ) as client:
            return await client.request(
                method,
                path,
                headers={
                    "X-Tenant-Id": str(TENANT),
                    **(
                        {"Content-Type": "application/pdf"}
                        if content is not None
                        else {}
                    ),
                },
                content=content,
                json=json,
            )

    return asyncio.run(run())


def test_upload_binds_tenant_and_employee_without_accepting_them_in_body() -> None:
    service = _WorkUploads()
    app = _app(service)

    response = _request(
        app,
        "POST",
        "/work-uploads?artifact_kind=pdf&source_kind=pdf_text&"
        "occurred_at=2026-08-22T12:00:00Z&customer_timezone=Asia%2FShanghai",
        content=b"pdf",
    )

    assert response.status_code == 201
    assert response.json()["upload_id"] == str(UPLOAD)
    assert service.upload_calls[0][0:2] == (TENANT, EMPLOYEE)
    assert str(TENANT) not in response.request.url.query.decode()
    assert str(EMPLOYEE) not in response.request.url.query.decode()


def test_upload_rejects_oversize_before_calling_artifact_store() -> None:
    service = _WorkUploads()
    response = _request(
        _app(service),
        "POST",
        "/work-uploads?artifact_kind=pdf&source_kind=pdf_text&"
        "occurred_at=2026-08-22T12:00:00Z&customer_timezone=Asia%2FShanghai",
        content=b"123456789",
    )

    assert response.status_code == 400
    assert service.upload_calls == []


def test_extraction_and_confirmation_are_bound_to_request_employee() -> None:
    service = _WorkUploads()
    app = _app(service)

    extraction = _request(app, "GET", f"/work-uploads/{UPLOAD}/extraction")
    confirmed = _request(
        app,
        "POST",
        f"/work-uploads/{UPLOAD}/confirm",
        json=_payload().model_dump(mode="json"),
    )

    assert extraction.status_code == 200
    assert confirmed.status_code == 200
    assert service.confirm_calls[0][0:3] == (TENANT, UPLOAD, EMPLOYEE)
