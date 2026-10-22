"""模型输出容量保持原边界，禁止通过截断原件或放宽证据验收。"""
import pytest

from agent_runtime.enterprise_knowledge.analyze import analyze_knowledge
from shared.errors import ValidationError
from tests.unit.test_enterprise_knowledge_analysis import analysis, identity, provider


@pytest.mark.parametrize("field,length", [("title", 241), ("label", 101), ("value", 2001), ("source_quote", 4001), ("facts", 101)])
async def test_over_limit_is_redacted_and_never_retried(field, length):
    body = analysis()
    if field == "facts":
        body["facts"] = body["facts"] * length
    elif field == "title":
        body[field] = "x" * length
    else:
        body["facts"][0][field] = "x" * length
    model = provider(body)
    with pytest.raises(ValidationError) as caught:
        await analyze_knowledge(model, identity(), "Model: Valve-A.", model_name="deepseek-flash", max_output_tokens=1000)
    assert caught.value.context == {"knowledge_stage": "response_schema_limit_" + field}
    model.generate.assert_awaited_once()

async def test_text_format_constraints_do_not_change_source_or_exact_values():
    source = "Model: Valve-A."
    model = provider(analysis())
    result = await analyze_knowledge(model, identity(), source, model_name="deepseek-flash", max_output_tokens=1000)
    request = model.generate.await_args.args[1]
    assert request.payload["document"] == source
    assert result.source_text == source
    assert result.analysis.facts[0].value == "Valve-A"
    for constraint in ["240", "100", "2000", "4000", "inferences必须为[]"]:
        assert constraint in request.system_prompt
    assert not result.analysis.inferences
