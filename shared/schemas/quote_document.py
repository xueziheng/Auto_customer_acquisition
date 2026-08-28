"""客户报价唯一白名单DTO；不证明访问权限或条款已获批准。"""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from shared.errors import ConnectorError


class CustomerQuoteView(BaseModel):
    """渲染器共享纯展示形状，不携带任何成本或来源内部字段。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    quote_id: str
    version: int = Field(gt=0)
    issuer_name: str
    issuer_address: str
    issuer_contact: str
    account_name: str
    description: str
    specification: str
    unit: str
    quantity_display: str
    unit_price_display: str
    total_display: str
    currency: str
    valid_until_display: str
    approved_terms: tuple[str, ...]


def customer_quote_hash(view: CustomerQuoteView) -> str:
    """显式客户白名单的规范hash，不改展示字符串或条款顺序。"""
    customer = {
        "quote_id": view.quote_id,
        "version": view.version,
        "issuer_name": view.issuer_name,
        "issuer_address": view.issuer_address,
        "issuer_contact": view.issuer_contact,
        "account_name": view.account_name,
        "description": view.description,
        "specification": view.specification,
        "unit": view.unit,
        "quantity_display": view.quantity_display,
        "unit_price_display": view.unit_price_display,
        "total_display": view.total_display,
        "currency": view.currency,
        "valid_until_display": view.valid_until_display,
        "approved_terms": list(view.approved_terms),
    }
    encoded = json.dumps(
        {"version": "customer-quote-v1", "customer": customer},
        sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


QuotePdfRenderErrorCode = Literal[
    "invalid_config",
    "invalid_input",
    "template_unsupported",
    "text_limit_exceeded",
    "page_limit_exceeded",
    "byte_limit_exceeded",
    "font_unavailable",
    "unsupported_glyph",
    "layout_failed",
    "render_failed",
]


class QuotePdfRenderError(ConnectorError):
    """PDF 渲染跨层固定错误，不携带客户内容、路径或运行时异常。"""

    is_retryable = False

    _MESSAGES: dict[QuotePdfRenderErrorCode, str] = {
        "invalid_config": "PDF配置无效",
        "invalid_input": "客户视图无效",
        "template_unsupported": "模板未注册",
        "text_limit_exceeded": "客户文本超限",
        "page_limit_exceeded": "PDF页数超限",
        "byte_limit_exceeded": "PDF字节超限",
        "font_unavailable": "PDF字体不可用",
        "unsupported_glyph": "字体不支持客户字符",
        "layout_failed": "PDF排版失败",
        "render_failed": "PDF渲染失败",
    }

    def __init__(self, code: QuotePdfRenderErrorCode) -> None:
        """以受限错误码构造，不允许调用方注入任意错误文本。"""
        if code not in self._MESSAGES:
            raise ValueError("无效PDF渲染错误码")
        self.code = code
        super().__init__(self._MESSAGES[code])
