"""离线客户报价 PDF 的公开契约与真实渲染验收。"""

from __future__ import annotations

from io import BytesIO
from typing import get_type_hints

import pytest
from pypdf import PdfReader

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
