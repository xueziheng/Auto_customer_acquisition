"""真实企业角色下验证资料隔离、原子队列和确认，不自动启动Docker。"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from domains.products.schemas import (
    KnowledgeActor,
    KnowledgeAnalysis,
    KnowledgeFact,
    KnowledgeSource,
)
from domains.products.service import EnterpriseKnowledgeServiceImpl
from infra.db.enterprise_knowledge import SqlAlchemyKnowledgeUnitOfWork
from shared.errors import IdempotencyConflict, PermissionDenied, ValidationError
from shared.schemas.identifiers import ArtifactId, EmployeeId, TenantId, new_id
from tests.integration.test_tenant_row_security import (
    IsolationDatabase,
    _engine,
    _migrate,
    isolated_database,
    prepared_isolation_url,
)

__all__ = ("isolated_database", "prepared_isolation_url")

_TABLES = ("enterprise_knowledge_requests", "enterprise_knowledge_revisions", "enterprise_knowledge_jobs", "enterprise_knowledge_documents")


@pytest.mark.asyncio
async def test_real_roles_share_within_company_and_reject_other_company(
    isolated_database: IsolationDatabase,
) -> None:
    db = isolated_database
    tenant = TenantId(db.tenants[0])
    actor = KnowledgeActor(tenant_id=tenant, employee_id=EmployeeId(db.employees[0]))
    source = KnowledgeSource(filename="spec.md", mime_type="text/markdown", size_bytes=20, sha256="b" * 64, artifact_id=ArtifactId(new_id("art")))
    factory = async_sessionmaker(db.apps[0], expire_on_commit=False)
    service = EnterpriseKnowledgeServiceImpl(lambda t: SqlAlchemyKnowledgeUnitOfWork(factory, t))
    try:
        async with db.apps[0].begin() as conn:
            await conn.execute(text("INSERT INTO raw_artifacts (tenant_id,artifact_id,kind,content_hash,size_bytes,mime_type,uploaded_by,created_at,object_key) VALUES (:tenant,:artifact,'text',:hash,20,'text/markdown',NULL,now(),:key)"), {"tenant": tenant, "artifact": source.artifact_id, "hash": source.sha256, "key": "raw/" + tenant + "/" + source.artifact_id})
        first, duplicate = await asyncio.gather(
            service.register_upload(tenant, actor, source, idempotency_key="same-upload"),
            service.register_upload(tenant, actor, source, idempotency_key="same-upload"),
        )
        assert first.document_id == duplicate.document_id
        with pytest.raises(IdempotencyConflict):
            await service.register_upload(tenant, actor, source.model_copy(update={"filename": "other.md"}), idempotency_key="same-upload")
        async with db.apps[1].connect() as conn:
            assert await conn.scalar(text("SELECT count(*) FROM enterprise_knowledge_documents")) == 0
            assert await conn.scalar(text("SELECT count(*) FROM enterprise_knowledge_jobs")) == 0
        foreign_factory = async_sessionmaker(db.apps[1], expire_on_commit=False)
        foreign_service = EnterpriseKnowledgeServiceImpl(lambda t: SqlAlchemyKnowledgeUnitOfWork(foreign_factory, t))
        other_actor = KnowledgeActor(tenant_id=TenantId(db.tenants[1]), employee_id=EmployeeId(db.employees[1]))
        with pytest.raises(ValidationError):
            await foreign_service.get_detail(other_actor.tenant_id, other_actor, first.document_id)
        claims = await asyncio.gather(
            service.claim_next(tenant, lease_owner="first"),
            service.claim_next(tenant, lease_owner="second"),
        )
        assert sum(item is not None for item in claims) == 1
        claim = next(item for item in claims if item is not None)
        draft = await service.complete_processing(tenant, claim, source_text="Material: steel.", analysis=KnowledgeAnalysis(title="Steel", facts=(KnowledgeFact(label="Material", value="steel", source_quote="Material: steel."),), inferences=()), model="synthetic-v1", extracted_by="synthetic-v1")
        assert draft.document.status == "awaiting_confirmation"
        with pytest.raises(PermissionDenied):
            await service.confirm(tenant, actor, first.document_id, revision_id=draft.revision.revision_id, expected_version=draft.document.version)
        async with db.apps[0].begin() as conn:
            await conn.execute(text("UPDATE employees SET role='boss' WHERE tenant_id=:tenant AND employee_id=:employee"), {"tenant": tenant, "employee": actor.employee_id})
        confirmed = await service.confirm(tenant, actor, first.document_id, revision_id=draft.revision.revision_id, expected_version=draft.document.version)
        assert confirmed.revision.analysis == draft.revision.analysis
        assert confirmed.revision.confirmed_by == actor.employee_id
        assert (await service.list_documents(tenant, actor, query="steel")).total == 1
        async with db.apps[0].connect() as conn:
            with pytest.raises(DBAPIError):
                async with conn.begin():
                    await conn.execute(text("UPDATE enterprise_knowledge_revisions SET payload=jsonb_set(payload,'{analysis,title}','\"changed\"') WHERE tenant_id=:tenant AND revision_id=:revision"), {"tenant": tenant, "revision": draft.revision.revision_id})
        async with db.apps[0].connect() as conn:
            with pytest.raises(DBAPIError):
                async with conn.begin():
                    await conn.execute(text("UPDATE enterprise_knowledge_documents SET filename='changed.md' WHERE tenant_id=:tenant AND document_id=:document"), {"tenant": tenant, "document": first.document_id})
    finally:
        async with db.admin.begin() as conn:
            for table in _TABLES:
                await conn.execute(text("DELETE FROM " + table + " WHERE tenant_id=:tenant"), {"tenant": tenant})
            await conn.execute(text("DELETE FROM raw_artifacts WHERE tenant_id=:tenant AND artifact_id=:artifact"), {"tenant": tenant, "artifact": source.artifact_id})


@pytest.mark.asyncio
async def test_0072_roundtrip_restores_tables_rls_and_text_mime(prepared_isolation_url) -> None:
    url = prepared_isolation_url
    engine = _engine(url)
    try:
        await _migrate(url, "downgrade", "0071")
        async with engine.connect() as conn:
            assert await conn.scalar(text("SELECT to_regclass('public.enterprise_knowledge_documents')")) is None
            old = await conn.scalar(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_raw_artifacts_kind_mime'"))
            assert "markdown" not in old
        await _migrate(url, "upgrade", "head")
        async with engine.connect() as conn:
            rows = (await conn.execute(text("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname IN ('enterprise_knowledge_documents','enterprise_knowledge_jobs','enterprise_knowledge_revisions','enterprise_knowledge_requests')"))).all()
            assert len(rows) == 4 and all(row[1] and row[2] for row in rows)
            current = await conn.scalar(text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname='ck_raw_artifacts_kind_mime'"))
            assert "markdown" in current
    finally:
        await engine.dispose()
        await _migrate(url, "upgrade", "head")
