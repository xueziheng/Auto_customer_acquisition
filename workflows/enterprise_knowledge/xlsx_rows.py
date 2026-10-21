"""将受信 XLSX 解析器的逐行文本投影为完整原文事实，不调用模型。"""
from __future__ import annotations

import csv
import io
import re

from agent_runtime.enterprise_knowledge.analyze import KnowledgeAnalysisResult
from domains.products.schemas import KnowledgeAnalysis, KnowledgeFact

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def organize_xlsx_rows(source_text: str) -> KnowledgeAnalysisResult:
    """每个已解析行全部保留；不能在100条/2000字符契约内完整表达时拒绝。"""
    lines = io.StringIO(source_text, newline="").readlines()
    reader = csv.reader(io.StringIO(source_text, newline=""), strict=True)
    facts: list[KnowledgeFact] = []
    sheet: str | None = None
    seen_sheets: set[str] = set()
    seen_rows: set[tuple[str, int]] = set()
    previous = 0
    for cells in reader:
        raw = "".join(lines[previous:reader.line_num])
        previous = reader.line_num
        if len(cells) == 1 and re.fullmatch(r"\[xl/worksheets/sheet[0-9]+\.xml\]", cells[0]):
            sheet = cells[0][1:-1]
            if sheet in seen_sheets:
                raise ValueError("表格工作表重复")
            seen_sheets.add(sheet)
            continue
        if sheet is None or not cells:
            raise ValueError("表格来源结构无效")
        addresses = [re.match(r"([A-Z]+)([1-9][0-9]*)=", cell) for cell in cells]
        if any(address is None for address in addresses):
            raise ValueError("表格单元格定位无效")
        row_numbers = {int(address.group(2)) for address in addresses if address is not None}
        columns = [address.group(1) for address in addresses if address is not None]
        if len(row_numbers) != 1 or len(columns) != len(set(columns)):
            raise ValueError("表格行定位无效")
        row = row_numbers.pop()
        if (sheet, row) in seen_rows:
            raise ValueError("表格行重复")
        seen_rows.add((sheet, row))
        # 只移除CSV记录终止符，单元格内换行、引号和空白全部保持原样。
        value = raw.removesuffix("\n")
        chunks = [value[offset:offset + 2000] for offset in range(0, len(value), 2000)]
        if not chunks or len(facts) + len(chunks) > 100 or any(not part.strip() for part in chunks):
            raise ValueError("表格行容量超限")
        for index, part in enumerate(chunks, 1):
            facts.append(KnowledgeFact(
                label=f"{sheet} 第{row}行（{index}/{len(chunks)}）",
                value=part, source_quote=part,
            ))
    if previous != len(lines) or not seen_rows:
        raise ValueError("表格原文未完整覆盖")
    analysis = KnowledgeAnalysis(title="表格原文逐行整理", facts=tuple(facts), inferences=())
    analysis.validate_evidence(source_text)
    return KnowledgeAnalysisResult(source_text, analysis, "document_text", 0, ())
