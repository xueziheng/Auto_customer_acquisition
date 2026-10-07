"""报价运行配置的纯解析；只处理非秘密值，不读取环境、文件或凭证。"""

import json
from collections.abc import Mapping
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from infra.quote_evidence_settings import QuoteEvidenceSettings
from infra.quote_evidence_settings import from_mapping as evidence_from_mapping
from shared.schemas.evidence_read import ObjectReadLimits
from shared.schemas.generated_documents import QuotePdfWriteLimits
from shared.schemas.quote_files import QUOTE_PDF_TEMPLATE_VERSIONS
from tool_gateway.file_rate_limit import QuoteFileRateLimits

Positive = Annotated[int, Field(gt=0)]
DatabasePositive = Annotated[int, Field(gt=0, le=2147483647)]


class QuotationConfigurationError(ValueError):
    """不携配置JSON或底层验证输入，只暴露固定类型。"""

    def __init__(self) -> None:
        super().__init__("报价运行配置无效")


class _Settings(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class QuotationCoreSettings(_Settings):
    """数据库及驱动预算显式输入，不含业务默认。"""

    lock_timeout_ms: DatabasePositive
    statement_timeout_ms: DatabasePositive
    maximum_page_size: DatabasePositive
    expiry_batch_limit: Annotated[int, Field(ge=1, le=1000)]


class QuoteFileRuntimeSettings(_Settings):
    """文件组只整体启用，实际读写和渲染使用各自明确预算。"""

    template_version: str
    maximum_bytes: Positive
    maximum_pages: Positive
    maximum_text_bytes: Positive
    object_read: ObjectReadLimits
    object_write: QuotePdfWriteLimits
    rate_limit: QuoteFileRateLimits
    gateway_lease_seconds: Annotated[int, Field(ge=1, le=86400)]

    @model_validator(mode="after")
    def valid_runtime(self) -> Self:
        """模板沿唯一登记集合，Postgres毫秒预算不越整型边界。"""
        if (
            self.template_version not in QUOTE_PDF_TEMPLATE_VERSIONS
            or max(
                self.rate_limit.lock_timeout_ms, self.rate_limit.statement_timeout_ms
            )
            > 2147483647
        ):
            raise ValueError("报价文件配置无效")
        return self


class QuotationRuntimeSettings(_Settings):
    """core/evidence必需，files必须显式为完整配置或None。"""

    core: QuotationCoreSettings
    evidence: QuoteEvidenceSettings
    files: QuoteFileRuntimeSettings | None

    @field_validator("evidence", mode="before")
    @classmethod
    def evidence_contract(cls, value: object) -> object:
        """复用A原解析及所有嵌套校验，不复制预算或平台选择。"""
        return evidence_from_mapping(value) if isinstance(value, Mapping) else value


def from_mapping(value: Mapping[str, object]) -> QuotationRuntimeSettings:
    """只解析给定值；任何非法配置固定失败，不降级为关闭。"""
    try:
        if not isinstance(value, Mapping):
            raise TypeError
        return QuotationRuntimeSettings.model_validate(dict(value))
    except (ValueError, TypeError):
        raise QuotationConfigurationError() from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _nonfinite(value: str) -> object:
    raise ValueError


def from_json(value: str) -> QuotationRuntimeSettings:
    """两个进程共用纯JSON解码，递归拒重复key及非有限数。"""
    try:
        if type(value) is not str:
            raise ValueError
        decoded = json.loads(
            value, object_pairs_hook=_unique_object, parse_constant=_nonfinite
        )
        return from_mapping(decoded)
    except (ValueError, TypeError):
        raise QuotationConfigurationError() from None
