"""客户报价唯一白名单DTO；不证明访问权限或条款已获批准。"""

from pydantic import BaseModel, ConfigDict, Field


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
