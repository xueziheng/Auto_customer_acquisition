"""固定 A4 客户报价排版；不提供通用文档或文件加载能力。"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from pathlib import Path

import reportlab
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFError, TTFont
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import Paragraph, Spacer
from reportlab.platypus.doctemplate import LayoutError

from connectors.quote_pdf.limits import (
    BoundedBytesIO,
    LimitedSimpleDocTemplate,
    PageLimitExceeded,
    all_visible_text,
)
from shared.schemas.quote_document import CustomerQuoteView, QuotePdfRenderError

_BODY_FONT = "TradeOSQuoteVera"
_TITLE_FONT = "TradeOSQuoteVeraBold"
_MARGIN = 36
_BODY_SIZE = 10
_BODY_LEADING = 14
_TITLE_SIZE = 14
_DOCUMENT_TITLE = "Customer quotation"
_FIELD_LABELS = (
    "Issuer",
    "Issuer address",
    "Issuer contact",
    "Customer",
    "Description",
    "Specification",
    "Unit",
    "Quantity",
    "Unit price",
    "Total",
    "Currency",
    "Valid until",
)
_TERMS_LABEL = "Approved terms"
_FOOTER_GLYPHS = "Page 0123456789"


def _font_path(filename: str) -> Path:
    """只解析已安装 ReportLab 包内固定 Vera 字体，拒绝任何外部路径。"""
    return Path(reportlab.__file__).resolve().parent / "fonts" / filename


def _registered_font(name: str, filename: str) -> TTFont:
    """注册或核同名固定字体，防全局字体表被不同文件污染。"""
    path = _font_path(filename)
    try:
        if not path.is_file():
            raise OSError
        existing = pdfmetrics.getFont(name)
    except KeyError:
        try:
            font = TTFont(name, str(path))
            pdfmetrics.registerFont(font)
            return font
        except (OSError, TTFError, ValueError):
            raise QuotePdfRenderError("font_unavailable") from None
    except OSError:
        raise QuotePdfRenderError("font_unavailable") from None
    if not isinstance(existing, TTFont) or Path(existing.face.filename) != path:
        raise QuotePdfRenderError("font_unavailable")
    return existing


def load_fonts() -> tuple[TTFont, TTFont]:
    """加载固定正文/标题字体；文件缺失或损坏统一为固定错误。"""
    return _registered_font(_BODY_FONT, "Vera.ttf"), _registered_font(
        _TITLE_FONT, "VeraBd.ttf"
    )


def validate_glyphs(values: Iterable[str], fonts: tuple[TTFont, TTFont]) -> None:
    """以最终呈现所用正文/标题字体分别验证全部可见字符。"""
    body_font, title_font = fonts
    _validate_font(
        (*all_visible_text(values), "Quote ", " / Version ", _FOOTER_GLYPHS),
        body_font,
    )
    _validate_font((*_FIELD_LABELS, _DOCUMENT_TITLE, _TERMS_LABEL), title_font)


def _validate_font(values: Iterable[str], font: TTFont) -> None:
    """验证一个实际输出字体的完整文本集合，不将其扩大到另一种字体。"""
    mapping = font.face.charToGlyph
    if any(mapping.get(ord(character), 0) == 0 for value in values for character in value):
        raise QuotePdfRenderError("unsupported_glyph")


def _escaped_line(line: str) -> str:
    """保留行首/行尾及连续空白，同时留出内部首空格作为确定性换行点。"""
    def replace(match: re.Match[str]) -> str:
        spaces = match.group()
        if match.start() == 0 or match.end() == len(line):
            return "&nbsp;" * len(spaces)
        return " " + "&nbsp;" * (len(spaces) - 1)

    return re.sub(r" +", replace, html.escape(line, quote=False))


def _text_flowables(value: str, style: ParagraphStyle) -> tuple[object, ...]:
    """逐行生成可分页文字；空行由同等行距 spacer 保留视觉空白。"""
    return tuple(
        Paragraph(_escaped_line(line), style) if line else Spacer(1, style.leading)
        for line in value.split("\n")
    )


def _styles() -> tuple[ParagraphStyle, ParagraphStyle, ParagraphStyle]:
    """固定字号/行距，不能借动态缩字绕过页数限制。"""
    body = ParagraphStyle(
        "TradeOSQuoteBody", fontName=_BODY_FONT, fontSize=_BODY_SIZE,
        leading=_BODY_LEADING, splitLongWords=True,
    )
    label = ParagraphStyle(
        "TradeOSQuoteLabel", parent=body, fontName=_TITLE_FONT,
    )
    title = ParagraphStyle(
        "TradeOSQuoteTitle", parent=body, fontName=_TITLE_FONT,
        fontSize=_TITLE_SIZE, leading=18,
    )
    return body, label, title


def build_story(view: CustomerQuoteView) -> list[object]:
    """构造可分页 flowables；空文本不额外生成 flowable。"""
    body, label, title = _styles()
    story: list[object] = [
        Paragraph(_DOCUMENT_TITLE, title),
        *_text_flowables(f"Quote {view.quote_id} / Version {view.version}", body),
        Spacer(1, 8),
    ]
    fields = (
        (_FIELD_LABELS[0], view.issuer_name),
        (_FIELD_LABELS[1], view.issuer_address),
        (_FIELD_LABELS[2], view.issuer_contact),
        (_FIELD_LABELS[3], view.account_name),
        (_FIELD_LABELS[4], view.description),
        (_FIELD_LABELS[5], view.specification),
        (_FIELD_LABELS[6], view.unit),
        (_FIELD_LABELS[7], view.quantity_display),
        (_FIELD_LABELS[8], view.unit_price_display),
        (_FIELD_LABELS[9], view.total_display),
        (_FIELD_LABELS[10], view.currency),
        (_FIELD_LABELS[11], view.valid_until_display),
    )
    for field_name, value in fields:
        if value:
            story.extend((Paragraph(field_name, label), *_text_flowables(value, body)))
    if view.approved_terms:
        story.append(Spacer(1, 6))
        story.append(Paragraph(_TERMS_LABEL, label))
        for term in view.approved_terms:
            if term:
                story.extend(_text_flowables(term, body))
    return story


def _canvas(filename: object, **_: object) -> Canvas:
    """构造无时间/随机元数据的独立 Canvas，不加链接、附件或动作。"""
    canvas = Canvas(filename, pagesize=A4, invariant=1, pageCompression=1)
    canvas.setTitle("Customer quotation")
    canvas.setAuthor("TradeOS")
    canvas.setCreator("TradeOS")
    canvas.setSubject("Customer quotation")
    return canvas


def _footer(canvas: Canvas, document: LimitedSimpleDocTemplate) -> None:
    """绘制单页页脚，不为总页数进行预渲染。"""
    page = document.page
    canvas.setFont(_BODY_FONT, 9)
    canvas.drawRightString(A4[0] - _MARGIN, 20, f"Page {page}")


def render_pdf(
    view: CustomerQuoteView, *, maximum_bytes: int, maximum_pages: int
) -> bytes:
    """将已通过输入/字体检查的 view 排版到受限内存 PDF。"""
    sink = BoundedBytesIO(maximum_bytes)
    document = LimitedSimpleDocTemplate(
        sink, pagesize=A4, leftMargin=_MARGIN, rightMargin=_MARGIN,
        topMargin=_MARGIN, bottomMargin=_MARGIN, maximum_pages=maximum_pages,
    )
    try:
        document.build(build_story(view), onFirstPage=_footer, onLaterPages=_footer, canvasmaker=_canvas)
    except PageLimitExceeded:
        raise QuotePdfRenderError("page_limit_exceeded") from None
    except QuotePdfRenderError:
        raise
    except LayoutError:
        raise QuotePdfRenderError("layout_failed") from None
    except Exception:  # noqa: BLE001 - 第三方排版异常必须脱敏为固定跨层错误
        raise QuotePdfRenderError("render_failed") from None
    content = sink.read_result()
    if not content:
        raise QuotePdfRenderError("render_failed")
    return content
