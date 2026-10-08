"""企业共享资料状态机：隔离、证据、权限、幂等与未知结果不重放。"""
from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from domains.products.knowledge_schemas import (
    KnowledgeActor,
    KnowledgeAnalysis,
    KnowledgeFact,
    KnowledgeInference,
    KnowledgeSource,
)
from domains.products.knowledge_service import EnterpriseKnowledgeServiceImpl
from shared.errors import (
    IdempotencyConflict,
    InvalidStateTransition,
    PermissionDenied,
    ValidationError,
)
from shared.schemas.identifiers import ArtifactId, EmployeeId, TenantId

TENANT = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0X")
OTHER = TenantId("tn_01K39P9M5D6K4A91YEQ80EJZ0Y")
BOSS = KnowledgeActor(tenant_id=TENANT, employee_id=EmployeeId("emp_boss"))
SALES = KnowledgeActor(tenant_id=TENANT, employee_id=EmployeeId("emp_sales"))
SOURCE = KnowledgeSource(filename="catalog.md", mime_type="text/markdown", size_bytes=25, sha256="a" * 64, artifact_id=ArtifactId("art_01K39P9M5D6K4A91YEQ80EJZ0X"))
TEXT = "Material: stainless steel. MOQ: 500."
ANALYSIS = KnowledgeAnalysis(title="Material", facts=(KnowledgeFact(label="Material", value="stainless steel", source_quote="Material: stainless steel."),), inferences=(KnowledgeInference(text="May suit humid environments.", evidence_quotes=("Material: stainless steel.",)),))


class MemoryRepo:
    def __init__(self):
        self.roles = {BOSS.employee_id: ("boss", "usr_boss"), SALES.employee_id: ("sales", "usr_sales")}
        self.docs, self.jobs, self.revisions, self.requests = {}, {}, {}, {}
        self.source_valid = True

    async def lock(self): pass
    async def employee_identity(self, employee_id): return self.roles.get(employee_id)
    async def source_matches(self, document): return self.source_valid
    async def get_document(self, document_id): return self.docs.get(document_id)
    async def get_by_artifact(self, artifact_id): return next((d for d in self.docs.values() if d.source.artifact_id == artifact_id), None)
    async def save_document(self, document): self.docs[document.document_id] = document
    async def get_request(self, actor_id, key_hash): return self.requests.get((actor_id, key_hash))
    async def save_request(self, actor_id, key_hash, request_hash, document_id): self.requests[actor_id, key_hash] = (request_hash, document_id)
    async def get_job(self, job_id): return self.jobs.get(job_id)
    async def save_job(self, job): self.jobs[job.job_id] = job
    async def claimable_job(self): return next((j for j in self.jobs.values() if j.state == "queued"), None)
    async def expired_jobs(self, now): return tuple(j for j in self.jobs.values() if j.state == "processing" and j.lease_until <= now)
    async def get_revision(self, revision_id): return self.revisions.get(revision_id)
    async def save_revision(self, revision): self.revisions[revision.revision_id] = revision
    async def list_documents(self, query, limit, offset):
        rows = tuple(d for d in self.docs.values() if not query or query in d.source.filename)
        return rows[offset:offset + limit], len(rows)
    async def pending_exports(self, limit):
        return tuple(d for d in self.docs.values() if d.export_state == "pending" and d.current_revision_id)[:limit]


class MemoryUow:
    def __init__(self, repo): self.knowledge = repo
    async def __aenter__(self):
        self.before = copy.deepcopy(vars(self.knowledge))
        return self
    async def __aexit__(self, exc_type, exc, traceback):
        if exc_type:
            self.knowledge.__dict__.update(self.before)


@pytest.fixture
def setup():
    repositories = {TENANT: MemoryRepo(), OTHER: MemoryRepo()}
    clock = [datetime(2026, 10, 8, tzinfo=UTC)]
    service = EnterpriseKnowledgeServiceImpl(lambda tenant: MemoryUow(repositories[tenant]), now=lambda: clock[0])
    return service, repositories, clock


async def uploaded(service):
    return await service.register_upload(TENANT, SALES, SOURCE, idempotency_key="upload-one")


