"""供应商额度的安全事实；不承载账户身份、密钥、原始响应或金额。"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

QuotaNumber = Annotated[Decimal, Field(ge=0, le=Decimal(1000000000000), allow_inf_nan=False)]


class ProviderUsageCounter(BaseModel):
    """remaining 是供应商明确返回的余额，缺失不根据套餐总量猜测。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["credits", "searches", "verifications"]
    used: QuotaNumber | None = None
    limit: QuotaNumber | None = None
    remaining: QuotaNumber | None = None


class ProviderUsageObservation(BaseModel):
    """一个配置版本的一次账户检查结果；非成功结果不能携带旧额度冒充最新值。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    outcome: Literal["success", "credential_invalid", "payment_required", "rate_limited", "unavailable", "unknown"]
    checked_at: AwareDatetime
    counters: tuple[ProviderUsageCounter, ...] = Field(default=(), max_length=3)
    reset_date: date | None = None
    retry_after_seconds: int | None = Field(default=None, ge=1, le=3600)


class DataSourceUsageView(BaseModel):
    """管理员额度投影；检查成功、额度可用和业务可用是不同事实。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    status: Literal["not_checked", "available", "low", "partial_exhausted", "exhausted", "credential_invalid", "payment_required", "rate_limited", "unavailable", "unknown"] = "not_checked"
    checked_at: datetime | None = None
    counters: tuple[ProviderUsageCounter, ...] = ()
    reset_date: date | None = None
    next_check_at: datetime | None = None
    stale: bool = False
