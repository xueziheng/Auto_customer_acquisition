"""ReportLab 离线报价 PDF renderer 的受限入口。"""

from __future__ import annotations

from connectors.base import ConnectorManifest
from connectors.quote_pdf.layout import load_fonts, render_pdf, validate_glyphs
from connectors.quote_pdf.limits import validate_customer_text
from connectors.quote_pdf.manifest import MANIFEST
from shared.schemas.quote_document import CustomerQuoteView, QuotePdfRenderError
from shared.schemas.quote_files import QUOTE_PDF_TEMPLATE_VERSIONS


class ReportLabQuotePdfRenderer:
    """只接收客户白名单 DTO 的离线渲染能力。"""

    manifest: ConnectorManifest = MANIFEST

    def __init__(
        self, maximum_bytes: int, maximum_pages: int, *, maximum_text_bytes: int
    ) -> None:
        """验证三个部署上限；未配置或错误配置时拒绝构造能力。"""
        for value in (maximum_bytes, maximum_pages, maximum_text_bytes):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise QuotePdfRenderError("invalid_config")
        self._maximum_bytes = maximum_bytes
        self._maximum_pages = maximum_pages
        self._maximum_text_bytes = maximum_text_bytes

    async def configure(self, secret_resolver: object) -> None:
        """离线 connector 不读取密钥；保留统一 Connector 生命周期接口。"""

    async def health_check(self) -> bool:
        """仅核固定本地字体可用，不生成 PDF、访问网络或读取密钥。"""
        try:
            load_fonts()
        except Exception:  # noqa: BLE001 - 离线健康检查只返回依赖可用性
            return False
        return True

    def render(self, view: CustomerQuoteView, *, template_version: str) -> bytes:
        """严格按输入、模板、文本、字体、页数和输出顺序完成离线渲染。"""
        if not isinstance(view, CustomerQuoteView):
            raise QuotePdfRenderError("invalid_input")
        if template_version not in QUOTE_PDF_TEMPLATE_VERSIONS:
            raise QuotePdfRenderError("template_unsupported")
        values = validate_customer_text(
            view, maximum_text_bytes=self._maximum_text_bytes
        )
        try:
            fonts = load_fonts()
        except QuotePdfRenderError:
            raise
        except Exception:  # noqa: BLE001 - 字体以外异常固定脱敏，系统退出不在 Exception 内
            raise QuotePdfRenderError("render_failed") from None
        validate_glyphs(values, fonts)
        return render_pdf(
            view, maximum_bytes=self._maximum_bytes, maximum_pages=self._maximum_pages
        )