async def processed(service):
    document = await uploaded(service)
    claim = await service.claim_next(TENANT, lease_owner="test")
    result = await service.complete_processing(TENANT, claim, source_text=TEXT, analysis=ANALYSIS, model="synthetic-model-v1", extracted_by="synthetic-model-v1")
    return document, claim, result


@pytest.mark.asyncio
async def test_upload_is_shared_and_idempotent_without_duplicate_jobs(setup):
    service, repos, _ = setup
    document = await uploaded(service)
    assert (await uploaded(service)).document_id == document.document_id
    boss_upload = await service.register_upload(TENANT, BOSS, SOURCE, idempotency_key="boss-same")
    assert boss_upload.document_id == document.document_id
    assert len(repos[TENANT].jobs) == 1
    assert (await service.get_detail(TENANT, BOSS, document.document_id)).document.source == SOURCE
    with pytest.raises(IdempotencyConflict):
        await service.register_upload(TENANT, SALES, SOURCE.model_copy(update={"filename": "different.md"}), idempotency_key="upload-one")
    with pytest.raises(PermissionDenied):
        await service.list_documents(OTHER, BOSS)
    other_actor = KnowledgeActor(tenant_id=OTHER, employee_id=BOSS.employee_id)
    assert (await service.list_documents(OTHER, other_actor)).total == 0


@pytest.mark.asyncio
async def test_source_mismatch_rolls_back_document_and_queue(setup):
    service, repos, _ = setup
    repos[TENANT].source_valid = False
    with pytest.raises(ValidationError):
        await uploaded(service)
    assert not repos[TENANT].docs and not repos[TENANT].jobs


@pytest.mark.asyncio
async def test_confirm_requires_current_boss_and_preserves_model_analysis(setup):
    service, repos, _ = setup
    _, _, result = await processed(service)
    doc, revision = result.document, result.revision
    assert doc.status == "awaiting_confirmation"
    assert revision.confirmed_by is None
    with pytest.raises(PermissionDenied):
        await service.confirm(TENANT, SALES, doc.document_id, revision_id=revision.revision_id, expected_version=doc.version)
    repos[TENANT].roles[BOSS.employee_id] = ("sales", "usr_boss")
    with pytest.raises(PermissionDenied):
        await service.confirm(TENANT, BOSS, doc.document_id, revision_id=revision.revision_id, expected_version=doc.version)
    repos[TENANT].roles[BOSS.employee_id] = ("boss", "usr_boss")
    confirmed = await service.confirm(TENANT, BOSS, doc.document_id, revision_id=revision.revision_id, expected_version=doc.version)
    assert confirmed.document.status == "confirmed"
    assert confirmed.revision.analysis == revision.analysis
    assert confirmed.revision.source_text == TEXT
    assert confirmed.revision.confirmed_by == BOSS.employee_id
    assert all(p.confirmed_by == BOSS.employee_id for p in confirmed.revision.fact_provenance)
    assert not confirmed.document.can_confirm
    with pytest.raises(InvalidStateTransition):
        await service.confirm(TENANT, BOSS, doc.document_id, revision_id=revision.revision_id, expected_version=doc.version)


@pytest.mark.asyncio
async def test_lease_expiry_becomes_unknown_and_never_requeues_automatically(setup):
    service, repos, clock = setup
    document = await uploaded(service)
    claim = await service.claim_next(TENANT, lease_owner="first", lease_seconds=30)
    assert await service.claim_next(TENANT, lease_owner="second") is None
    clock[0] += timedelta(seconds=31)
    assert await service.claim_next(TENANT, lease_owner="second") is None
    detail = await service.get_detail(TENANT, BOSS, document.document_id)
    assert detail.document.status == "unknown"
    assert detail.document.failure_reason == "lease_expired"
    with pytest.raises(InvalidStateTransition):
        await service.complete_processing(TENANT, claim, source_text=TEXT, analysis=ANALYSIS, model="synthetic-v1", extracted_by="synthetic-v1")
    with pytest.raises(ValidationError):
        await service.retry_processing(TENANT, BOSS, document.document_id, expected_version=detail.document.version)
    retried = await service.retry_processing(TENANT, BOSS, document.document_id, expected_version=detail.document.version, acknowledge_unknown=True)
    assert retried.job_id != claim.job_id
    assert repos[TENANT].jobs[claim.job_id].state == "unknown"


