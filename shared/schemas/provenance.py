"""来源追踪（Provenance）。

硬边界 4：**所有影响商业决策的字段都必须能回答"这个信息从哪来"。**

验收标准很具体：老板在界面上点"为什么判断这是高意向"，必须能一路
点到原始证据（某条消息、某个网页快照、某份上传文件）。做不到这点的
字段，等于没有这个字段——因为没人敢用它做决定。

硬边界 5 的配套要求：**事实与推断在数据结构上分离**，不靠字段命名
约定。见 ``FactualField`` 与 ``InferredField``。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from pydantic import AwareDatetime, BaseModel, ConfigDict, field_validator

from shared.errors import ValidationError
from shared.schemas.evidence import EvidenceItem
from shared.schemas.identifiers import EmployeeId


class SourceType(str, Enum):
    """来源类型。"""

    CONVERSATION = "conversation"
    """来自与客户的会话消息。最可靠的需求来源。"""

    WEB_PAGE = "web_page"
    """来自公开网页。必须配套记录 URL、观察时间、页面哈希——
    网页会变，没有哈希就无法证明当时看到的是什么。"""

    UPLOAD = "upload"
    """来自员工上传的文件（截图、PDF、规格表）。"""

    EMPLOYEE_INPUT = "employee_input"
    """员工直接录入。"""

    AGENT_INFERENCE = "agent_inference"
    """Agent 推断。**必须指向它依据的事实**，无依据的推断会被
    ``agent_runtime.guardrails`` 拦下。"""

    EXTERNAL_API = "external_api"
    """来自外部数据 API（联系人补全、供应商数据）。记录哪个 provider，
    便于评估数据源质量和成本。"""


def _is_blank(value: str | None) -> bool:
    """None 或 strip 后为空都视为未填写。"""
    return value is None or not value.strip()


@dataclass(frozen=True)
class Provenance:
    """一个字段值的来源记录。

    字段：
        source_type:   来源类型
        source_id:     来源标识（消息 ID / 页面哈希 / 上传 ID）
        extracted_by:  提取者（模型版本标识或 ``"human"``）
        extracted_at:  提取时间
        confirmed_by:  确认人。``None`` 表示尚未经人工确认
        confirmed_at:  确认时间
        source_url:    来源 URL，网页类必填
        page_hash:     页面内容哈希，网页类必填
        source_quote:  客户/员工原文逐字摘录；事实提取可选，禁止空串

    ``extracted_by`` 要记具体的模型版本标识，不要只写 ``"model"``——
    换模型后需要能分开评估提取质量。
    """

    source_type: SourceType
    source_id: str
    extracted_by: str
    extracted_at: datetime
    confirmed_by: EmployeeId | None = None
    confirmed_at: datetime | None = None
    source_url: str | None = None
    page_hash: str | None = None
    source_quote: str | None = None

    def __post_init__(self) -> None:
        """校验：

        - ``source_id`` / ``extracted_by`` strip 后非空
        - ``source_type == WEB_PAGE`` 时 ``source_url`` 与 ``page_hash``
          均须提供（非 None 且 strip 后非空）
        - ``confirmed_by`` 与 ``confirmed_at`` 必须同时有或同时无
        """
        if _is_blank(self.source_id):
            raise ValidationError("source_id 不能为空")
        if _is_blank(self.extracted_by):
            raise ValidationError("extracted_by 不能为空")
        if self.source_type == SourceType.WEB_PAGE and (
            _is_blank(self.source_url) or _is_blank(self.page_hash)
        ):
            raise ValidationError("WEB_PAGE 来源必须同时提供 source_url 与 page_hash")
        if (self.confirmed_by is None) != (self.confirmed_at is None):
            raise ValidationError("confirmed_by 与 confirmed_at 必须同时有或同时无")
        if self.source_quote is not None and not self.source_quote.strip():
            raise ValidationError("source_quote 不得为空")

    @property
    def is_human_confirmed(self) -> bool:
        """是否经人工确认。

        用于判断能否用于对外承诺——未经确认的模型提取结果不能直接
        进客户可见报价。
        """
        return self.confirmed_by is not None


class ProvenanceSummary(BaseModel):
    """来源的安全HTTP形状；不包含原文、URL、定位或原件读权。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    source_type: SourceType
    source_id: str
    extracted_by: str
    extracted_at: AwareDatetime
    confirmed_by: EmployeeId | None
    confirmed_at: AwareDatetime | None

    @field_validator("extracted_at", "confirmed_at")
    @classmethod
    def _summary_utc(cls, value: datetime | None) -> datetime | None:
        """只统一安全摘要的时区，不改变原Provenance契约。"""
        from shared.schemas.quote_facts import fact_utc

        return fact_utc(value) if value is not None else None

    @field_validator("confirmed_by")
    @classmethod
    def _summary_identity(cls, value: EmployeeId | None) -> EmployeeId | None:
        """保留现员工身份形状；摘要不授予访问原件的能力。"""
        from shared.schemas.quote_facts import fact_identity

        if value is not None:
            fact_identity(value)
        return value


