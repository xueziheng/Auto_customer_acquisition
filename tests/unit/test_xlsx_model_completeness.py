"""真实XLSX任务不得用成功标签掩盖缺行、改价或来源改写。"""
from dataclasses import replace

import pytest

from tests.unit.test_enterprise_knowledge_processor import setup_driver
from tests.unit.test_xlsx_rows import source
from workflows.enterprise_knowledge.processor import (
    KnowledgeInput,
    KnowledgeProcessingFailure,
)
from workflows.enterprise_knowledge.xlsx_rows import XLSX_MIME, organize_xlsx_rows


def xlsx_driver():
    driver, service, _, parser, analyzer, _ = setup_driver()
    job = service.claim_next.return_value
    service.claim_next.return_value = job.model_copy(update={
        'source': job.source.model_copy(update={'mime_type': XLSX_MIME, 'filename': 'quote.xlsx'})
    })
    parser.parse.return_value = KnowledgeInput(source(), warnings=('公式未计算，图片保留在原件。',))
    analyzer.analyze.return_value = organize_xlsx_rows(source())
    return driver, service, parser, analyzer


@pytest.mark.parametrize('case', ['missing_row', 'altered_price', 'rewritten_source', 'model_unknown'])
async def test_incomplete_model_result_never_commits_or_retries(case):
    driver, service, _, analyzer = xlsx_driver()
    result = analyzer.analyze.return_value
    if case == 'missing_row':
        result = replace(result, analysis=result.analysis.model_copy(update={'facts': result.analysis.facts[:-1]}))
    elif case == 'altered_price':
        fact = result.analysis.facts[-1]
        changed = fact.model_copy(update={'value': fact.value.replace('25.40', '25.41')})
        result = replace(result, analysis=result.analysis.model_copy(update={'facts': (*result.analysis.facts[:-1], changed)}))
    elif case == 'rewritten_source':
        result = replace(result, source_text=result.source_text + '额外内容')
    else:
        analyzer.analyze.side_effect = KnowledgeProcessingFailure('model_result_unknown', uncertain=True)
    analyzer.analyze.return_value = result
    await driver.scan_once()
    analyzer.analyze.assert_awaited_once()
    service.complete_processing.assert_not_called()
    assert service.fail_processing.await_args.kwargs['reason'] == ('model_result_unknown' if case == 'model_unknown' else 'invalid_analysis')


async def test_preflight_capacity_failure_is_zero_model():
    driver, service, parser, analyzer = xlsx_driver()
    parser.parse.return_value = KnowledgeInput(source(101))
    await driver.scan_once()
    analyzer.analyze.assert_not_called()
    service.complete_processing.assert_not_called()
    assert service.fail_processing.await_args.kwargs['reason'] == 'source_limit_exceeded'


async def test_valid_model_result_preserves_parser_warnings():
    driver, service, parser, analyzer = xlsx_driver()
    await driver.scan_once()
    assert service.complete_processing.await_args.kwargs['parse_warnings'] == parser.parse.return_value.warnings
    analyzer.analyze.assert_awaited_once()
