"""企业共享资料服务；模型草稿、人工确认和文件投影分别推进。"""
from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from domains.products.knowledge_repository import (
    KnowledgeRepository,
    KnowledgeUnitOfWorkFactory,
)
from domains.products.knowledge_schemas import (
    _SAFE_FAILURES,
    KnowledgeActor,
    KnowledgeAnalysis,
    KnowledgeClaim,
    KnowledgeDocumentDetail,
    KnowledgeDocumentView,
    KnowledgeExportState,
    KnowledgeJob,
    KnowledgePage,
    KnowledgeRevisionView,
    KnowledgeSource,
    KnowledgeSourceKind,
    require_parse_warnings,
    require_safe_text,
)
from shared.errors import (
    IdempotencyConflict,
    InvalidStateTransition,
    PermissionDenied,
    ValidationError,
)
from shared.schemas.identifiers import TenantId, new_id
from shared.schemas.provenance import ProvenanceSummary, SourceType


class EnterpriseKnowledgeService(Protocol):
    async def authorize_upload(self, tenant_id: TenantId, actor: KnowledgeActor) -> None: ...
    async def register_upload(self, tenant_id: TenantId, actor: KnowledgeActor, source: KnowledgeSource, *, idempotency_key: str) -> KnowledgeDocumentView: ...
    async def list_documents(self, tenant_id: TenantId, actor: KnowledgeActor, *, query: str = "", limit: int = 50, offset: int = 0) -> KnowledgePage: ...
    async def get_detail(self, tenant_id: TenantId, actor: KnowledgeActor, document_id: str) -> KnowledgeDocumentDetail: ...
    async def confirm(self, tenant_id: TenantId, actor: KnowledgeActor, document_id: str, *, revision_id: str, expected_version: int) -> KnowledgeDocumentDetail: ...
    async def retry_processing(self, tenant_id: TenantId, actor: KnowledgeActor, document_id: str, *, expected_version: int, acknowledge_unknown: bool = False) -> KnowledgeDocumentView: ...
    async def request_sync(self, tenant_id: TenantId, actor: KnowledgeActor, document_id: str, *, revision_id: str, expected_version: int) -> KnowledgeDocumentView: ...
    async def claim_next(self, tenant_id: TenantId, *, lease_owner: str, lease_seconds: int = 300) -> KnowledgeClaim | None: ...
    async def authorize_processing(self, tenant_id: TenantId, claim: KnowledgeClaim) -> None: ...
    async def complete_processing(self, tenant_id: TenantId, claim: KnowledgeClaim, *, source_text: str, analysis: KnowledgeAnalysis, model: str, extracted_by: str, source_kind: KnowledgeSourceKind = "document_text", image_count: int = 0, image_source_pages: tuple[int, ...] = (), parse_warnings: tuple[str, ...] = ()) -> KnowledgeDocumentDetail: ...
    async def fail_processing(self, tenant_id: TenantId, claim: KnowledgeClaim, *, reason: str, uncertain: bool) -> KnowledgeDocumentView: ...
    async def list_pending_exports(self, tenant_id: TenantId, *, limit: int = 10) -> tuple[KnowledgeDocumentDetail, ...]: ...
    async def mark_export(self, tenant_id: TenantId, document_id: str, *, revision_id: str, expected_version: int, state: KnowledgeExportState) -> KnowledgeDocumentView: ...


def _permissions(document: KnowledgeDocumentView, role: str, actor: KnowledgeActor) -> KnowledgeDocumentView:
    retry = role == "boss" or document.uploader_id == actor.employee_id
    return document.model_copy(update={
        "can_retry": retry and document.status in {"failed", "unknown"},
        "can_confirm": role == "boss" and document.status == "awaiting_confirmation",
        "can_sync": document.current_revision_id is not None and document.export_state != "synced",
    })


