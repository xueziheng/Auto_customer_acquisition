"""ADR0070：受信模型调用身份、非秘密限额及实际用量；不含贸易业务规则。"""

from __future__ import annotations

import json
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.schemas.identifiers import AgentTurnId, EmployeeId, RunId, TenantId, UserId

PositiveInt = Annotated[int, Field(strict=True, gt=0)]
NonnegativeInt = Annotated[int, Field(strict=True, ge=0)]
ModelCapability = Literal[
    "product_help", "business_read", "research_proposal", "research", "model_probe"
]


class ModelDTO(BaseModel):
    """正文只显式交给授权调用栈，默认投影和校验错误不回显输入。"""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)


class InvocationIdentity(ModelDTO):
    """由认证与持久编排构造，模型不得提供这些字段。"""

    tenant_id: TenantId = Field(min_length=1, max_length=128)
    user_id: UserId = Field(min_length=1, max_length=128)
    employee_id: EmployeeId = Field(min_length=1, max_length=128)
    run_id: RunId = Field(min_length=1, max_length=128)
    turn_id: AgentTurnId | None = Field(default=None, min_length=1, max_length=128)
    capability: ModelCapability
    configuration_version: str = Field(min_length=1, max_length=128)
    sequence: NonnegativeInt

    @field_validator(
        "tenant_id",
        "user_id",
        "employee_id",
        "run_id",
        "turn_id",
        "configuration_version",
    )
    @classmethod
    def clean_identity(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError("调用身份无效")
        return value


class ModelLimits(ModelDTO):
    """部署方必须逐项提供，不能用测试阈值开启付费服务。"""

    window_seconds: PositiveInt
    tenant_calls: PositiveInt
    employee_calls: PositiveInt
    tenant_concurrency: PositiveInt
    employee_concurrency: PositiveInt
    max_input_bytes: PositiveInt
    max_output_tokens: PositiveInt
    timeout_seconds: PositiveInt


class ModelRequest(ModelDTO):
    """无身份/凭证的单次 JSON 请求，实际限长由已验证部署配置决定。"""

    model: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._/-]+$")
    system_prompt: str = Field(min_length=1, repr=False, exclude=True)
    payload: dict[str, object] = Field(repr=False, exclude=True)
    max_output_tokens: PositiveInt

    @field_validator("payload")
    @classmethod
    def json_only(cls, value: dict[str, object]) -> dict[str, object]:
        try:
            json.dumps(value, allow_nan=False, ensure_ascii=False)
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise ValueError("模型输入必须是有限 JSON") from None
        return value


class ModelUsage(ModelDTO):
    """Provider 缺失计量保留未知，不推算或补零。"""

    input_tokens: NonnegativeInt | None
    cached_input_tokens: NonnegativeInt | None
    output_tokens: NonnegativeInt | None

    @model_validator(mode="after")
    def check_cache(self) -> ModelUsage:
        if (
            self.input_tokens is not None
            and self.cached_input_tokens is not None
            and self.cached_input_tokens > self.input_tokens
        ):
            raise ValueError("缓存用量超过输入用量")
        return self


class ModelResponse(ModelDTO):
    """仅交付最终文本；thinking 不在本契约中。"""

    text: str = Field(min_length=1, repr=False, exclude=True)
    model: str = Field(min_length=1, max_length=128)
    usage: ModelUsage


class ModelGenerationPort(Protocol):
    """受信上层唯一的模型出口，由 Tool Gateway 实现。"""

    async def generate(
        self, identity: InvocationIdentity, request: ModelRequest
    ) -> ModelResponse:
        """重核权限、预留额度后调用；未知执行不能自动重试。"""
        ...
