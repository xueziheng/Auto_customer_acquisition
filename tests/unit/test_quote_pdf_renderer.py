"""离线客户报价 PDF 的公开契约与真实渲染验收。"""

from __future__ import annotations

import subprocess
import sys
from io import BytesIO
from pathlib import Path
from typing import get_type_hints

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject, NameObject

from domains.quotations import service as quotations_service
from domains.quotations.errors import QuotationError
from shared.schemas.quote_document import CustomerQuoteView
from tests.unit.test_quotation_contracts import detail_fixture


def _renderer(
    *, maximum_bytes: int = 1_000_000, maximum_pages: int = 10,
    maximum_text_bytes: int = 100_000,
):
    from connectors.quote_pdf.client import ReportLabQuotePdfRenderer

    return ReportLabQuotePdfRenderer(
        maximum_bytes, maximum_pages, maximum_text_bytes=maximum_text_bytes
    )


def _customer_view(**changes: object) -> CustomerQuoteView:
    view = quotations_service.project_customer(detail_fixture())
    return view.model_copy(update=changes)


def test_quote_pdf_renderer_protocol_uses_the_shared_customer_view() -> None:
    """报价域公开端口只接受唯一客户投影，connector 无需导入报价域。"""
    renderer = quotations_service.QuotePdfRenderer
    hints = get_type_hints(renderer.render)

    assert hints["view"] is CustomerQuoteView
    assert hints["return"] is bytes


@pytest.mark.parametrize(
    ("code", "message"),
    [
        ("invalid_config", "PDF配置无效"),
        ("invalid_input", "客户视图无效"),
        ("template_unsupported", "模板未注册"),
        ("text_limit_exceeded", "客户文本超限"),
        ("page_limit_exceeded", "PDF页数超限"),
        ("byte_limit_exceeded", "PDF字节超限"),
        ("font_unavailable", "PDF字体不可用"),
        ("unsupported_glyph", "字体不支持客户字符"),
        ("layout_failed", "PDF排版失败"),
        ("render_failed", "PDF渲染失败"),
    ],
)
def test_quote_pdf_render_error_has_a_fixed_safe_message(
    code: str, message: str
) -> None:
    """跨层渲染错误只接受固定 code，不能带客户原文或运行期上下文。"""
    error = quotations_service.QuotePdfRenderError(code)  # type: ignore[arg-type]

    assert error.code == code
    assert str(error) == message
    assert error.is_retryable is False


def test_domain_rejects_changed_customer_price_before_renderer() -> None:
    """展示金额篡改由报价域的同一真实投影校验拦截。"""
    detail = detail_fixture()
    view = quotations_service.project_customer(detail)
    changed = view.model_copy(update={"total_display": "tampered"})

    with pytest.raises(QuotationError) as caught:
        quotations_service.validate_customer_projection(changed, detail)

    assert caught.value.code == "basis_mismatch"


def test_renderer_rejects_missing_or_boolean_resource_limits() -> None:
    """三个资源上限都必须是非布尔正整数，不能有隐式生产默认值。"""
    from connectors.quote_pdf.client import ReportLabQuotePdfRenderer
    from shared.schemas.quote_document import QuotePdfRenderError

    for values in ((0, 1, 1), (1, 0, 1), (1, 1, 0), (True, 1, 1)):
        with pytest.raises(QuotePdfRenderError) as caught:
            ReportLabQuotePdfRenderer(
                values[0], values[1], maximum_text_bytes=values[2]
            )
        assert caught.value.code == "invalid_config"


def test_renderer_rejects_unregistered_template_before_rendering() -> None:
    """模板只能来自 shared 的唯一注册集合，不能作为自由文件路径。"""
    from shared.schemas.quote_document import QuotePdfRenderError

    with pytest.raises(QuotePdfRenderError) as caught:
        _renderer().render(_customer_view(), template_version="../../invoice.html")

    assert caught.value.code == "template_unsupported"


def test_pdf_contains_all_customer_projection_fields_and_terms_in_order() -> None:
    """真实 PDF 完整展示唯一客户投影，且不加密。"""
    view = _customer_view(approved_terms=("First approved term", "Second approved term"))

    content = _renderer().render(view, template_version="quote_pdf_v1")
    reader = PdfReader(BytesIO(content))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)

    assert content.startswith(b"%PDF-")
    assert not reader.is_encrypted
    for value in (
        view.quote_id, view.issuer_name, view.issuer_address, view.issuer_contact,
        view.account_name, view.description, view.specification, view.unit,
        view.quantity_display, view.unit_price_display, view.total_display,
        view.currency, view.valid_until_display, *view.approved_terms,
    ):
        assert value in text
    assert text.index(view.approved_terms[0]) < text.index(view.approved_terms[1])


