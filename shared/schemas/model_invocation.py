"""ADR0070：受信模型调用身份、非秘密限额及实际用量；不含贸易业务规则。"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from shared.schemas.identifiers import AgentTurnId, EmployeeId, RunId, TenantId, UserId

PositiveInt = Annotated[int, Field(strict=True, gt=0)]
NonnegativeInt = Annotated[int, Field(strict=True, ge=0)]
ModelCapability = Literal[
    "product_help",
    "business_read",
    "research_proposal",
    "research",
    "model_probe",
    "reply_qualification",
    "knowledge_ingest",
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


MAX_MODEL_IMAGE_BYTES = 8 * 1024 * 1024
MAX_MODEL_IMAGES_BYTES = 16 * 1024 * 1024
MAX_MODEL_IMAGES = 10


class ModelInputImage(ModelDTO):
    """进程内受信图像；仅适配器显式编码，默认投影不包含原件。"""

    mime_type: Literal["image/png", "image/jpeg", "image/webp"]
    data: bytes = Field(strict=True, repr=False, exclude=True, min_length=1, max_length=MAX_MODEL_IMAGE_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(strict=True, gt=0, le=MAX_MODEL_IMAGE_BYTES)

    @model_validator(mode="after")
    def validate_content(self) -> ModelInputImage:
        magic_matches = (
            (self.mime_type == "image/png" and self.data.startswith(b"\x89PNG\r\n\x1a\n"))
            or (self.mime_type == "image/jpeg" and self.data.startswith(b"\xff\xd8\xff"))
            or (self.mime_type == "image/webp" and self.data.startswith(b"RIFF") and self.data[8:12] == b"WEBP")
        )
        if (
            self.byte_length != len(self.data)
            or self.sha256 != hashlib.sha256(self.data).hexdigest()
            or not magic_matches
        ):
            raise ValueError("模型图像内容与元数据不一致")
        return self


class ModelRequest(ModelDTO):
    """无身份/凭证的请求；文本限额来自部署配置，图像另有固定容量上限。"""

    model: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._/-]+$")
    system_prompt: str = Field(min_length=1, repr=False, exclude=True)
    payload: dict[str, object] = Field(repr=False, exclude=True)
    max_output_tokens: PositiveInt
    images: tuple[ModelInputImage, ...] = Field(default=(), repr=False, exclude=True, max_length=MAX_MODEL_IMAGES)

    @model_validator(mode="after")
    def bounded_images(self) -> ModelRequest:
        if sum(image.byte_length for image in self.images) > MAX_MODEL_IMAGES_BYTES:
            raise ValueError("模型图像总大小超过上限")
        return self

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


ModelFailureCode = Literal[
    "permission",
    "configuration",
    "quota",
    "authentication",
    "insufficient_balance",
    "invalid_request",
    "rate_limit",
    "provider_error",
    "invalid_response",
    "output_limit",
    "unknown",
]


class ModelGenerationError(RuntimeError):
    """固定安全失败分类；不能据此自动重新发送可能已经执行的请求。"""

    def __init__(self, code: ModelFailureCode) -> None:
        super().__init__("模型调用未能交付可用结果")
        self.code = code
