"""离线客户报价 PDF connector 的注册元数据。"""

from connectors.base import ConnectorManifest

MANIFEST = ConnectorManifest(
    connector_id="quote_pdf",
    capabilities=("quotation.pdf.render",),
    secret_refs=(),
    rate_limit_note="文本、页数和字节上限由部署配置与 Gateway 设定。",
    compliance_note="离线渲染；不授予报价授权、不发送、不读取凭证。",
)

__all__ = ("MANIFEST",)
