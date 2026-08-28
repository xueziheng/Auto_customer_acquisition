"""ReportLab 离线报价 PDF renderer 的受限入口。"""

from __future__ import annotations

from connectors.base import ConnectorManifest
from connectors.quote_pdf.manifest import MANIFEST
from shared.schemas.quote_document import CustomerQuoteView, QuotePdfRenderError


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
        """后续仅检查固定本地依赖，不生成 PDF 或访问网络。"""
        return True

    def render(self, view: CustomerQuoteView, *, template_version: str) -> bytes:
        """实际排版由后续切片实现；当前不接受内部报价或自由输入。"""
        raise NotImplementedError
