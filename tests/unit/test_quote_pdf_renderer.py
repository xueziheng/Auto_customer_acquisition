"""离线客户报价 PDF 的公开契约与真实渲染验收。"""

from __future__ import annotations

from typing import get_type_hints

import pytest

from domains.quotations import service as quotations_service
from domains.quotations.errors import QuotationError
from shared.schemas.quote_document import CustomerQuoteView
from tests.unit.test_quotation_contracts import detail_fixture


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
