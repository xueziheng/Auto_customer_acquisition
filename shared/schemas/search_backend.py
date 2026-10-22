"""平台控制的搜索后端许可；网站证据来源与检索后端不是同一对象。"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class InventorySearchPolicy(BaseModel):
    """仅可信部署配置可提供，缺许可不能消费充值余额。"""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)
    version: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")
    backend_id: Literal["deepseek", "tavily"] = "deepseek"
    paid_search_enabled: bool = Field(default=False, strict=True)
    maximum_requests: int = Field(default=10, strict=True, ge=1, le=24)
    maximum_cost_cny: Decimal = Field(default=Decimal(0), ge=0, le=1)

    @field_validator("maximum_cost_cny", mode="before")
    @classmethod
    def reject_float(cls, value: object) -> object:
        if isinstance(value, (float, bool)):
            raise ValueError("搜索金额必须使用Decimal或十进制字符串")  # noqa: TRY004 -- Pydantic输入校验。
        return value


class SearchBackendPin(BaseModel):
    """任务恢复必须使用原后端、连接与政策，不能换Key重派未知来源。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    backend_id: Literal["deepseek", "tavily"]
    connection_version: str = Field(min_length=1, max_length=128)
    policy_version: str = Field(min_length=1, max_length=128)


class SearchBackendReadiness(BaseModel):
    """可调度资格和实际搜索执行分开；版本只是安全元数据，不是Key引用。"""
    model_config = ConfigDict(frozen=True, extra="forbid")
    backend_id: Literal["deepseek"] = "deepseek"
    connection_version: str | None = None
    policy_version: str
    connection_status: Literal["not_configured", "not_run", "passed", "failed"] = "not_configured"
    search_execution_state: Literal["not_run", "running", "succeeded", "failed", "unknown"] = "not_run"