class EnterpriseKnowledgeServiceImpl:
    """租户事务锁下原子推进；执行租约过期只标未知，绝不自动重放模型。"""

    def __init__(self, uow_factory: KnowledgeUnitOfWorkFactory, *, now: Callable[[], datetime] | None = None) -> None:
        self._uow_factory = uow_factory
        self._now = now or (lambda: datetime.now(UTC))

    async def _authorize(self, repo: KnowledgeRepository, tenant_id: TenantId, actor: KnowledgeActor, *, boss: bool = False) -> tuple[str, str]:
        if actor.tenant_id != tenant_id:
            raise PermissionDenied("企业资料权限不足")
        identity = await repo.employee_identity(actor.employee_id)
        if identity is None or identity[0] not in ({"boss"} if boss else {"boss", "sales"}):
            raise PermissionDenied("企业资料权限不足")
        return identity

    async def _document(self, repo: KnowledgeRepository, document_id: str) -> KnowledgeDocumentView:
        document = await repo.get_document(document_id)
        if document is None:
            raise ValidationError("企业资料不存在")
        return document

    async def _detail(self, repo: KnowledgeRepository, document: KnowledgeDocumentView) -> KnowledgeDocumentDetail:
        revision = await repo.get_revision(document.current_revision_id) if document.current_revision_id else None
        return KnowledgeDocumentDetail(document=document, revision=revision)

    def _changed(self, document: KnowledgeDocumentView, **changes: object) -> KnowledgeDocumentView:
        return document.model_copy(update={**changes, "version": document.version + 1, "updated_at": self._now(), "can_retry": False, "can_confirm": False, "can_sync": False})

    async def authorize_upload(self, tenant_id: TenantId, actor: KnowledgeActor) -> None:
        async with self._uow_factory(tenant_id) as uow:
            await self._authorize(uow.knowledge, tenant_id, actor)

    async def register_upload(self, tenant_id: TenantId, actor: KnowledgeActor, source: KnowledgeSource, *, idempotency_key: str) -> KnowledgeDocumentView:
        if not idempotency_key or idempotency_key != idempotency_key.strip() or len(idempotency_key) > 200:
            raise ValidationError("资料上传幂等键无效")
        source = KnowledgeSource.model_validate(source.model_dump())
        key_hash = hashlib.sha256(idempotency_key.encode()).hexdigest()
        request_hash = hashlib.sha256(json.dumps(source.model_dump(mode="json"), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        async with self._uow_factory(tenant_id) as uow:
            repo = uow.knowledge
            await repo.lock()
            role, user_id = await self._authorize(repo, tenant_id, actor)
            existing_request = await repo.get_request(actor.employee_id, key_hash)
            if existing_request:
                if existing_request[0] != request_hash:
                    raise IdempotencyConflict("同一上传请求已绑定其他资料")
                return _permissions(await self._document(repo, existing_request[1]), role, actor)
            document = await repo.get_by_artifact(source.artifact_id)
            if document is None:
                now = self._now()
                document = KnowledgeDocumentView(document_id=new_id("kdoc"), tenant_id=tenant_id, source=source, uploader_id=actor.employee_id, status="queued", version=1, job_id=new_id("run"), created_at=now, updated_at=now)
                if not await repo.source_matches(document):
                    raise ValidationError("资料原件元数据不一致")
                await repo.save_document(document)
                await repo.save_job(KnowledgeJob(tenant_id=tenant_id, job_id=document.job_id, document_id=document.document_id, actor_id=actor.employee_id, user_id=user_id, state="queued", attempt=1, created_at=now))
            await repo.save_request(actor.employee_id, key_hash, request_hash, document.document_id)
            return _permissions(document, role, actor)

    async def list_documents(self, tenant_id: TenantId, actor: KnowledgeActor, *, query: str = "", limit: int = 50, offset: int = 0) -> KnowledgePage:
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset <= 10000 or len(query) > 200:
            raise ValidationError("资料查询范围无效")
        async with self._uow_factory(tenant_id) as uow:
            role, _ = await self._authorize(uow.knowledge, tenant_id, actor)
            documents, total = await uow.knowledge.list_documents(query.strip(), limit, offset)
            return KnowledgePage(items=tuple(_permissions(item, role, actor) for item in documents), total=total, limit=limit, offset=offset)

    async def get_detail(self, tenant_id: TenantId, actor: KnowledgeActor, document_id: str) -> KnowledgeDocumentDetail:
        async with self._uow_factory(tenant_id) as uow:
            role, _ = await self._authorize(uow.knowledge, tenant_id, actor)
            return await self._detail(uow.knowledge, _permissions(await self._document(uow.knowledge, document_id), role, actor))

    async def _expire(self, repo: KnowledgeRepository) -> None:
        for job in await repo.expired_jobs(self._now()):
            await repo.save_job(job.model_copy(update={"state": "unknown"}))
            document = await self._document(repo, job.document_id)
            if document.job_id == job.job_id and document.status == "processing":
                await repo.save_document(self._changed(document, status="unknown", failure_reason="lease_expired"))

    async def claim_next(self, tenant_id: TenantId, *, lease_owner: str, lease_seconds: int = 300) -> KnowledgeClaim | None:
        if not lease_owner.strip() or type(lease_seconds) is not int or not 30 <= lease_seconds <= 3600:
            raise ValidationError("资料处理租约无效")
        async with self._uow_factory(tenant_id) as uow:
            repo = uow.knowledge
            await repo.lock()
            await self._expire(repo)
            job = await repo.claimable_job()
            if job is None:
                return None
            document = await self._document(repo, job.document_id)
            identity = await repo.employee_identity(job.actor_id)
            if identity is None or identity[0] not in {"boss", "sales"} or identity[1] != job.user_id:
                await repo.save_job(job.model_copy(update={"state": "failed"}))
                await repo.save_document(self._changed(document, status="failed", failure_reason="authorization_revoked"))
                return None
            token = secrets.token_hex(24)
            until = self._now() + timedelta(seconds=lease_seconds)
            await repo.save_job(job.model_copy(update={"state": "processing", "lease_token": token, "lease_until": until}))
            await repo.save_document(self._changed(document, status="processing", failure_reason=None))
            return KnowledgeClaim(tenant_id=tenant_id, job_id=job.job_id, document_id=job.document_id, actor_id=job.actor_id, user_id=job.user_id, lease_token=token, lease_until=until, attempt=job.attempt, source=document.source)

    async def _claim(self, repo: KnowledgeRepository, tenant_id: TenantId, claim: KnowledgeClaim, *, authorize: bool = True) -> tuple[KnowledgeJob, KnowledgeDocumentView]:
        if claim.tenant_id != tenant_id:
            raise PermissionDenied("企业资料权限不足")
        job = await repo.get_job(claim.job_id)
        document = await self._document(repo, claim.document_id)
        if job is None or job.document_id != claim.document_id or job.actor_id != claim.actor_id or job.user_id != claim.user_id or job.lease_token != claim.lease_token or job.lease_until != claim.lease_until or job.attempt != claim.attempt or document.source != claim.source or job.state != "processing" or job.lease_until is None or job.lease_until <= self._now() or document.job_id != job.job_id or document.status != "processing":
            raise InvalidStateTransition("资料处理租约已失效")
        if authorize:
            _, user_id = await self._authorize(repo, tenant_id, KnowledgeActor(tenant_id=tenant_id, employee_id=claim.actor_id))
            if user_id != claim.user_id:
                raise PermissionDenied("企业资料权限不足")
        return job, document

    async def authorize_processing(self, tenant_id: TenantId, claim: KnowledgeClaim) -> None:
        async with self._uow_factory(tenant_id) as uow:
            await uow.knowledge.lock()
            await self._claim(uow.knowledge, tenant_id, claim)

    async def complete_processing(self, tenant_id: TenantId, claim: KnowledgeClaim, *, source_text: str, analysis: KnowledgeAnalysis, model: str, extracted_by: str, source_kind: KnowledgeSourceKind = "document_text", image_count: int = 0, image_source_pages: tuple[int, ...] = (), parse_warnings: tuple[str, ...] = ()) -> KnowledgeDocumentDetail:
        if not isinstance(source_text, str) or not source_text.strip() or len(source_text) > 200_000:
            raise ValidationError("资料解析正文无效")
        if any(not value or value != value.strip() or len(value) > 128 for value in (model, extracted_by)):
            raise ValidationError("资料模型来源标识无效")
        try:
            parse_warnings = require_parse_warnings(parse_warnings)
            analysis = KnowledgeAnalysis.model_validate(analysis.model_dump())
            analysis.validate_evidence(source_text, image_count=image_count)
            if source_kind not in {"document_text", "vision_transcription"} or (source_kind == "document_text" and image_count != 0) or (source_kind == "vision_transcription" and image_count < 1) or type(image_source_pages) is not tuple or len(image_source_pages) != image_count or any(type(page) is not int or page < 1 for page in image_source_pages) or tuple(sorted(set(image_source_pages))) != image_source_pages:
                raise ValueError("资料来源形态无效")
            require_safe_text(model)
            require_safe_text(extracted_by)
        except ValueError:
            raise ValidationError("资料分析未通过证据校验") from None
        async with self._uow_factory(tenant_id) as uow:
            repo = uow.knowledge
            await repo.lock()
            job, document = await self._claim(repo, tenant_id, claim)
            now = self._now()
            provenance = tuple(ProvenanceSummary(source_type=SourceType.UPLOAD, source_id=str(document.source.artifact_id), extracted_by=extracted_by, extracted_at=now, confirmed_by=None, confirmed_at=None) for _ in analysis.facts)
            revision = KnowledgeRevisionView(revision_id=new_id("krev"), document_id=document.document_id, job_id=job.job_id, source_text=source_text, analysis=analysis, model=model, extracted_by=extracted_by, extracted_at=now, fact_provenance=provenance, source_kind=source_kind, image_count=image_count, image_source_pages=image_source_pages, parse_warnings=parse_warnings)
            await repo.save_revision(revision)
            await repo.save_job(job.model_copy(update={"state": "complete"}))
            document = self._changed(document, status="awaiting_confirmation", current_revision_id=revision.revision_id, export_state="pending", failure_reason=None)
            await repo.save_document(document)
            return KnowledgeDocumentDetail(document=document, revision=revision)

    async def fail_processing(self, tenant_id: TenantId, claim: KnowledgeClaim, *, reason: str, uncertain: bool) -> KnowledgeDocumentView:
        if reason not in _SAFE_FAILURES:
            reason = "model_result_unknown" if uncertain else "processing_failed"
        async with self._uow_factory(tenant_id) as uow:
            repo = uow.knowledge
            await repo.lock()
            job, document = await self._claim(repo, tenant_id, claim, authorize=False)
            state = "unknown" if uncertain else "failed"
            await repo.save_job(job.model_copy(update={"state": state}))
            document = self._changed(document, status=state, failure_reason=reason)
            await repo.save_document(document)
            return document

    async def retry_processing(self, tenant_id: TenantId, actor: KnowledgeActor, document_id: str, *, expected_version: int, acknowledge_unknown: bool = False) -> KnowledgeDocumentView:
        async with self._uow_factory(tenant_id) as uow:
            repo = uow.knowledge
            await repo.lock()
            await self._expire(repo)
            role, user_id = await self._authorize(repo, tenant_id, actor)
            document = await self._document(repo, document_id)
            if not _permissions(document, role, actor).can_retry:
                raise PermissionDenied("无权重试这份资料")
            if document.version != expected_version:
                raise InvalidStateTransition("资料版本已变化")
            if document.status == "unknown" and acknowledge_unknown is not True:
                raise ValidationError("须确认未知结果重试可能再次消耗模型额度")
            previous = await repo.get_job(document.job_id)
            assert previous is not None
            job = KnowledgeJob(tenant_id=tenant_id, job_id=new_id("run"), document_id=document_id, actor_id=actor.employee_id, user_id=user_id, state="queued", attempt=previous.attempt + 1, created_at=self._now())
            await repo.save_job(job)
            document = self._changed(document, status="queued", job_id=job.job_id, failure_reason=None)
            await repo.save_document(document)
            return _permissions(document, role, actor)

    async def confirm(self, tenant_id: TenantId, actor: KnowledgeActor, document_id: str, *, revision_id: str, expected_version: int) -> KnowledgeDocumentDetail:
        async with self._uow_factory(tenant_id) as uow:
            repo = uow.knowledge
            await repo.lock()
            role, _ = await self._authorize(repo, tenant_id, actor, boss=True)
            document = await self._document(repo, document_id)
            if document.version != expected_version or document.current_revision_id != revision_id or document.status != "awaiting_confirmation":
                raise InvalidStateTransition("待确认资料版本已变化")
            revision = await repo.get_revision(revision_id)
            if revision is None or revision.confirmed_by is not None:
                raise InvalidStateTransition("资料确认版本无效")
            now = self._now()
            revision = revision.model_copy(update={"confirmed_by": actor.employee_id, "confirmed_at": now, "fact_provenance": tuple(p.model_copy(update={"confirmed_by": actor.employee_id, "confirmed_at": now}) for p in revision.fact_provenance)})
            await repo.save_revision(revision)
            document = self._changed(document, status="confirmed", export_state="pending")
            await repo.save_document(document)
            return KnowledgeDocumentDetail(document=_permissions(document, role, actor), revision=revision)

    async def request_sync(self, tenant_id: TenantId, actor: KnowledgeActor, document_id: str, *, revision_id: str, expected_version: int) -> KnowledgeDocumentView:
        async with self._uow_factory(tenant_id) as uow:
            repo = uow.knowledge
            await repo.lock()
            role, _ = await self._authorize(repo, tenant_id, actor)
            document = await self._document(repo, document_id)
            if document.version != expected_version or document.current_revision_id != revision_id or not revision_id:
                raise InvalidStateTransition("资料同步版本已变化")
            document = document.model_copy(update={"export_state": "pending", "updated_at": self._now()})
            await repo.save_document(document)
            return _permissions(document, role, actor)

    async def list_pending_exports(self, tenant_id: TenantId, *, limit: int = 10) -> tuple[KnowledgeDocumentDetail, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValidationError("资料同步批次无效")
        async with self._uow_factory(tenant_id) as uow:
            documents = await uow.knowledge.pending_exports(limit)
            return tuple([await self._detail(uow.knowledge, item) for item in documents])

    async def mark_export(self, tenant_id: TenantId, document_id: str, *, revision_id: str, expected_version: int, state: KnowledgeExportState) -> KnowledgeDocumentView:
        if state not in {"synced", "failed"}:
            raise ValidationError("资料同步状态无效")
        async with self._uow_factory(tenant_id) as uow:
            repo = uow.knowledge
            await repo.lock()
            document = await self._document(repo, document_id)
            if document.version != expected_version or document.current_revision_id != revision_id:
                raise InvalidStateTransition("资料同步版本已变化")
            document = document.model_copy(update={"export_state": state, "updated_at": self._now()})
            await repo.save_document(document)
            return document
