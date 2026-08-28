"""固定 A4 客户报价排版；不提供通用文档或文件加载能力。"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Iterable

import reportlab
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
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
        except (OSError, ValueError):
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


def validate_glyphs(values: Iterable[str], fonts: Iterable[TTFont]) -> None:
    """以实际 TTFont glyph 映射验证可见字符，绝不使用系统 fallback。"""
    text = tuple(all_visible_text(values))
    for font in fonts:
        mapping = font.face.charToGlyph
        if any(mapping.get(ord(character), 0) == 0 for value in text for character in value):
            raise QuotePdfRenderError("unsupported_glyph")


def _escaped(value: str) -> str:
    """只把原客户字符串转义为可换行文本，保留换行和连续空白。"""
    escaped = html.escape(value, quote=False).replace("\n", "<br/>")
    while "  " in escaped:
        escaped = escaped.replace("  ", " &nbsp;")
    return escaped


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
        Paragraph("Customer quotation", title),
        Paragraph(_escaped(f"Quote {view.quote_id} / Version {view.version}"), body),
        Spacer(1, 8),
    ]
    fields = (
        ("Issuer", view.issuer_name),
        ("Issuer address", view.issuer_address),
        ("Issuer contact", view.issuer_contact),
        ("Customer", view.account_name),
        ("Description", view.description),
        ("Specification", view.specification),
        ("Unit", view.unit),
        ("Quantity", view.quantity_display),
        ("Unit price", view.unit_price_display),
        ("Total", view.total_display),
        ("Currency", view.currency),
        ("Valid until", view.valid_until_display),
    )
    for field_name, value in fields:
        if value:
            story.extend((Paragraph(field_name, label), Paragraph(_escaped(value), body)))
    if view.approved_terms:
        story.append(Spacer(1, 6))
        story.append(Paragraph("Approved terms", label))
        for term in view.approved_terms:
            if term:
                story.append(Paragraph(_escaped(term), body))
    return story


def _canvas(filename: object, **_: object) -> Canvas:
    """构造无时间/随机元数据的独立 Canvas，不加链接、附件或动作。"""
    canvas = Canvas(filename, pagesize=A4, invariant=1, pageCompression=1)
    canvas.setTitle("Customer quotation")
    canvas.setAuthor("TradeOS")
    canvas.setCreator("TradeOS")
    canvas.setSubject("Customer quotation")
    return canvas


def _footer(canvas: Canvas, document: object) -> None:
    """绘制单页页脚，不为总页数进行预渲染。"""
    page = getattr(document, "page")
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
    except Exception:
        raise QuotePdfRenderError("render_failed") from None
    content = sink.read_result()
    if not content:
        raise QuotePdfRenderError("render_failed")
    return content
