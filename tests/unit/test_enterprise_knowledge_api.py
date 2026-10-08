"""企业资料 HTTP 必须绑定当前身份、限制原件并拒绝未配置运行。"""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from apps.api.composition.enterprise_knowledge import (
    EnterpriseKnowledgeApplication,
    validate_knowledge_file,
)
from apps.api.dependencies import get_api_dependencies, get_request_identity
from apps.api.identity import RequestIdentity
from apps.api.main import create_app
from apps.api.middleware import ApiSettings
from artifact_store.store import (
    RAW_ARTIFACT_MIME_TYPES,
    RawArtifactKind,
    RawArtifactMeta,
)
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.schemas import EmployeeView
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.permissions import OpportunityScope
from domains.products.schemas import (
    KnowledgeDocumentDetail,
    KnowledgeDocumentView,
    KnowledgePage,
    KnowledgeSource,
)
from shared.errors import PermissionDenied
from shared.schemas.identifiers import ArtifactId, EmployeeId, TenantId

NOW = datetime(2026, 10, 8, tzinfo=UTC)
TENANT = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0X")
EMPLOYEE = EmployeeId("emp_01K39P9M5D6K4A91YEQ80EJZ0X")
ARTIFACT = ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0X")


class Artifacts:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.content = b"Material steel"
        self.metadata = RawArtifactMeta(TENANT, ARTIFACT, RawArtifactKind.TEXT, hashlib.sha256(self.content).hexdigest(), len(self.content), "text/plain", None, NOW)

    async def put(self, tenant_id: TenantId, kind: RawArtifactKind, content: bytes, mime_type: str, uploaded_by: object) -> RawArtifactMeta:
        assert tenant_id == TENANT
        assert kind is RawArtifactKind.TEXT
        assert mime_type == "text/plain"
        self.calls.append("put")
        self.content = content
        self.metadata = RawArtifactMeta(TENANT, ARTIFACT, kind, hashlib.sha256(content).hexdigest(), len(content), mime_type, None, NOW)
        return self.metadata

    async def get_bounded(self, tenant_id: TenantId, artifact_id: ArtifactId, *, maximum_bytes: int) -> tuple[RawArtifactMeta, bytes]:
        assert tenant_id == TENANT and artifact_id == ARTIFACT and maximum_bytes == 32
        self.calls.append("bounded")
        return self.metadata, self.content


class Knowledge:
    def __init__(self, calls: list[str], artifacts: Artifacts) -> None:
        self.calls = calls
        self.deny = False
        self.received: dict[str, Any] = {}
        self.doc = KnowledgeDocumentView(document_id="kdoc_fixture", tenant_id=TENANT, source=KnowledgeSource(filename="spec.txt", mime_type="text/plain", size_bytes=len(artifacts.content), sha256=artifacts.metadata.content_hash, artifact_id=ARTIFACT), uploader_id=EMPLOYEE, status="queued", version=1, job_id="run_fixture", created_at=NOW, updated_at=NOW)

    async def authorize_upload(self, tenant_id: TenantId, actor: Any) -> None:
        assert tenant_id == TENANT and actor.employee_id == EMPLOYEE and actor.tenant_id == TENANT
        if self.deny:
            raise PermissionDenied("拒绝")
        self.calls.append("authorize")

    async def register_upload(self, tenant_id: TenantId, actor: Any, source: KnowledgeSource, *, idempotency_key: str) -> KnowledgeDocumentView:
        self.calls.append("register")
        self.received = {"tenant": tenant_id, "employee": actor.employee_id, "source": source, "key": idempotency_key}
        return self.doc.model_copy(update={"source": source})

    async def list_documents(self, tenant_id: TenantId, actor: Any, **query: Any) -> KnowledgePage:
        self.received = {"tenant": tenant_id, "employee": actor.employee_id, **query}
        return KnowledgePage(items=(self.doc,), total=1, limit=query["limit"], offset=query["offset"])

    async def get_detail(self, tenant_id: TenantId, actor: Any, document_id: str) -> KnowledgeDocumentDetail:
        if self.deny:
            raise PermissionDenied("拒绝")
        assert tenant_id == TENANT and actor.employee_id == EMPLOYEE and document_id == self.doc.document_id
        self.calls.append("detail")
        return KnowledgeDocumentDetail(document=self.doc, revision=None)

    async def retry_processing(self, tenant_id: TenantId, actor: Any, document_id: str, **body: Any) -> KnowledgeDocumentView:
        self.received = {"tenant": tenant_id, "employee": actor.employee_id, "document_id": document_id, **body}
        return self.doc

    async def confirm(self, tenant_id: TenantId, actor: Any, document_id: str, **body: Any) -> KnowledgeDocumentDetail:
        raise PermissionDenied("仅企业管理员确认")

    async def request_sync(self, tenant_id: TenantId, actor: Any, document_id: str, **body: Any) -> KnowledgeDocumentView:
        self.received = {"tenant": tenant_id, "employee": actor.employee_id, "document_id": document_id, **body}
        return self.doc


