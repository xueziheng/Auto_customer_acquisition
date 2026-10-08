"""企业资料 prompt 的受控契约评估；不连接 Provider，不声称真实模型准确率。"""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent_runtime.enterprise_knowledge.analyze import (
    KnowledgeAnalysisResult,
    analyze_knowledge,
)
from domains.products.schemas import (
    KnowledgeDocumentDetail,
    KnowledgeDocumentView,
    KnowledgeRevisionView,
    KnowledgeSource,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import ArtifactId, EmployeeId, RunId, TenantId, UserId
from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelInputImage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from shared.schemas.provenance import ProvenanceSummary, SourceType
from workflows.enterprise_knowledge.markdown import render_knowledge

CASES = tuple(sorted(Path(__file__).parent.glob("*/input.json")))
MODEL = "synthetic-knowledge-eval-v1"
NOW = datetime(2026, 10, 8, tzinfo=UTC)
IDENTITY = InvocationIdentity(
    tenant_id=TenantId("tn_knowledge_eval"),
    user_id=UserId("usr_knowledge_eval"),
    employee_id=EmployeeId("emp_knowledge_eval"),
    run_id=RunId("run_knowledge_eval"),
    capability="knowledge_ingest",
    configuration_version="synthetic-eval-v1",
    sequence=0,
)
# 仅作为受信图像封装的占位；本组不测试 OCR 或图像解析。
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/lR8AAAAASUVORK5CYII="
)


class CandidateModel:
    def __init__(self, candidate: object) -> None:
        self.response = ModelResponse(
            text=json.dumps(candidate, ensure_ascii=False),
            model=MODEL,
            usage=ModelUsage(
                input_tokens=None, cached_input_tokens=None, output_tokens=None
            ),
        )
        self.calls: list[tuple[InvocationIdentity, ModelRequest]] = []

    async def generate(
        self, identity: InvocationIdentity, request: ModelRequest
    ) -> ModelResponse:
        self.calls.append((identity, request))
        return self.response


def detail(
    result: KnowledgeAnalysisResult, parse_warnings: tuple[str, ...]
) -> KnowledgeDocumentDetail:
    visual = result.source_kind == "vision_transcription"
    source = KnowledgeSource(
        filename="synthetic.pdf" if visual else "synthetic.md",
        mime_type="application/pdf" if visual else "text/markdown",
        size_bytes=1,
        sha256=hashlib.sha256(b"x").hexdigest(),
        artifact_id=ArtifactId("art_knowledge_eval"),
    )
    revision = KnowledgeRevisionView(
        revision_id="krev_knowledge_eval",
        document_id="kdoc_knowledge_eval",
        job_id=str(IDENTITY.run_id),
        source_text=result.source_text,
        analysis=result.analysis,
        model=MODEL,
        extracted_by="controlled-eval-v1",
        extracted_at=NOW,
        fact_provenance=tuple(
            ProvenanceSummary(
                source_type=SourceType.UPLOAD,
                source_id=str(source.artifact_id),
                extracted_by="controlled-eval-v1",
                extracted_at=NOW,
                confirmed_by=None,
                confirmed_at=None,
            )
            for _ in result.analysis.facts
        ),
        source_kind=result.source_kind,
        image_count=result.image_count,
        image_source_pages=result.image_source_pages,
        parse_warnings=parse_warnings,
    )
    document = KnowledgeDocumentView(
        document_id=revision.document_id,
        tenant_id=IDENTITY.tenant_id,
        source=source,
        uploader_id=IDENTITY.employee_id,
        status="awaiting_confirmation",
        version=3,
        job_id=str(IDENTITY.run_id),
        current_revision_id=revision.revision_id,
        created_at=NOW,
        updated_at=NOW,
    )
    return KnowledgeDocumentDetail(document=document, revision=revision)


@pytest.mark.asyncio
@pytest.mark.parametrize("input_path", CASES, ids=lambda path: path.parent.name)
async def test_enterprise_knowledge_controlled_eval(input_path: Path) -> None:
    case = json.loads(input_path.read_text(encoding="utf-8"))
    expected = json.loads(
        input_path.with_name("expected.json").read_text(encoding="utf-8")
    )
    model = CandidateModel(case["candidate_response"])
    source_pages = tuple(case.get("image_source_pages", ()))
    images = tuple(
        ModelInputImage(
            mime_type="image/png",
            data=PNG,
            sha256=hashlib.sha256(PNG).hexdigest(),
            byte_length=len(PNG),
        )
        for _ in source_pages
    )

    async def analyze() -> KnowledgeAnalysisResult:
        return await analyze_knowledge(
            model,
            IDENTITY,
            case["document"],
            model_name=MODEL,
            max_output_tokens=1500,
            images=images,
            image_source_pages=source_pages,
        )

    if expected["outcome"] == "reject":
        with pytest.raises(ValidationError) as failure:
            await analyze()
        assert str(failure.value) == expected["safe_error"]
    else:
        result = await analyze()
        assert [fact.value for fact in result.analysis.facts] == expected["fact_values"]
        assert result.source_kind == expected["source_kind"]
        assert list(result.image_source_pages) == expected["image_source_pages"]
        mapped_pages = [
            result.image_source_pages[index - 1]
            for inference in result.analysis.inferences
            for index in inference.image_pages
        ]
        assert mapped_pages == expected["inference_source_pages"]
        if result.source_kind == "document_text":
            assert result.source_text == case["document"]
        else:
            assert result.source_text == case["candidate_response"]["source_text"]

        warnings = tuple(case.get("parse_warnings", ()))
        rendered = render_knowledge(detail(result, warnings))
        assert "状态：待管理员确认" in rendered
        assert "不构成客户报价" in rendered
        if warnings:
            assert "解析提醒（不属于原件事实）" in rendered
            assert all(warning not in result.source_text for warning in warnings)
        if result.source_kind == "vision_transcription":
            assert "AI看图转录，需对照原件核对" in rendered
            for page in expected["inference_source_pages"]:
                assert f"原件图像页：{page}" in rendered

    assert len(model.calls) == 1
    identity, request = model.calls[0]
    assert identity == IDENTITY
    assert request.payload["document"] == case["document"]
    assert request.payload["image_source_pages"] == list(source_pages)
    assert request.images == images
    assert "tenant_id" not in request.payload
    assert "action" not in request.payload
    for warning in case.get("parse_warnings", ()):
        assert warning not in request.payload["document"]
    foreign = case.get("foreign_context_not_available")
    if foreign:
        assert foreign not in json.dumps(request.payload, ensure_ascii=False)
