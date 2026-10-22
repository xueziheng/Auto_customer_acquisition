"""Grouping instructions never replace or enrich the uploaded source."""
from agent_runtime.enterprise_knowledge.analyze import analyze_knowledge
from tests.unit.test_enterprise_knowledge_analysis import identity, provider


async def test_table_rows_keep_values_and_evidence_without_cell_expansion():
    rows = [f'A{i}={i},B{i}=Product-{i},E{i}=10,F{i}=120' for i in range(1, 26)]
    source = '\n'.join(rows)
    body = {'title': '来源表格', 'facts': [
        {'label': f'工作表第{i}行', 'value': row, 'source_quote': row}
        for i, row in enumerate(rows, 1)
    ], 'inferences': []}
    model = provider(body)
    result = await analyze_knowledge(model, identity(), source, model_name='deepseek-flash', max_output_tokens=1000)
    request = model.generate.await_args.args[1]
    assert request.payload['document'] == source
    assert result.source_text == source
    assert [fact.value for fact in result.analysis.facts] == rows
    assert [fact.source_quote for fact in result.analysis.facts] == rows
    assert len(result.analysis.facts) == 25
    assert '每个逻辑产品行作为一个fact' in request.system_prompt
    assert '不截断或忽略后面的产品' in request.system_prompt
    assert '示例不是本次资料' in request.system_prompt
    assert not result.analysis.inferences
    model.generate.assert_awaited_once()
