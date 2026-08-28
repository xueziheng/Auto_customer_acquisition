"""来源非秘密配置的纯解析；不读取环境、文件或凭证。"""

from collections.abc import Mapping
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from shared.schemas.evidence_read import (
    EvidenceParseLimits,
    EvidenceProbeLimits,
    ObjectReadLimits,
)


class QuoteEvidenceSettings(BaseModel):
    """全部限额由部署显式提供，无隐式启用或生产默认值。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    raw_maximum_bytes: Annotated[int, Field(gt=0)]
    object_read: ObjectReadLimits
    parser: EvidenceParseLimits
    probe: EvidenceProbeLimits


def from_mapping(values: Mapping[str, object]) -> QuoteEvidenceSettings:
    """纯映射解析，未知字段和非严格值拒绝。"""
    return QuoteEvidenceSettings.model_validate(dict(values))
