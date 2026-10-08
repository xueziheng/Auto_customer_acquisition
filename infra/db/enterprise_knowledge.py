"""企业资料的 PostgreSQL 事务适配，所有SQL显式绑定企业。"""
from __future__ import annotations

from datetime import datetime
from types import TracebackType
from typing import Self

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from domains.products.knowledge_repository import KnowledgeRepository
from domains.products.knowledge_schemas import (
    KnowledgeDocumentView,
    KnowledgeJob,
    KnowledgeRevisionView,
)
from infra.db.tables import (
    EmployeeRow,
    RawArtifactRow,
)
from infra.db.tables import (
    EnterpriseKnowledgeDocumentRow as DocumentRow,
)
from infra.db.tables import (
    EnterpriseKnowledgeJobRow as JobRow,
)
from infra.db.tables import (
    EnterpriseKnowledgeRequestRow as RequestRow,
)
from infra.db.tables import (
    EnterpriseKnowledgeRevisionRow as RevisionRow,
)
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import EmployeeId, TenantId


class PostgresKnowledgeRepository:
    """领域服务负责状态规则；适配器只执行有界、tenant-bound 的存取。"""
    def __init__(self, session: AsyncSession, tenant_id: TenantId) -> None:
        self._session, self._tenant = session, tenant_id

    async def lock(self) -> None:
        await self._session.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:scope, 0))"), {"scope": "enterprise-knowledge:" + self._tenant})

    async def employee_identity(self, employee_id: EmployeeId) -> tuple[str, str] | None:
        row = (await self._session.execute(select(EmployeeRow.role, EmployeeRow.user_id).where(EmployeeRow.tenant_id == self._tenant, EmployeeRow.employee_id == employee_id, EmployeeRow.is_active.is_(True)).with_for_update(read=True))).one_or_none()
        return (row[0], row[1]) if row is not None and row[1] else None

    async def source_matches(self, document: KnowledgeDocumentView) -> bool:
        source = document.source
        row = (await self._session.scalars(select(RawArtifactRow).where(RawArtifactRow.tenant_id == self._tenant, RawArtifactRow.artifact_id == source.artifact_id))).one_or_none()
        return row is not None and row.content_hash == source.sha256 and row.size_bytes == source.size_bytes and row.mime_type == source.mime_type

    async def get_document(self, document_id: str) -> KnowledgeDocumentView | None:
        row = (await self._session.scalars(select(DocumentRow).where(DocumentRow.tenant_id == self._tenant, DocumentRow.document_id == document_id))).one_or_none()
        return KnowledgeDocumentView.model_validate(row.payload) if row else None

    async def get_by_artifact(self, artifact_id: str) -> KnowledgeDocumentView | None:
        row = (await self._session.scalars(select(DocumentRow).where(DocumentRow.tenant_id == self._tenant, DocumentRow.artifact_id == artifact_id))).one_or_none()
        return KnowledgeDocumentView.model_validate(row.payload) if row else None

    async def save_document(self, document: KnowledgeDocumentView) -> None:
        if document.tenant_id != self._tenant:
            raise TenantIsolationViolation("企业资料租户不匹配")
        row = (await self._session.scalars(select(DocumentRow).where(DocumentRow.tenant_id == self._tenant, DocumentRow.document_id == document.document_id))).one_or_none()
        values = {"artifact_id": document.source.artifact_id, "uploader_id": document.uploader_id, "status": document.status, "version": document.version, "current_revision_id": document.current_revision_id, "export_state": document.export_state, "filename": document.source.filename, "payload": document.model_dump(mode="json"), "created_at": document.created_at}
        if row is None:
            self._session.add(DocumentRow(tenant_id=self._tenant, document_id=document.document_id, **values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
        await self._session.flush()

    async def get_request(self, actor_id: EmployeeId, key_hash: str) -> tuple[str, str] | None:
        row = (await self._session.execute(select(RequestRow.request_hash, RequestRow.document_id).where(RequestRow.tenant_id == self._tenant, RequestRow.actor_id == actor_id, RequestRow.key_hash == key_hash))).one_or_none()
        return (row[0], row[1]) if row else None

    async def save_request(self, actor_id: EmployeeId, key_hash: str, request_hash: str, document_id: str) -> None:
        self._session.add(RequestRow(tenant_id=self._tenant, actor_id=actor_id, key_hash=key_hash, request_hash=request_hash, document_id=document_id))
        await self._session.flush()

    async def get_job(self, job_id: str) -> KnowledgeJob | None:
        row = (await self._session.scalars(select(JobRow).where(JobRow.tenant_id == self._tenant, JobRow.job_id == job_id))).one_or_none()
        return KnowledgeJob.model_validate(row.payload) if row else None

    async def save_job(self, job: KnowledgeJob) -> None:
        if job.tenant_id != self._tenant:
            raise TenantIsolationViolation("企业资料租户不匹配")
        row = (await self._session.scalars(select(JobRow).where(JobRow.tenant_id == self._tenant, JobRow.job_id == job.job_id))).one_or_none()
        values = {"document_id": job.document_id, "state": job.state, "lease_until": job.lease_until, "payload": job.model_dump(mode="json"), "created_at": job.created_at}
        if row is None:
            self._session.add(JobRow(tenant_id=self._tenant, job_id=job.job_id, **values))
        else:
            for key, value in values.items():
                setattr(row, key, value)
        await self._session.flush()

    async def claimable_job(self) -> KnowledgeJob | None:
        row = (await self._session.scalars(select(JobRow).where(JobRow.tenant_id == self._tenant, JobRow.state == "queued").order_by(JobRow.created_at, JobRow.job_id).limit(1).with_for_update(skip_locked=True))).one_or_none()
        return KnowledgeJob.model_validate(row.payload) if row else None

    async def expired_jobs(self, now: datetime) -> tuple[KnowledgeJob, ...]:
        rows = (await self._session.scalars(select(JobRow).where(JobRow.tenant_id == self._tenant, JobRow.state == "processing", JobRow.lease_until <= now).order_by(JobRow.created_at).limit(100).with_for_update())).all()
        return tuple(KnowledgeJob.model_validate(row.payload) for row in rows)

    async def get_revision(self, revision_id: str) -> KnowledgeRevisionView | None:
        row = (await self._session.scalars(select(RevisionRow).where(RevisionRow.tenant_id == self._tenant, RevisionRow.revision_id == revision_id))).one_or_none()
        return KnowledgeRevisionView.model_validate(row.payload) if row else None

    async def save_revision(self, revision: KnowledgeRevisionView) -> None:
        row = (await self._session.scalars(select(RevisionRow).where(RevisionRow.tenant_id == self._tenant, RevisionRow.revision_id == revision.revision_id))).one_or_none()
        if row is None:
            self._session.add(RevisionRow(tenant_id=self._tenant, revision_id=revision.revision_id, document_id=revision.document_id, job_id=revision.job_id, payload=revision.model_dump(mode="json")))
        else:
            row.payload = revision.model_dump(mode="json")
        await self._session.flush()

    async def list_documents(self, query: str, limit: int, offset: int) -> tuple[tuple[KnowledgeDocumentView, ...], int]:
        statement = select(DocumentRow).where(DocumentRow.tenant_id == self._tenant)
        if query:
            match = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            revisions = select(RevisionRow.document_id).where(RevisionRow.tenant_id == self._tenant, RevisionRow.payload["source_text"].astext.ilike(match, escape="\\"))
            statement = statement.where(or_(DocumentRow.filename.ilike(match, escape="\\"), DocumentRow.document_id.in_(revisions)))
        total = await self._session.scalar(select(func.count()).select_from(statement.subquery()))
        rows = (await self._session.scalars(statement.order_by(DocumentRow.created_at.desc(), DocumentRow.document_id).limit(limit).offset(offset))).all()
        return tuple(KnowledgeDocumentView.model_validate(row.payload) for row in rows), int(total or 0)

    async def pending_exports(self, limit: int) -> tuple[KnowledgeDocumentView, ...]:
        rows = (await self._session.scalars(select(DocumentRow).where(DocumentRow.tenant_id == self._tenant, DocumentRow.export_state == "pending", DocumentRow.current_revision_id.is_not(None), DocumentRow.status.in_(("awaiting_confirmation", "confirmed"))).order_by(DocumentRow.created_at, DocumentRow.document_id).limit(limit))).all()
        return tuple(KnowledgeDocumentView.model_validate(row.payload) for row in rows)


class SqlAlchemyKnowledgeUnitOfWork:
    """提交原件引用与处理任务为同一事务；异常一并回滚。"""
    def __init__(self, factory: async_sessionmaker[AsyncSession], tenant_id: TenantId) -> None:
        self._factory, self._tenant = factory, tenant_id
        self.knowledge: KnowledgeRepository

    async def __aenter__(self) -> Self:
        self._session = self._factory()
        await self._session.begin()
        self.knowledge = PostgresKnowledgeRepository(self._session, self._tenant)
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: TracebackType | None) -> None:
        try:
            if exc_type is None:
                await self._session.commit()
            else:
                await self._session.rollback()
        finally:
            await self._session.close()