def test_long_terms_paginate_and_draw_each_page_footer() -> None:
    """长条款可分页，不能塞进不可分割表格单元或丢失页脚。"""
    view = _customer_view(approved_terms=(("approved term " * 500) + "end",))

    reader = PdfReader(BytesIO(_renderer(maximum_pages=10).render(
        view, template_version="quote_pdf_v1"
    )))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)

    assert len(reader.pages) >= 2
    assert "Page 1" in text and "Page 2" in text
    assert "end" in text


def test_text_limit_runs_before_font_validation_and_does_not_truncate() -> None:
    """UTF-8 限额涵盖所有客户文本；超限不以删字或字体替代伪成功。"""
    from shared.schemas.quote_document import QuotePdfRenderError

    with pytest.raises(QuotePdfRenderError) as caught:
        _renderer(maximum_text_bytes=1).render(
            _customer_view(account_name="中文😀"), template_version="quote_pdf_v1"
        )

    assert caught.value.code == "text_limit_exceeded"


def test_unavailable_vera_glyph_is_rejected_without_replacement() -> None:
    """Vera 无字形时固定失败，不能 fallback、画方框或改写商业文字。"""
    from shared.schemas.quote_document import QuotePdfRenderError

    with pytest.raises(QuotePdfRenderError) as caught:
        _renderer().render(
            _customer_view(account_name="中文"), template_version="quote_pdf_v1"
        )

    assert caught.value.code == "unsupported_glyph"


def test_vera_mapped_non_ascii_character_renders_without_default_font() -> None:
    """实际 Vera glyph 映射中的非 ASCII 字符可渲染，不依赖默认字体。"""
    from connectors.quote_pdf.layout import load_fonts

    body_font, _ = load_fonts()
    character = next(
        chr(codepoint)
        for codepoint, glyph in body_font.face.charToGlyph.items()
        if codepoint > 127 and glyph and chr(codepoint).isprintable()
        and not chr(codepoint).isspace()
    )
    view = _customer_view(account_name=f"Buyer {character}")

    text = "\n".join(
        page.extract_text() or ""
        for page in PdfReader(BytesIO(_renderer().render(
            view, template_version="quote_pdf_v1"
        ))).pages
    )

    assert view.account_name in text


def test_missing_fixed_vera_file_returns_font_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """固定包内字体缺失时失败关闭，不能 fallback 到系统或默认字体。"""
    from connectors.quote_pdf import layout
    from shared.schemas.quote_document import QuotePdfRenderError

    monkeypatch.setattr(layout, "_font_path", lambda _: Path("/missing/Vera.ttf"))
    with pytest.raises(QuotePdfRenderError) as caught:
        _renderer().render(_customer_view(), template_version="quote_pdf_v1")

    assert caught.value.code == "font_unavailable"


def test_pdf_preserves_customer_newlines_and_consecutive_spaces() -> None:
    """客户商业文字的换行及连续空格在真实 PDF 提取中不被静默改写。"""
    specification = "First  line\nSecond   line"
    text = "\n".join(
        page.extract_text() or ""
        for page in PdfReader(BytesIO(_renderer().render(
            _customer_view(specification=specification), template_version="quote_pdf_v1"
        ))).pages
    )

    assert "First  line" in text
    assert "Second   line" in text


def test_text_limit_accepts_exact_utf8_total_and_rejects_one_less() -> None:
    """文本限额逐字段按 UTF-8 累计，不从最终 PDF 大小倒推。"""
    from connectors.quote_pdf.limits import customer_texts
    from shared.schemas.quote_document import QuotePdfRenderError

    view = _customer_view(approved_terms=("é", "term"))
    total = sum(len(value.encode("utf-8")) for value in customer_texts(view))

    assert _renderer(maximum_text_bytes=total).render(
        view, template_version="quote_pdf_v1"
    )
    with pytest.raises(QuotePdfRenderError) as caught:
        _renderer(maximum_text_bytes=total - 1).render(
            view, template_version="quote_pdf_v1"
        )

    assert caught.value.code == "text_limit_exceeded"


def test_page_limit_stops_before_a_second_page_is_drawn() -> None:
    """第 N+1 页开始前必须固定拒绝，不能在最终解析页数后才判断。"""
    from shared.schemas.quote_document import QuotePdfRenderError

    with pytest.raises(QuotePdfRenderError) as caught:
        _renderer(maximum_pages=1).render(
            _customer_view(approved_terms=(("approved term " * 500) + "end",)),
            template_version="quote_pdf_v1",
        )

    assert caught.value.code == "page_limit_exceeded"