@pytest.mark.asyncio
async def test_revoked_uploader_cannot_start_or_commit_processing(setup):
    service, repos, _ = setup
    document = await uploaded(service)
    claim = await service.claim_next(TENANT, lease_owner="test")
    del repos[TENANT].roles[SALES.employee_id]
    with pytest.raises(PermissionDenied):
        await service.authorize_processing(TENANT, claim)
    with pytest.raises(PermissionDenied):
        await service.complete_processing(TENANT, claim, source_text=TEXT, analysis=ANALYSIS, model="synthetic-v1", extracted_by="synthetic-v1")
    assert not repos[TENANT].revisions
    assert repos[TENANT].docs[document.document_id].status == "processing"


@pytest.mark.asyncio
async def test_export_is_independent_and_stale_version_cannot_mark_new_projection(setup):
    service, repos, _ = setup
    _, _, result = await processed(service)
    before = result.document
    confirmed = await service.confirm(TENANT, BOSS, before.document_id, revision_id=before.current_revision_id, expected_version=before.version)
    with pytest.raises(InvalidStateTransition):
        await service.mark_export(TENANT, before.document_id, revision_id=before.current_revision_id, expected_version=before.version, state="synced")
    failed = await service.mark_export(TENANT, before.document_id, revision_id=before.current_revision_id, expected_version=confirmed.document.version, state="failed")
    await service.request_sync(TENANT, SALES, before.document_id, revision_id=before.current_revision_id, expected_version=failed.version)
    assert len(repos[TENANT].jobs) == 1
    assert len(await service.list_pending_exports(TENANT)) == 1


@pytest.mark.parametrize("analysis", [
    {"title": "Spec", "facts": [{"label": "Material", "value": "brass", "source_quote": "Material: stainless steel."}], "inferences": []},
    {"title": "Spec", "facts": [{"label": "Material", "value": "stainless steel", "source_quote": "Material: stainless steel."}], "inferences": [{"text": "Cheap", "evidence_quotes": ["Invented quote"]}]},
])
def test_unsupported_facts_and_inference_evidence_rejected(analysis):
    with pytest.raises(ValueError):
        KnowledgeAnalysis.model_validate(analysis).validate_evidence(TEXT)


def test_unknown_fields_and_sensitive_input_rejected():
    with pytest.raises(ValueError):
        KnowledgeAnalysis.model_validate({**ANALYSIS.model_dump(), "confidence": "high"})
    with pytest.raises(ValueError):
        ANALYSIS.validate_evidence(TEXT + "\n" + "sk-" + "a" * 30)



def test_visual_only_inference_requires_actual_trusted_pages():
    analysis = KnowledgeAnalysis(title="Visual observation", facts=(), inferences=(KnowledgeInference(text="Possible valve shape", image_pages=(1,)),))
    analysis.validate_evidence("No readable text.", image_count=1)
    with pytest.raises(ValueError):
        analysis.validate_evidence("No readable text.", image_count=0)
    with pytest.raises(ValueError):
        KnowledgeInference(text="Invalid", image_pages=(True,))
    with pytest.raises(ValueError):
        KnowledgeInference(text="Invalid", image_pages=(1, 1))
    with pytest.raises(ValueError):
        KnowledgeAnalysis(title="Empty", facts=(), inferences=())


