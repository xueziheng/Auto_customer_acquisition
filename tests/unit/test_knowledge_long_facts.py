"""真实原文长片段可无损分段，原证据与容量门禁仍生效。"""
import pytest

from agent_runtime.enterprise_knowledge.analyze import analyze_knowledge
from shared.errors import ValidationError
from tests.unit.test_enterprise_knowledge_analysis import identity, provider


@pytest.mark.parametrize("length", [2001, 4001, 9999])
async def test_long_original_value_is_preserved_in_bounded_chunks(length):
    source = ("规格 原文 价格 123.45\n" * 1000)[:length]
    body = {"title": "原文", "facts": [{"label": "原产品行", "value": source, "source_quote": source}], "inferences": []}
    model = provider(body)
    result = await analyze_knowledge(model, identity(), source, model_name="deepseek-flash", max_output_tokens=1000)
    assert result.source_text == source
    assert "".join(f.value for f in result.analysis.facts) == source
    assert all(len(f.value) <= 2000 and f.source_quote == f.value for f in result.analysis.facts)
    assert not result.analysis.inferences
    model.generate.assert_awaited_once()


async def test_long_context_quote_can_be_narrowed_to_exact_value():
    source = "x" * 4500 + "Valve-A"
    model = provider({"title": "原文", "facts": [{"label": "型号", "value": "Valve-A", "source_quote": source}], "inferences": []})
    result = await analyze_knowledge(model, identity(), source, model_name="deepseek-flash", max_output_tokens=1000)
    assert result.analysis.facts[0].source_quote == "Valve-A"
    assert result.source_text == source


@pytest.mark.parametrize("case", ["ungrounded", "inference", "extra", "capacity"])
async def test_long_fact_normalization_does_not_bypass_validation(case):
    source = "a" * 6000
    fact = {"label": "字段", "value": source, "source_quote": source}
    body = {"title": "原文", "facts": [fact], "inferences": []}
    if case == "ungrounded":
        fact["value"] = "b" * 2001
    elif case == "inference":
        body["inferences"] = [{"text": "推断", "evidence_quotes": [source]}]
    elif case == "extra":
        fact["untrusted"] = "extra"
    else:
        body["facts"] = [fact] * 34
    model = provider(body)
    with pytest.raises(ValidationError):
        await analyze_knowledge(model, identity(), source, model_name="deepseek-flash", max_output_tokens=1000)
    model.generate.assert_awaited_once()