def setup() -> tuple[Any, Knowledge, list[str]]:
    calls: list[str] = []
    artifacts = Artifacts(calls)
    knowledge = Knowledge(calls, artifacts)
    application = EnterpriseKnowledgeApplication(knowledge, artifacts, maximum_upload_bytes=32)  # type: ignore[arg-type]
    employee = EmployeeView(employee_id=EMPLOYEE, tenant_id=TENANT, name="测试员工", role="sales")
    identity = RequestIdentity(tenant_id=TENANT, employee=employee, employee_actor=EmployeeActor(str(EMPLOYEE), EmployeeScope.SELF, "sales"), opportunity_actor=OpportunityActor(str(EMPLOYEE), OpportunityScope(), "sales"))
    app = create_app(settings=ApiSettings(tenant_id=str(TENANT), dev_mode=True, retry_after_seconds=17))
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(enterprise_knowledge=application)
    app.dependency_overrides[get_request_identity] = lambda: identity
    return app, knowledge, calls


@pytest.mark.asyncio
async def test_upload_binds_identity_preserves_key_and_queues_only_after_raw_save() -> None:
    app, service, calls = setup()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tenant-Id": str(TENANT)}) as client:
        response = await client.post("/knowledge/documents?filename=spec.txt", content=b"Material steel", headers={"Content-Type": "text/plain", "Idempotency-Key": "synthetic-upload-intent"})
    assert response.status_code == 202
    assert service.received["tenant"] == TENANT and service.received["employee"] == EMPLOYEE
    assert service.received["key"] == "synthetic-upload-intent"
    assert calls.index("authorize") < calls.index("put") < calls.index("register")
    assert response.json()["status"] == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize(("filename", "mime", "content", "key", "status"), [
    ("../secret.txt", "text/plain", b"text", "upload", 400),
    ("AGENTS.md", "text/markdown", b"text", "upload", 400),
    ("spec.txt", "application/pdf", b"text", "upload", 400),
    ("spec.txt", "text/plain", b"x" * 33, "upload", 413),
    ("spec.txt", "text/plain", b"text", None, 400),
    ("spec.txt", "text/plain", b"\xff", "upload", 400),
    ("spec.png", "image/png", b"not-a-png", "upload", 400),
    ("spec.jpg", "image/png", b"not-a-jpeg", "upload", 400),
    ("spec.webp", "image/webp", b"RIFF1234OTHER", "upload", 400),
])
async def test_invalid_upload_never_writes_raw(filename: str, mime: str, content: bytes, key: str | None, status: int) -> None:
    app, _, calls = setup()
    headers = {"Content-Type": mime}
    if key is not None:
        headers["Idempotency-Key"] = key
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tenant-Id": str(TENANT)}) as client:
        response = await client.post("/knowledge/documents", params={"filename": filename}, content=content, headers=headers)
    assert response.status_code == status
    assert "put" not in calls