@pytest.mark.asyncio
async def test_visual_source_kind_and_projection_version_are_preserved(setup):
    service, _, _ = setup
    document = await uploaded(service)
    claim = await service.claim_next(TENANT, lease_owner="visual")
    analysis = KnowledgeAnalysis(title="Visual", facts=(), inferences=(KnowledgeInference(text="Possible valve", image_pages=(1,)),))
    detail = await service.complete_processing(TENANT, claim, source_text="No readable text.", analysis=analysis, model="synthetic-vision-v1", extracted_by="synthetic-vision-v1", source_kind="vision_transcription", image_count=1, image_source_pages=(3,))
    assert detail.revision.source_kind == "vision_transcription"
    assert detail.revision.image_count == 1
    assert detail.revision.image_source_pages == (3,)
    synced = await service.mark_export(TENANT, document.document_id, revision_id=detail.revision.revision_id, expected_version=detail.document.version, state="synced")
    assert synced.version == detail.document.version
    resync = await service.request_sync(TENANT, SALES, document.document_id, revision_id=detail.revision.revision_id, expected_version=synced.version)
    assert resync.version == synced.version


def test_schema_errors_do_not_echo_source_values():
    marker = "SENSITIVE_SOURCE_MATERIAL"
    with pytest.raises(ValueError) as error:
        KnowledgeAnalysis.model_validate({"title": "Test", "facts": [{"label": marker}], "inferences": []})
    assert marker not in str(error.value)


@pytest.mark.asyncio
async def test_parser_warnings_are_not_fact_evidence_and_remain_separate(setup):
    service, _, _ = setup
    await uploaded(service)
    claim = await service.claim_next(TENANT, lease_owner="test")
    warning = "文件包含图片，请对照原件核对。"
    fabricated = KnowledgeAnalysis(
        title="Invalid parser-derived fact",
        facts=(KnowledgeFact(label="Warning", value="包含图片", source_quote=warning),),
        inferences=(),
    )
    with pytest.raises(ValidationError):
        await service.complete_processing(
            TENANT, claim, source_text=TEXT, analysis=fabricated,
            model="synthetic", extracted_by="synthetic", parse_warnings=(warning,),
        )
    result = await service.complete_processing(
        TENANT, claim, source_text=TEXT, analysis=ANALYSIS,
        model="synthetic", extracted_by="synthetic", parse_warnings=(warning,),
    )
    assert result.revision.parse_warnings == (warning,)
    assert result.revision.source_text == TEXT
    confirmed = await service.confirm(
        TENANT, BOSS, result.document.document_id,
        revision_id=result.revision.revision_id, expected_version=result.document.version,
    )
    assert confirmed.revision.parse_warnings == (warning,)


@pytest.mark.asyncio
@pytest.mark.parametrize("warnings", [
    ("x" * 513,), ("warning",) * 5, (" ",),
    ("password=" + "synthetic-secret",),
])
async def test_parser_warnings_reject_unbounded_or_sensitive_content(setup, warnings):
    service, repos, _ = setup
    await uploaded(service)
    claim = await service.claim_next(TENANT, lease_owner="test")
    with pytest.raises(ValidationError, match="证据校验"):
        await service.complete_processing(
            TENANT, claim, source_text=TEXT, analysis=ANALYSIS,
            model="synthetic", extracted_by="synthetic", parse_warnings=warnings,
        )
    assert not repos[TENANT].revisions


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [False, True])
async def test_persisted_revision_restores_strict_provenance_for_read_and_confirmation(setup, confirmed):
    from infra.db.enterprise_knowledge import PostgresKnowledgeRepository

    service, _, _ = setup
    _, _, draft = await processed(service)
    detail = await service.confirm(
        TENANT, BOSS, draft.document.document_id,
        revision_id=draft.revision.revision_id, expected_version=draft.document.version,
    ) if confirmed else draft
    revision = detail.revision
    payload = revision.model_dump(mode="json")

    class StoredRows:
        def one_or_none(self):
            return SimpleNamespace(payload=payload)

    class Session:
        async def scalars(self, statement):
            return StoredRows()

    repository = PostgresKnowledgeRepository(Session(), TENANT)
    restored = await repository.get_revision(revision.revision_id)
    assert restored == revision
    assert restored.fact_provenance[0].extracted_at.tzinfo is not None
    assert restored.fact_provenance[0].confirmed_at == revision.confirmed_at

    from pydantic import ValidationError as PydanticValidationError

    payload["fact_provenance"][0]["extracted_at"] = "2026-10-08T00:00:00"
    with pytest.raises(PydanticValidationError):
        await repository.get_revision(revision.revision_id)
