"""文档和视觉分析输出的证据门，不访问真实 Provider。"""
from __future__ import annotations

import hashlib
import json
from unittest.mock import AsyncMock

import pytest

from agent_runtime.enterprise_knowledge.analyze import analyze_knowledge
from shared.errors import ValidationError
from shared.schemas.identifiers import EmployeeId, RunId, TenantId, UserId
from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelInputImage,
    ModelResponse,
    ModelUsage,
)


def identity():
    return InvocationIdentity(
        tenant_id=TenantId("tenant"), user_id=UserId("user"), employee_id=EmployeeId("employee"),
        run_id=RunId("run"), capability="knowledge_ingest", configuration_version="v1", sequence=0,
    )

def provider(body):
    response = ModelResponse(text=json.dumps(body), model="deepseek-flash",
        usage=ModelUsage(input_tokens=1, cached_input_tokens=0, output_tokens=1))
    return type("Model", (), {"generate": AsyncMock(return_value=response)})()

def analysis(value="Valve-A"):
    return {"title":"阀门资料","facts":[{"label":"型号","value":value,"source_quote":"Model: Valve-A."}],"inferences":[]}

@pytest.mark.asyncio
async def test_exact_quote_only():
    model=provider(analysis())
    result=await analyze_knowledge(model,identity(),"Model: Valve-A.",model_name="deepseek-flash",max_output_tokens=200)
    assert result.analysis.facts[0].value=="Valve-A"
    assert result.source_kind=="document_text"

@pytest.mark.asyncio
async def test_invented_spec_rejected():
    with pytest.raises(ValidationError):
        await analyze_knowledge(provider(analysis("Valve-B")),identity(),"Model: Valve-A.",model_name="deepseek-flash",max_output_tokens=200)

@pytest.mark.asyncio
async def test_credential_marker_never_sent():
    model=provider(analysis())
    with pytest.raises(ValidationError):
        await analyze_knowledge(model,identity(),"api_key=placeholder-sensitive-value",model_name="deepseek-flash",max_output_tokens=200)
    model.generate.assert_not_called()

@pytest.mark.asyncio
async def test_image_observation_remains_inference():
    data=b"\\x89PNG\\r\\n\\x1a\\n".decode("unicode_escape").encode("latin1") + b"synthetic-header"
    image=ModelInputImage(mime_type="image/png",data=data,sha256=hashlib.sha256(data).hexdigest(),byte_length=len(data))
    model=provider({"source_text":"图像中没有可确认的文字，请对照原件核对观察。",
        "analysis":{"title":"待核对图片","facts":[],"inferences":[{"text":"AI推断：外观呈圆形，需核对。","evidence_quotes":[],"image_pages":[1]}]}})
    result=await analyze_knowledge(model,identity(),"",model_name="deepseek-flash",max_output_tokens=200,images=(image,),image_source_pages=(1,))
    assert result.source_kind=="vision_transcription"
    assert not result.analysis.facts
    assert result.image_count==1
    assert model.generate.await_args.args[1].images==(image,)

@pytest.mark.asyncio
async def test_text_input_cannot_forge_visual_reference():
    model=provider({"title":"非法视觉来源","facts":[],"inferences":[{"text":"推断","evidence_quotes":[],"image_pages":[1]}]})
    with pytest.raises(ValidationError):
        await analyze_knowledge(model,identity(),"Model: Valve-A.",model_name="deepseek-flash",max_output_tokens=200)

@pytest.mark.asyncio
async def test_invalid_image_mapping_never_calls_provider():
    model = provider(analysis())
    with pytest.raises(ValidationError, match="页码"):
        await analyze_knowledge(model, identity(), "", model_name="deepseek-flash",
                                max_output_tokens=200, image_source_pages=(1,))
    model.generate.assert_not_called()