@pytest.mark.asyncio
async def test_forbidden_upload_never_reads_body_or_writes_store() -> None:
    app, service, calls = setup()
    service.deny = True
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tenant-Id": str(TENANT)}) as client:
        response = await client.post("/knowledge/documents?filename=spec.txt", content=b"Material steel", headers={"Content-Type": "text/plain", "Idempotency-Key": "synthetic-upload"})
    assert response.status_code == 403 and calls == []


@pytest.mark.asyncio
async def test_source_download_authorizes_before_bounded_io_and_again_after() -> None:
    app, _, calls = setup()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tenant-Id": str(TENANT)}) as client:
        response = await client.get("/knowledge/documents/kdoc_fixture/source")
    assert response.status_code == 200 and response.content == b"Material steel"
    assert calls == ["detail", "bounded", "detail"]
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"].startswith("attachment;")


@pytest.mark.asyncio
async def test_mutations_reject_client_identity_and_leave_confirmation_to_domain() -> None:
    app, service, _ = setup()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tenant-Id": str(TENANT)}) as client:
        rejected = await client.post("/knowledge/documents/kdoc_fixture/retry", json={"expected_version": 1, "tenant_id": "other"})
        confirm = await client.post("/knowledge/documents/kdoc_fixture/confirm", json={"expected_version": 1, "revision_id": "revision"})
        retry = await client.post("/knowledge/documents/kdoc_fixture/retry", json={"expected_version": 1, "acknowledge_unknown": True})
    assert rejected.status_code == 400
    assert confirm.status_code == 403
    assert retry.status_code == 202
    assert service.received["tenant"] == TENANT and service.received["acknowledge_unknown"] is True


@pytest.mark.asyncio
async def test_unconfigured_application_fails_closed() -> None:
    app, _, _ = setup()
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(enterprise_knowledge=None)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers={"X-Tenant-Id": str(TENANT)}) as client:
        response = await client.get("/knowledge/documents")
    assert response.status_code == 503


def test_openapi_exports_typed_commands_and_binary_upload_contract() -> None:
    schema = create_app().openapi()
    paths = schema["paths"]
    upload = paths["/knowledge/documents"]["post"]
    assert upload["requestBody"]["required"] is True
    assert set(upload["requestBody"]["content"]) == {"text/plain", "text/markdown", "text/csv", "application/pdf", "image/png", "image/jpeg", "image/webp", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
    assert any(p["name"] == "Idempotency-Key" and p["required"] for p in upload["parameters"])
    assert "KnowledgeDocumentDetail" in schema["components"]["schemas"]

@pytest.mark.parametrize(("filename", "mime", "kind"), [
    ("spec.txt", "text/plain", RawArtifactKind.TEXT),
    ("spec.md", "text/markdown", RawArtifactKind.TEXT),
    ("spec.csv", "text/csv", RawArtifactKind.EXCEL),
    ("spec.pdf", "application/pdf", RawArtifactKind.PDF),
    ("spec.png", "image/png", RawArtifactKind.IMAGE),
    ("spec.jpg", "image/jpeg", RawArtifactKind.IMAGE),
    ("spec.webp", "image/webp", RawArtifactKind.IMAGE),
    ("spec.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", RawArtifactKind.WORD),
    ("spec.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", RawArtifactKind.EXCEL),
])
def test_supported_uploads_match_real_raw_artifact_contract(filename: str, mime: str, kind: RawArtifactKind) -> None:
    assert validate_knowledge_file(filename, mime) is kind
    assert mime in RAW_ARTIFACT_MIME_TYPES[kind]
    metadata = RawArtifactMeta(TENANT, ARTIFACT, kind, "a" * 64, 1, mime, None, NOW)
    assert metadata.mime_type == mime
