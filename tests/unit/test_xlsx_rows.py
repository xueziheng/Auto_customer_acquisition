"""表格逐行完整性、原文不变和单次模型调用验收。"""
import csv
import io

import pytest

from tests.unit.test_enterprise_knowledge_processor import setup_driver
from workflows.enterprise_knowledge.processor import KnowledgeInput
from workflows.enterprise_knowledge.xlsx_rows import XLSX_MIME, organize_xlsx_rows


def source(rows=25):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["[xl/worksheets/sheet1.xml]"])
    for row in range(1, rows + 1):
        writer.writerow([f"A{row}=产品{row}", f'B{row}=第一行\n第二行,"原文"\u2028不改', f"C{row}={row}.40"])
    writer.writerow(["[xl/worksheets/sheet2.xml]"])
    return output.getvalue()


def test_every_row_and_last_row_preserved_without_inference():
    text = source()
    result = organize_xlsx_rows(text)
    assert result.source_text == text
    assert len(result.analysis.facts) == 25
    assert "A25=产品25" in result.analysis.facts[-1].value
    assert not result.analysis.inferences
    for fact in result.analysis.facts:
        assert fact.value == fact.source_quote
        assert fact.source_quote in text


def test_long_row_split_without_losing_characters():
    text = "[xl/worksheets/sheet1.xml]\nA1=" + "规" * 4200 + "\n"
    facts = organize_xlsx_rows(text).analysis.facts
    assert len(facts) == 3
    assert "".join(f.value for f in facts) == text.split("\n")[1]


@pytest.mark.parametrize("text", [
    source(101), "A1=missing sheet\n", "[xl/worksheets/sheet1.xml]\nA1=a,B2=b\n",
    "[xl/worksheets/sheet1.xml]\nA1=a\nA1=b\n", "[xl/worksheets/sheet1.xml]\nA1=a,A1=b\n",
])
def test_incomplete_or_over_capacity_layout_fails_closed(text):
    with pytest.raises(ValueError):
        organize_xlsx_rows(text)


async def test_xlsx_driver_calls_model_once_and_reports_true_extractor():
    driver, service, _, parser, analyzer, _ = setup_driver()
    claim = service.claim_next.return_value
    claim = claim.model_copy(update={"source": claim.source.model_copy(update={"mime_type": XLSX_MIME, "filename": "quote.xlsx"})})
    service.claim_next.return_value = claim
    parser.parse.return_value = KnowledgeInput(source())
    analyzer.analyze.return_value = organize_xlsx_rows(source())
    await driver.scan_once()
    analyzer.analyze.assert_awaited_once()
    args = service.complete_processing.await_args.kwargs
    assert args["model"] == "deepseek-flash"
    assert args["extracted_by"] == "codex-cli:enterprise-knowledge-v1"
    assert len(args["analysis"].facts) == 25
    assert args["source_text"] == source()
