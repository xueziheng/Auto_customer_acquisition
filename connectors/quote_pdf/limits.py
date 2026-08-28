"""离线报价 PDF 的输入、页数与输出边界。"""

from __future__ import annotations

from io import BytesIO
from typing import Iterable

from reportlab.platypus import SimpleDocTemplate

from shared.schemas.quote_document import CustomerQuoteView, QuotePdfRenderError


class PageLimitExceeded(Exception):
    """内部页数停止信号；调用方统一转换为脱敏固定错误。"""


class BoundedBytesIO(BytesIO):
    """限制最终 PDF sink 的最大写入 extent，不保证 ReportLab 的进程 RSS。"""

    def __init__(self, maximum_bytes: int) -> None:
        super().__init__()
        self._maximum_bytes = maximum_bytes
        self._maximum_extent = 0

    def write(self, value: bytes) -> int:
        """在底层 buffer 扩展前拒绝越界写入。"""
        end = self.tell() + len(value)
        if end > self._maximum_bytes:
            raise QuotePdfRenderError("byte_limit_exceeded")
        self._maximum_extent = max(self._maximum_extent, end)
        return super().write(value)

    def read_result(self) -> bytes:
        """再次核最终大小；不返回任何截断成功结果。"""
        result = self.getvalue()
        if len(result) > self._maximum_bytes or self._maximum_extent > self._maximum_bytes:
            raise QuotePdfRenderError("byte_limit_exceeded")
        return result


class LimitedSimpleDocTemplate(SimpleDocTemplate):
    """在第 N+1 页绘制前中止的单用途 ReportLab 文档。"""

    def __init__(self, *args: object, maximum_pages: int, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._maximum_pages = maximum_pages

    def handle_pageBegin(self) -> None:  # noqa: N802 - ReportLab hook 名称
        """页模板/页脚绘制前检查，不能等解析完整 PDF 后才拒绝。"""
        if self.page >= self._maximum_pages:
            raise PageLimitExceeded
        super().handle_pageBegin()


def customer_texts(view: CustomerQuoteView) -> tuple[str, ...]:
    """按实际渲染字段顺序枚举客户文本，不拼接、修剪或重写。"""
    if not isinstance(view, CustomerQuoteView):
        raise QuotePdfRenderError("invalid_input")
    values: tuple[object, ...] = (
        view.quote_id,
        str(view.version),
        view.issuer_name,
        view.issuer_address,
        view.issuer_contact,
        view.account_name,
        view.description,
        view.specification,
        view.unit,
        view.quantity_display,
        view.unit_price_display,
        view.total_display,
        view.currency,
        view.valid_until_display,
        *view.approved_terms,
    )
    if any(not isinstance(value, str) for value in values):
        raise QuotePdfRenderError("invalid_input")
    return tuple(values)  # type: ignore[return-value]


def validate_customer_text(
    view: CustomerQuoteView, *, maximum_text_bytes: int
) -> tuple[str, ...]:
    """在 story 前累计 UTF-8 上限与可见控制字符；超限不截断。"""
    remaining = maximum_text_bytes
    values = customer_texts(view)
    for value in values:
        if any(ord(character) < 32 and character != "\n" or ord(character) == 127 for character in value):
            raise QuotePdfRenderError("invalid_input")
        if len(value) > remaining:
            raise QuotePdfRenderError("text_limit_exceeded")
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            raise QuotePdfRenderError("invalid_input") from None
        if len(encoded) > remaining:
            raise QuotePdfRenderError("text_limit_exceeded")
        remaining -= len(encoded)
    return values


def all_visible_text(values: Iterable[str]) -> Iterable[str]:
    """排除明确排版换行，仅向字体检查提供可见字符。"""
    for value in values:
        yield value.replace("\n", "")
