"""仅管理员可重整未确认表格，保留旧修订、版本锁和已确认边界。"""
import pytest

from shared.errors import InvalidStateTransition, PermissionDenied
from tests.unit.test_enterprise_knowledge import (
    ANALYSIS,
    BOSS,
    SALES,
    SOURCE,
    TENANT,
    TEXT,
)
from tests.unit.test_enterprise_knowledge import (
    setup as setup,  # noqa: PLC0414 - pytest fixture re-export
)
from workflows.enterprise_knowledge.xlsx_rows import XLSX_MIME


async def ready(service):
    source = SOURCE.model_copy(update={"filename": "quote.xlsx", "mime_type": XLSX_MIME})
    doc = await service.register_upload(TENANT, SALES, source, idempotency_key="xlsx")
    claim = await service.claim_next(TENANT, lease_owner="worker")
    result = await service.complete_processing(TENANT, claim, source_text=TEXT, analysis=ANALYSIS, model="synthetic", extracted_by="test")
    return doc, result


async def test_boss_reprocess_preserves_revision_and_rejects_duplicate_or_sales(setup):
    service, repos, _ = setup
    doc, result = await ready(service)
    version = result.document.version
    with pytest.raises(PermissionDenied):
        await service.retry_processing(TENANT, SALES, doc.document_id, expected_version=version)
    queued = await service.retry_processing(TENANT, BOSS, doc.document_id, expected_version=version)
    assert queued.status == "queued"
    assert result.revision.revision_id in repos[TENANT].revisions
    assert queued.current_revision_id == result.revision.revision_id
    with pytest.raises((PermissionDenied, InvalidStateTransition)):
        await service.retry_processing(TENANT, BOSS, doc.document_id, expected_version=version)
    with pytest.raises(InvalidStateTransition):
        await service.confirm(TENANT, BOSS, doc.document_id, expected_version=version, revision_id=result.revision.revision_id)


async def test_confirmed_spreadsheet_cannot_be_reprocessed(setup):
    service, _, _ = setup
    doc, result = await ready(service)
    confirmed = await service.confirm(TENANT, BOSS, doc.document_id, expected_version=result.document.version, revision_id=result.revision.revision_id)
    with pytest.raises(PermissionDenied):
        await service.retry_processing(TENANT, BOSS, doc.document_id, expected_version=confirmed.document.version)
