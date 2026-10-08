"""受信 Markdown 模板；用户文件名与模型正文只能成为转义文本。"""
from __future__ import annotations

import html
import re

from domains.products.schemas import KnowledgeDocumentDetail
from shared.errors import ValidationError


def _text(value: str) -> str:
    value = html.escape(value, quote=True)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", value).replace("\n", "  \n")

def render_knowledge(detail: KnowledgeDocumentDetail) -> str:
    """投影不可变版本及完整来源说明，不构造模型控制的链接或 frontmatter。"""
    doc, revision = detail.document, detail.revision
    if revision is None or revision.document_id != doc.document_id:
        raise ValidationError("资料投影绑定无效")
    lines = [
        "# " + _text(revision.analysis.title), "",
        "状态：" + ("已人工确认" if doc.status == "confirmed" else "待管理员确认"), "",
        "企业：" + _text(str(doc.tenant_id)),
        "资料：" + _text(doc.document_id),
        "版本：" + str(doc.version),
        "来源文件：" + _text(doc.source.filename),
        "原件标识：" + _text(str(doc.source.artifact_id)),
        "原件 SHA256：" + doc.source.sha256,
        "分析者：" + _text(revision.extracted_by),
        "来源处理：" + ("AI看图转录，需对照原件核对" if revision.source_kind == "vision_transcription" else "文件文本提取"),
        "模型：" + _text(revision.model),
        "分析时间：" + revision.extracted_at.isoformat(),
        "确认人：" + _text(str(revision.confirmed_by or "未确认")),
        "确认时间：" + (revision.confirmed_at.isoformat() if revision.confirmed_at else "未确认"),
        "", "## 来源内容提取（AI结果须对照原件）", "", _text(revision.source_text), "",
    ]
    if revision.parse_warnings:
        lines.extend(["## 解析提醒（不属于原件事实）", *(_text(warning) for warning in revision.parse_warnings), ""])
    lines.extend(["## 来源事实", ""])
    for fact in revision.analysis.facts:
        lines.extend(["### " + _text(fact.label), _text(fact.value), "", "原文证据：", _text(fact.source_quote), ""])
    lines.extend(["## 分析推断", ""])
    for inference in revision.analysis.inferences:
        lines.extend([_text(inference.text), "依据："])
        lines.extend(_text(quote) for quote in inference.evidence_quotes)
        if inference.image_pages:
            lines.append("原件图像页：" + "、".join(str(revision.image_source_pages[page - 1]) for page in inference.image_pages))
        lines.append("")
    if not revision.analysis.inferences:
        lines.append("无。")
    lines.extend(["", "本文是企业内部资料，不构成客户报价、供应保证或对外承诺。", ""])
    return "\n".join(lines)
