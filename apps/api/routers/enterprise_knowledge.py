"""企业共享资料接口：租户与员工仅来自已认证身份。"""
from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from domains.products.schemas import (
    KnowledgeActor,
    KnowledgeConfirmCommand,
    KnowledgeDocumentDetail,
    KnowledgeDocumentView,
    KnowledgePage,
    KnowledgeRetryCommand,
    KnowledgeSyncCommand,
)
from shared.errors import TransientError

from ..composition.enterprise_knowledge import (
    KNOWLEDGE_UPLOAD_MIME_TYPES,
    EnterpriseKnowledgeApplication,
    validate_knowledge_file,
)
from ..dependencies import (
    ConfiguredApiDependencies,
    document_idempotency_header,
    get_api_dependencies,
    get_request_identity,
    raw_idempotency_key,
)
from ..identity import RequestIdentity
from ..middleware import ApiErrorResponse

router = APIRouter()
_ERRORS: dict[int | str, dict[str, Any]] = {400: {"model": ApiErrorResponse}, 403: {"model": ApiErrorResponse}, 409: {"model": ApiErrorResponse}, 503: {"model": ApiErrorResponse}}
Identity = Annotated[RequestIdentity, Depends(get_request_identity)]
Dependencies = Annotated[ConfiguredApiDependencies, Depends(get_api_dependencies)]


def _application(dependencies: ConfiguredApiDependencies) -> EnterpriseKnowledgeApplication:
    application = dependencies.enterprise_knowledge
    if application is None:
        raise TransientError("企业资料服务未配置")
    return application


def _actor(identity: RequestIdentity) -> KnowledgeActor:
    return KnowledgeActor(tenant_id=identity.tenant_id, employee_id=identity.employee.employee_id)


@router.get("/documents", response_model=KnowledgePage, responses=_ERRORS)
async def list_documents(identity: Identity, dependencies: Dependencies, query: Annotated[str, Query(max_length=200)] = "", limit: Annotated[int, Query(ge=1, le=100)] = 20, offset: Annotated[int, Query(ge=0, le=100000)] = 0) -> KnowledgePage:
    return await _application(dependencies).service.list_documents(identity.tenant_id, _actor(identity), query=query, limit=limit, offset=offset)


@router.post("/documents", status_code=202, response_model=KnowledgeDocumentView, responses={**_ERRORS, 413: {"model": ApiErrorResponse}}, openapi_extra={"requestBody": {"required": True, "content": {mime: {"schema": {"type": "string", "format": "binary"}} for mime in sorted(KNOWLEDGE_UPLOAD_MIME_TYPES)}}})
async def upload_document(request: Request, identity: Identity, dependencies: Dependencies, filename: Annotated[str, Query(min_length=1, max_length=240)], _idempotency: Annotated[None, Depends(document_idempotency_header)]) -> KnowledgeDocumentView:
    application = _application(dependencies)
    key = raw_idempotency_key(request)
    mime_types = request.headers.getlist("content-type")
    if len(mime_types) != 1:
        raise HTTPException(status_code=400)
    validate_knowledge_file(filename, mime_types[0])
    await application.service.authorize_upload(identity.tenant_id, _actor(identity))
    content = bytearray()
    async for chunk in request.stream():
        if len(content) + len(chunk) > application.maximum_upload_bytes:
            raise HTTPException(status_code=413)
        content.extend(chunk)
    return await application.upload(
        identity.tenant_id, _actor(identity), uploaded_by=identity.employee.user_id,
        filename=filename, mime_type=mime_types[0], content=bytes(content), idempotency_key=key,
    )


@router.get("/documents/{document_id}", response_model=KnowledgeDocumentDetail, responses=_ERRORS)
async def get_document(document_id: str, identity: Identity, dependencies: Dependencies) -> KnowledgeDocumentDetail:
    return await _application(dependencies).service.get_detail(identity.tenant_id, _actor(identity), document_id)


@router.get("/documents/{document_id}/source", response_class=Response, responses={**_ERRORS, 200: {"content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}}})
async def get_source(document_id: str, identity: Identity, dependencies: Dependencies) -> Response:
    filename, metadata, content = await _application(dependencies).source(identity.tenant_id, _actor(identity), document_id)
    return Response(content=content, media_type=metadata.mime_type, headers={
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox",
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename, safe=""),
    })


@router.post("/documents/{document_id}/confirm", response_model=KnowledgeDocumentDetail, responses=_ERRORS)
async def confirm_document(document_id: str, body: KnowledgeConfirmCommand, identity: Identity, dependencies: Dependencies) -> KnowledgeDocumentDetail:
    return await _application(dependencies).service.confirm(identity.tenant_id, _actor(identity), document_id, revision_id=body.revision_id, expected_version=body.expected_version)


@router.post("/documents/{document_id}/retry", status_code=202, response_model=KnowledgeDocumentView, responses=_ERRORS)
async def retry_document(document_id: str, body: KnowledgeRetryCommand, identity: Identity, dependencies: Dependencies) -> KnowledgeDocumentView:
    return await _application(dependencies).service.retry_processing(identity.tenant_id, _actor(identity), document_id, expected_version=body.expected_version, acknowledge_unknown=body.acknowledge_unknown)


@router.post("/documents/{document_id}/sync", status_code=202, response_model=KnowledgeDocumentView, responses=_ERRORS)
async def sync_document(document_id: str, body: KnowledgeSyncCommand, identity: Identity, dependencies: Dependencies) -> KnowledgeDocumentView:
    return await _application(dependencies).service.request_sync(identity.tenant_id, _actor(identity), document_id, revision_id=body.revision_id, expected_version=body.expected_version)