def test_page_limit_uses_page_begin_hook_before_second_page_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """页数门禁真实运行在 ReportLab page-begin hook，不靠最终页数扫描。"""
    from connectors.quote_pdf.limits import LimitedSimpleDocTemplate
    from shared.schemas.quote_document import QuotePdfRenderError

    original = LimitedSimpleDocTemplate.handle_pageBegin
    calls: list[int] = []

    def record_page_begin(document: LimitedSimpleDocTemplate) -> None:
        calls.append(document.page)
        original(document)

    monkeypatch.setattr(LimitedSimpleDocTemplate, "handle_pageBegin", record_page_begin)
    with pytest.raises(QuotePdfRenderError) as caught:
        _renderer(maximum_pages=1).render(
            _customer_view(approved_terms=(("approved term " * 500) + "end",)),
            template_version="quote_pdf_v1",
        )

    assert caught.value.code == "page_limit_exceeded"
    assert len(calls) == 2


def test_byte_limit_accepts_exact_output_and_rejects_one_byte_less() -> None:
    """输出大小以真实 bytes 边界验收；失败不会返回部分 PDF。"""
    view = _customer_view()
    complete = _renderer().render(view, template_version="quote_pdf_v1")
    from shared.schemas.quote_document import QuotePdfRenderError

    assert _renderer(maximum_bytes=len(complete)).render(
        view, template_version="quote_pdf_v1"
    ) == complete
    with pytest.raises(QuotePdfRenderError) as caught:
        _renderer(maximum_bytes=len(complete) - 1).render(
            view, template_version="quote_pdf_v1"
        )

    assert caught.value.code == "byte_limit_exceeded"


def test_pdf_bytes_are_deterministic_across_instances_and_a_fresh_process() -> None:
    """固定 runtime/font/view/template 不得受实例缓存或先前文档影响。"""
    view = _customer_view()
    expected = _renderer().render(view, template_version="quote_pdf_v1")
    _renderer().render(
        _customer_view(description="Different customer text"),
        template_version="quote_pdf_v1",
    )
    assert _renderer().render(view, template_version="quote_pdf_v1") == expected

    program = (
        "from connectors.quote_pdf.client import ReportLabQuotePdfRenderer;"
        "from domains.quotations.service import project_customer;"
        "from tests.unit.test_quotation_contracts import detail_fixture;"
        "view=project_customer(detail_fixture());"
        "print(ReportLabQuotePdfRenderer(1000000,10,maximum_text_bytes=100000).render(view,template_version='quote_pdf_v1').hex())"
    )
    result = subprocess.run(
        [sys.executable, "-c", program], check=True, capture_output=True, text=True,
        env={"PYTHONPATH": str(Path.cwd()), "PYTHON_DOTENV_DISABLED": "1"},
    )

    assert bytes.fromhex(result.stdout.strip()) == expected


def _assert_pdf_has_no_actions(reader: PdfReader) -> None:
    """实际展开 PDF 对象图，拒绝动作、附件、链接和表单。"""
    rejected_types = {"/Action", "/EmbeddedFile", "/Filespec", "/RichMedia"}
    rejected_actions = {
        "/JavaScript", "/Launch", "/URI", "/GoTo", "/GoToR", "/Named",
        "/SubmitForm", "/ImportData",
    }
    seen: set[tuple[int, int] | int] = set()

    def walk(value: object) -> None:
        if isinstance(value, IndirectObject):
            identity = (value.idnum, value.generation)
            if identity in seen:
                return
            seen.add(identity)
            walk(value.get_object())
            return
        identity = id(value)
        if identity in seen:
            return
        seen.add(identity)
        if isinstance(value, DictionaryObject):
            assert not {"/A", "/AA", "/OpenAction", "/AF", "/Annots"} & set(value)
            assert str(value.get("/Type", "")) not in rejected_types
            assert str(value.get("/S", "")) not in rejected_actions
            for nested in value.values():
                walk(nested)
        elif isinstance(value, ArrayObject):
            for nested in value:
                walk(nested)

    walk(reader.trailer["/Root"])
    assert reader.attachments == {}
    assert reader.outline == []


def test_pdf_object_safety_detector_rejects_a_nested_indirect_action() -> None:
    """检测器先在纯内存反例上失败，避免只对安全输出空跑。"""
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    action = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Action"),
        NameObject("/S"): NameObject("/JavaScript"),
    }))
    writer._root_object[NameObject("/OpenAction")] = action
    payload = BytesIO()
    writer.write(payload)

    with pytest.raises(AssertionError):
        _assert_pdf_has_no_actions(PdfReader(BytesIO(payload.getvalue())))


def test_renderer_pdf_has_no_actions_attachments_or_internal_sentinel() -> None:
    """客户文本中的 HTML/URL 只能成为文本，PDF 不暴露内部资料。"""
    sentinel = "INTERNAL_SUPPLIER_MARGIN_SOURCE_SENTINEL"
    view = _customer_view(description='<a href="https://invalid.example">Widget</a>')
    reader = PdfReader(BytesIO(_renderer().render(view, template_version="quote_pdf_v1")))

    _assert_pdf_has_no_actions(reader)
    extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
    assert '<a href="https://invalid.example">Widget</a>' in extracted
    assert sentinel not in extracted
    assert sentinel not in str(reader.metadata)
