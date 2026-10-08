"""企业资料任务重启/撤权/失败语义；无真实模型或文件IO。"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock

import pytest

from agent_runtime.enterprise_knowledge.analyze import KnowledgeAnalysisResult
from domains.products.schemas import KnowledgeAnalysis, KnowledgeClaim, KnowledgeSource
from domains.products.service import EnterpriseKnowledgeService
from shared.errors import PermissionDenied
from shared.schemas.identifiers import ArtifactId, EmployeeId, TenantId
from workflows.enterprise_knowledge.processor import (
    EnterpriseKnowledgeDriver,
    KnowledgeInput,
    KnowledgeProcessingFailure,
)

TENANT = TenantId("tn_test")
SOURCE_TEXT = "Model: Valve-A. Material: stainless steel."
ANALYSIS = KnowledgeAnalysis.model_validate({
    "title": "产品资料",
    "facts": [{"label": "型号", "value": "Valve-A", "source_quote": "Model: Valve-A."}],
    "inferences": [],
})

def claim() -> KnowledgeClaim:
    return KnowledgeClaim(
        tenant_id=TENANT, job_id="run_job", document_id="kdoc_test",
        actor_id=EmployeeId("emp_test"), user_id="usr_test",
        lease_token="test-lease", lease_until=datetime.now(UTC) + timedelta(minutes=5),
        attempt=1, source=KnowledgeSource(
            filename="product.txt", mime_type="text/plain", size_bytes=40,
            sha256="a" * 64, artifact_id=ArtifactId("art_test"),
        ),
    )

def setup_driver():
    job = claim()
    service = SimpleNamespace(
        claim_next=AsyncMock(return_value=job),
        authorize_processing=AsyncMock(),
        complete_processing=AsyncMock(),
        fail_processing=AsyncMock(),
        list_pending_exports=AsyncMock(return_value=()),
        mark_export=AsyncMock(),
    )
    reader = SimpleNamespace(read=AsyncMock(return_value=SOURCE_TEXT.encode()))
    parser = SimpleNamespace(parse=AsyncMock(return_value=KnowledgeInput(SOURCE_TEXT)))
    analyzer = SimpleNamespace(analyze=AsyncMock(return_value=KnowledgeAnalysisResult(
        SOURCE_TEXT, ANALYSIS, "document_text", 0, (),
    )))
    exporter = SimpleNamespace(export=AsyncMock())
    driver = EnterpriseKnowledgeDriver(
        tenant_id=TENANT, service=cast(EnterpriseKnowledgeService, service),
        reader=reader, parser=parser, analyzer=analyzer, exporter=exporter,
        lease_owner="scheduler", lease_seconds=300, model_name="deepseek-flash",
    )
    return driver, service, reader, parser, analyzer, exporter

@pytest.mark.asyncio
async def test_no_job_is_zero_model():
    driver, service, _, _, analyzer, _ = setup_driver()
    service.claim_next.return_value = None
    assert await driver.scan_once() == 0
    analyzer.analyze.assert_not_called()

@pytest.mark.asyncio
async def test_process_reauthorizes_and_records_provenance():
    driver, service, _, _, analyzer, _ = setup_driver()
    assert await driver.scan_once() == 1
    assert service.authorize_processing.await_count == 3
    analyzer.analyze.assert_awaited_once()
    command = service.complete_processing.await_args.kwargs
    assert command["source_kind"] == "document_text"
    assert command["source_text"] == SOURCE_TEXT
    assert command["extracted_by"] == "codex-cli:enterprise-knowledge-v1"

@pytest.mark.asyncio
async def test_unknown_never_automatically_replays():
    driver, service, _, _, analyzer, _ = setup_driver()
    analyzer.analyze.side_effect = KnowledgeProcessingFailure("model_result_unknown", uncertain=True)
    await driver.scan_once()
    assert service.fail_processing.await_args.kwargs["uncertain"] is True
    assert analyzer.analyze.await_count == 1
    service.complete_processing.assert_not_called()

@pytest.mark.asyncio
async def test_revoked_before_read_is_zero_provider_and_source():
    driver, service, reader, _, analyzer, _ = setup_driver()
    service.authorize_processing.side_effect = PermissionDenied("已撤权")
    await driver.scan_once()
    reader.read.assert_not_called()
    analyzer.analyze.assert_not_called()
    assert service.fail_processing.await_args.kwargs["reason"] == "authorization_revoked"

@pytest.mark.asyncio
async def test_cancel_after_model_started_is_unknown_and_propagates():
    driver, service, _, _, analyzer, _ = setup_driver()
    analyzer.analyze.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await driver.scan_once()
    assert service.fail_processing.await_args.kwargs["uncertain"] is True

@pytest.mark.asyncio
async def test_database_commit_uncertainty_does_not_call_again():
    driver, service, _, _, analyzer, _ = setup_driver()
    service.complete_processing.side_effect = RuntimeError("private data")
    await driver.scan_once()
    assert service.fail_processing.await_args.kwargs["uncertain"] is True
    analyzer.analyze.assert_awaited_once()

@pytest.mark.asyncio
async def test_vault_failure_does_not_call_model():
    driver, service, _, _, analyzer, exporter = setup_driver()
    service.claim_next.return_value = None
    detail = SimpleNamespace(
        document=SimpleNamespace(tenant_id=TENANT, document_id="doc", version=3),
        revision=SimpleNamespace(revision_id="rev"),
    )
    service.list_pending_exports.side_effect = [(detail,), ()]
    exporter.export.side_effect = OSError("private path")
    assert await driver.scan_once() == 1
    assert service.mark_export.await_args.kwargs["state"] == "failed"
    analyzer.analyze.assert_not_called()