def summarize_provenance(value: Provenance) -> ProvenanceSummary:
    """显式白名单，不递归转储完整来源后再做排除。"""
    return ProvenanceSummary(source_type=value.source_type, source_id=value.source_id,
        extracted_by=value.extracted_by, extracted_at=value.extracted_at,
        confirmed_by=value.confirmed_by, confirmed_at=value.confirmed_at)


@dataclass(frozen=True)
class FactualField[T]:
    """事实字段：直接观察到的内容。

    例：客户在消息里写了 "we need 5000 units"，则 quantity 是事实。

    ``provenance.source_type`` 不得为 ``AGENT_INFERENCE``——那属于
    ``InferredField``。这个约束由 ``__post_init__`` 强制，不靠自觉。
    """

    value: T
    provenance: Provenance

    def __post_init__(self) -> None:
        """事实字段不得来自 Agent 推断——那属于 ``InferredField``。"""
        if self.provenance.source_type == SourceType.AGENT_INFERENCE:
            raise ValidationError("FactualField 不得使用 AGENT_INFERENCE 来源（属 InferredField）")


@dataclass(frozen=True)
class InferredField[T]:
    """推断字段：由事实推出的结论。

    例：客户新增户外家具产品线（事实）→ 可能需要耐腐蚀五金（推断）。

    字段：
        value:         推断内容
        based_on:      依据的证据列表，**不得为空**
        inferred_by:   推断者（模型版本或 ``"human"``）
        inferred_at:   推断时间

    注意这里**没有 confidence 数值字段**（硬边界 3）。置信度由
    ``evidence.derive_confidence`` 依据 ``based_on`` 推导，需要时
    现算，不存小数。

    反例（禁止）：把"这家公司正在扩张，所以一定要采购我们的产品"
    作为一个字段值——它把事实和推断焊在一句话里，无法分别核对。
    """

    value: T
    based_on: list[EvidenceItem]
    inferred_by: str
    inferred_at: datetime

    def __post_init__(self) -> None:
        """``based_on`` 非空。空推断说明调用方有 bug。"""
        if not self.based_on:
            raise ValidationError("based_on 不能为空：推断必须指向证据")


@dataclass(frozen=True)
class ExtractionChain[T]:
    """提取链条。

    员工上传的资料经模型提取后，必须保留完整链条，**不能只存最终版本**：

    ```text
    原始资料 → Agent 提取版本 → 员工修改版本 → 最终确认版本
    ```

    这样才能回答"这个数量是谁改的"，也才能评估模型提取质量随时间
    的变化。只存最终版本会让这两件事都做不到。

    字段：
        raw_artifact_id:  原始资料 ID（不可变，永不覆盖）
        agent_extracted:  Agent 提取的值
        employee_edited:  员工修改后的值，``None`` 表示未修改
        final_value:      最终确认值
        edited_by:        修改人
        edited_at:        修改时间
    """

    raw_artifact_id: str
    agent_extracted: T
    final_value: T
    employee_edited: T | None = None
    edited_by: EmployeeId | None = None
    edited_at: datetime | None = None

    def __post_init__(self) -> None:
        """编辑三元组必须同有同无（全 None 或全非 None）。"""
        edit_fields = [self.employee_edited, self.edited_by, self.edited_at]
        if any(f is None for f in edit_fields) and any(f is not None for f in edit_fields):
            raise ValidationError("employee_edited/edited_by/edited_at 必须同时有或同时无")
