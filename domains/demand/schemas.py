"""需求域对外 DTO。

跨域和对 API 暴露的形状。与 ``models.py`` 分开的原因：
实体字段会因存储需要而变化，DTO 是承诺给外部的形状，两者变化节奏不同。

**View 类必须携带 provenance 摘要**——前端要能展示"这个数字从哪来"，
这是硬边界 4 的落地出口。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from domains.demand.http_schemas import (
    NeedUnitConfirmationPublicView as NeedUnitConfirmationPublicView,  # noqa: PLC0414 - 同类型公开重导出
)
from domains.demand.http_schemas import (
    NeedUnitPreparationView as NeedUnitPreparationView,  # noqa: PLC0414 - 同类型公开重导出
)
from shared.schemas.evidence import EvidenceLevel
from shared.schemas.identifiers import (
    ArtifactId,
    ContactPointId,
    EmployeeId,
    EnrollmentId,
    MessageId,
    NeedClusterId,
    NeedHypothesisId,
    OpportunityId,
    OutboundMessageId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.money import Money
from shared.schemas.provenance import FactualField, SourceType
from shared.schemas.quote_facts import NeedFactDTO, QuoteEmployeeFact
from shared.schemas.quote_facts import (
    NeedQuoteFacts as NeedQuoteFacts,  # noqa: PLC0414 - 保持公开类型或测试fixture身份
)


class NeedUnitScopeFacts(NeedFactDTO):
    """单位用途的锁内事实，不附带文件scope或owner在职前置。"""

    tenant_id: TenantId
    need_id: ValidatedNeedId
    opportunity_id: OpportunityId
    account_id: ProspectAccountId
    actor: QuoteEmployeeFact

NeedUnitAction = Literal["read", "confirm"]
NeedUnitErrorCode = Literal[
    "invalid_input", "unit_unspecified", "quantity_invalid", "source_mismatch",
    "source_unsupported", "need_not_found", "confirmation_not_found", "need_terminal",
    "quantity_changed", "unit_changed", "idempotency_conflict", "unit_missing",
    "unit_stale", "fact_unconfirmed", "permission_denied", "source_unavailable",
    "dependency_unavailable", "lock_timeout", "storage_unknown", "facts_corrupt",
]


def _unit_text(value: str) -> str:
    """输入不静默去空白或控制字符，摘录单独允许换行。"""
    if value != value.strip() or not value or any(
        ord(c) < 32 or 127 <= ord(c) < 160 for c in value
    ):
        raise ValueError("字符串必须非空且不含首尾空白或控制字符")
    return value


def _unit_quote(value: str) -> str:
    """摘录仅放行换行，保留逐字内容。"""
    _unit_text(value.replace("\n", " "))
    if value != value.strip():
        raise ValueError("摘录不能有首尾空白")
    return value


def _unit_utc(value: datetime) -> datetime:
    """时间必须带时区，持久与哈希统一UTC。"""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(UTC)


def _unit_identity(value: object) -> None:
    """仅新单位契约的身份ID使用40字符边界，不约束旧多态来源/提取者标签。"""
    if type(value) is not str or len(value) > 40:
        raise ValueError("标识必须为不超过40字符的字符串")
    _unit_text(value)


_UnitText = Annotated[str, Field(min_length=1, max_length=64), AfterValidator(_unit_text)]
_UnitLocator = Annotated[str, Field(min_length=1, max_length=256), AfterValidator(_unit_text)]
_UnitQuote = Annotated[str, Field(min_length=1, max_length=4096), AfterValidator(_unit_quote)]
_UnitHash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_UnitTime = Annotated[datetime, AfterValidator(_unit_utc)]
_UnitId = Annotated[str, Field(min_length=1, max_length=40), AfterValidator(_unit_text)]


class _NeedUnitDTO(BaseModel):
    """新单位契约严格、不可变，不放宽旧DTO。"""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    @model_validator(mode="after")
    def _metadata(self) -> Self:
        """顶层与事实确认人ID同样校验；完整Provenance时间统一UTC。"""
        for name in type(self).model_fields:
            value = getattr(self, name)
            if value is not None and (name.endswith("_id") or name == "confirmed_by"):
                _unit_identity(value)
            if isinstance(value, FactualField):
                provenance = value.provenance
                if provenance.confirmed_by is not None:
                    _unit_identity(provenance.confirmed_by)
                updated = replace(provenance,
                    extracted_at=_unit_utc(provenance.extracted_at),
                    confirmed_at=_unit_utc(provenance.confirmed_at) if provenance.confirmed_at else None)
                object.__setattr__(self, name, replace(value, provenance=updated))
        return self


class NeedUnitConfirmationCommand(_NeedUnitDTO):
    """人工明确口径，确认身份只能由服务端绑定。"""

    unit: _UnitText
    source_message_id: Annotated[MessageId, Field(min_length=1, max_length=40),
                                 AfterValidator(_unit_text)]
    locator: _UnitLocator
    source_quote: _UnitQuote
    expected_quantity_fact_hash: _UnitHash
    expected_unit_confirmation_id: _UnitId | None


class NeedUnitAccess(_NeedUnitDTO):
    """当前员工与机会范围授权，不存可伪造allowed标记。"""

    tenant_id: Annotated[TenantId, Field(min_length=1, max_length=40), AfterValidator(_unit_text)]
    need_id: Annotated[ValidatedNeedId, Field(min_length=1, max_length=40),
                       AfterValidator(_unit_text)]
    opportunity_id: Annotated[OpportunityId, Field(min_length=1, max_length=40),
                              AfterValidator(_unit_text)]
    account_id: Annotated[ProspectAccountId, Field(min_length=1, max_length=40),
                          AfterValidator(_unit_text)]
    actor_id: Annotated[EmployeeId, Field(min_length=1, max_length=40), AfterValidator(_unit_text)]
    authorization_ref: Annotated[str, Field(min_length=1), AfterValidator(_unit_text)]


class NeedUnitEvidenceQuery(_NeedUnitDTO):
    """来源reader须核实当前quantity与所指单位之间的真实关系。"""

    tenant_id: TenantId
    need_id: ValidatedNeedId
    account_id: ProspectAccountId
    actor_id: EmployeeId
    quantity: FactualField[int]
    quantity_fact_hash: _UnitHash
    unit: _UnitText
    source_message_id: MessageId
    locator: _UnitLocator
    source_quote: _UnitQuote


class VerifiedNeedUnitEvidence(_NeedUnitDTO):
    """可信reader输出客户入站消息的核验元数据，不含原始对象键。"""

    tenant_id: Annotated[TenantId, Field(min_length=1, max_length=40), AfterValidator(_unit_text)]
    need_id: Annotated[ValidatedNeedId, Field(min_length=1, max_length=40),
                       AfterValidator(_unit_text)]
    account_id: Annotated[ProspectAccountId, Field(min_length=1, max_length=40),
                          AfterValidator(_unit_text)]
    source_message_id: Annotated[MessageId, Field(min_length=1, max_length=40),
                                 AfterValidator(_unit_text)]
    artifact_id: Annotated[ArtifactId, Field(min_length=1, max_length=40), AfterValidator(_unit_text)]
    content_hash: _UnitHash
    locator: _UnitLocator
    source_quote: _UnitQuote
    unit: _UnitText
    quantity_fact_hash: _UnitHash
    observed_at: _UnitTime


class NeedUnitConfirmationView(_NeedUnitDTO):
    """永久确认receipt，读取历史不表示当前绑定有效。"""

    tenant_id: TenantId
    need_id: ValidatedNeedId
    confirmation_id: _UnitId
    quantity_fact_hash: _UnitHash
    unit: FactualField[str]
    source: VerifiedNeedUnitEvidence
    confirmed_by: EmployeeId
    confirmed_at: _UnitTime

    @model_validator(mode="after")
    def _receipt_binding(self) -> Self:
        """确认receipt不能自相矛盾：单位、消息、人工身份与时间必须一致。"""
        provenance = self.unit.provenance
        if (self.source.tenant_id, self.source.need_id, self.source.quantity_fact_hash,
            self.source.unit, self.source.source_message_id, self.source.source_quote) != (
            self.tenant_id, self.need_id, self.quantity_fact_hash,
            self.unit.value, provenance.source_id, provenance.source_quote,
        ) or (provenance.source_type, provenance.extracted_by, provenance.confirmed_by,
              provenance.extracted_at, provenance.confirmed_at) != (
            SourceType.CONVERSATION, self.confirmed_by, self.confirmed_by,
            self.confirmed_at, self.confirmed_at,
        ):
            raise ValueError("单位确认receipt的事实与来源绑定不一致")
        return self


class NeedUnitStoredConfirmation(_NeedUnitDTO):
    """内部操作记录，不将请求正文持久化或挂HTTP。"""

    view: NeedUnitConfirmationView
    idempotency_key: Annotated[str, Field(min_length=1, max_length=128),
                              AfterValidator(_unit_text)]
    request_hash: _UnitHash

# ISO 3166-1已分配alpha-2代码（2026-08-27核对），只识别来源文字，不授予市场许可。
# 数据快照：pycountry/pycountry e974d00d5ead823a48d6944a6df1696e95e507e3
# src/pycountry/databases/iso3166-1.json；仅取249个代码事实，不引入运行时依赖。
_ISO_3166_ALPHA_2 = frozenset(
    [
        "AD", "AE", "AF", "AG", "AI", "AL", "AM", "AO", "AQ", "AR", "AS", "AT",
        "AU", "AW", "AX", "AZ", "BA", "BB", "BD", "BE", "BF", "BG", "BH", "BI",
        "BJ", "BL", "BM", "BN", "BO", "BQ", "BR", "BS", "BT", "BV", "BW", "BY",
        "BZ", "CA", "CC", "CD", "CF", "CG", "CH", "CI", "CK", "CL", "CM", "CN",
        "CO", "CR", "CU", "CV", "CW", "CX", "CY", "CZ", "DE", "DJ", "DK", "DM",
        "DO", "DZ", "EC", "EE", "EG", "EH", "ER", "ES", "ET", "FI", "FJ", "FK",
        "FM", "FO", "FR", "GA", "GB", "GD", "GE", "GF", "GG", "GH", "GI", "GL",
        "GM", "GN", "GP", "GQ", "GR", "GS", "GT", "GU", "GW", "GY", "HK", "HM",
        "HN", "HR", "HT", "HU", "ID", "IE", "IL", "IM", "IN", "IO", "IQ", "IR",
        "IS", "IT", "JE", "JM", "JO", "JP", "KE", "KG", "KH", "KI", "KM", "KN",
        "KP", "KR", "KW", "KY", "KZ", "LA", "LB", "LC", "LI", "LK", "LR", "LS",
        "LT", "LU", "LV", "LY", "MA", "MC", "MD", "ME", "MF", "MG", "MH", "MK",
        "ML", "MM", "MN", "MO", "MP", "MQ", "MR", "MS", "MT", "MU", "MV", "MW",
        "MX", "MY", "MZ", "NA", "NC", "NE", "NF", "NG", "NI", "NL", "NO", "NP",
        "NR", "NU", "NZ", "OM", "PA", "PE", "PF", "PG", "PH", "PK", "PL", "PM",
        "PN", "PR", "PS", "PT", "PW", "PY", "QA", "RE", "RO", "RS", "RU", "RW",
        "SA", "SB", "SC", "SD", "SE", "SG", "SH", "SI", "SJ", "SK", "SL", "SM",
        "SN", "SO", "SR", "SS", "ST", "SV", "SX", "SY", "SZ", "TC", "TD", "TF",
        "TG", "TH", "TJ", "TK", "TL", "TM", "TN", "TO", "TR", "TT", "TV", "TW",
        "TZ", "UA", "UG", "UM", "US", "UY", "UZ", "VA", "VC", "VE", "VG", "VI",
        "VN", "VU", "WF", "WS", "YE", "YT", "ZA", "ZM", "ZW",
    ]
)


class ResearchEvidence(BaseModel):
    """研究来源归属；查询国家不是企业所在地，自述不是工商核验。

    由受信编排从确认查询和原页面计算，模型不可设置。保守支持英文第一人称
    “We are <名称>, ...”以及同主体总部/所在地句式；接受全部已分配ISO2原文代码，
    英文国名别名有限，不识别时保留待核验。代码有效不等于获准在该市场探索。
    """

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    proposal_id: str = Field(min_length=1, max_length=64)
    query: str = Field(min_length=1, max_length=400)
    discovery_lane: Literal["importer", "distributor", "ecommerce"]
    query_country: str = Field(pattern=r"^[A-Z]{2}$")
    query_category: str = Field(min_length=1, max_length=100)
    source_kind: Literal["company_self_description", "directory_listing", "unverified_public_page"]
    identity_status: Literal["self_described", "pending_verification"]
    company_name: str | None = None
    website_domain: str | None = None
    country: str | None = None
    identity_quote: str | None = None
    country_quote: str | None = None
    source_url: str = Field(min_length=1, max_length=2000)

    @property
    def discovery_key(self) -> str:
        """同一提案、查询、线路、URL重放稳定，跨线路绝不吞证据。"""
        identity = (
            self.proposal_id, self.query, self.discovery_lane,
            self.query_country, self.query_category, self.source_url,
        )
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()

    @classmethod
    def from_page(
        cls, *, proposal_id: str, query: str, discovery_lane: str,
        query_country: str, query_category: str, text: str, url: str,
    ) -> ResearchEvidence:
        """只提取受支持的自述，不从TLD、配送地、query country推断所在地。"""
        base: dict[str, object] = {
            "proposal_id": proposal_id, "query": query, "discovery_lane": discovery_lane,
            "query_country": query_country, "query_category": query_category, "source_url": url,
            "identity_status": "pending_verification", "source_kind": "unverified_public_page",
        }
        if re.search(
            r"\b(?:directory|directories|dealer locator|find a dealer|dealer listings|"
            r"business listings|brand dealers)\b|经销商目录|企业名录|行业目录",
            text, re.IGNORECASE,
        ) or re.search(r"/(?:directory|directories|dealers|companies)(?:/|$)", urlsplit(url).path):
            return cls.model_validate({**base, "source_kind": "directory_listing"})
        identity = re.search(
            r"(?:^|[.\n]\s*)(We are ([A-Z][A-Za-z0-9 &'-]{1,100}), "
            r"(?:an? |the )[^\n.]{1,200}[.])", text,
        )
        if identity is None:
            return cls.model_validate(base)
        name = identity.group(2)
        base.update(source_kind="company_self_description",
                    company_name=name, identity_quote=identity.group(1))
        # 不接受第三方描述、目的地、分支机构地址或低写 us 等不完整证据。
        location = re.search(
            rf"(?:^|[.\n]\s*)((?:We are|{re.escape(name)} is) "
            r"(?:headquartered|based|located) in (?:the )?"
            r"([A-Za-z][A-Za-z ]{1,60})(?=[.,\n]|$))", text,
        )
        aliases = {
            "United States": "US", "United States of America": "US",
            "Germany": "DE", "United Kingdom": "GB", "Canada": "CA",
            "Australia": "AU", "New Zealand": "NZ", "France": "FR",
            "Spain": "ES", "Italy": "IT", "Netherlands": "NL",
        }
        country = None
        if location is not None:
            raw_country = location.group(2).strip()
            country = aliases.get(raw_country)
            if raw_country in _ISO_3166_ALPHA_2:
                country = raw_country
        hostname = urlsplit(url).hostname
        if country is not None and hostname is not None:
            base.update(
                identity_status="self_described", country=country,
                website_domain=hostname.lower().removeprefix("www."),
                country_quote=location.group(1) if location else None,
            )
        return cls.model_validate(base)


@dataclass(frozen=True)
class CustomerReplyEvidenceClaim:
    """调用方声明的回复关联；必须经持久事实 verifier 证明后才能入证据链。"""

    hypothesis_id: NeedHypothesisId
    source_message_id: MessageId
    outbound_message_id: OutboundMessageId
    enrollment_id: EnrollmentId
    account_id: ProspectAccountId
    contact_point_id: ContactPointId


@dataclass(frozen=True)
class VerifiedCustomerReplyEvidence:
    """租户绑定 verifier 从 Conversation/Outreach 持久事实得出的证明。"""

    tenant_id: TenantId
    hypothesis_id: NeedHypothesisId
    source_message_id: MessageId
    account_id: ProspectAccountId
    evidence_level: EvidenceLevel
    classified_by: str
    classified_at: datetime


@dataclass(frozen=True)
class DemandSignalView:
    """需求雷达信号视图；事实观察与可能需求保持结构分离。"""

    signal_id: str
    signal_type: str
    entity_name: str
    raw_observation: str
    possible_need: str | None
    status: str
    observed_at: datetime
    source_type: str
    source_ref: str
    source_url: str | None
    page_hash: str | None
    snapshot_artifact_ref: str | None
    is_inference: bool = False
    research_evidence: ResearchEvidence | None = None


@dataclass(frozen=True)
class HypothesisDiscoveryView:
    """账户发现结构化投影；自由文本证据仅在 demand 域内保留。"""

    hypothesis_id: str
    account_id: str
    organization_name: str
    country: str
    website_domain: str
    category: str
    source_signal_refs: tuple[str, ...]


@dataclass(frozen=True)
class SignalCaptureRequest:
    """记录信号的入参。

    字段：
        signal_type, entity_name, raw_observation
        source_id:     来源身份（网页=页面哈希；非网页=调用方提供的记录 identity）
        extracted_by:  提取者（模型版本标识或 "human"）
        possible_need:  可能的需求方向（参考，不是结论）
        source_url:     网页来源必填
        page_hash:      网页来源必填
        snapshot_artifact_ref: 网页不可变快照引用，网页来源必填
        observed_at
        source_type:    见 ``SourceType``

    校验：``source_type == WEB_PAGE`` 时 URL、小写 SHA-256 hash 与不可变
    快照引用必填，且 ``source_id == page_hash``；服务层还会在同一事务验证
    快照的租户、kind 与 content_hash。服务层拒绝不合格入参，不做"友好补全"。
    """

    signal_type: str
    entity_name: str
    raw_observation: str
    observed_at: datetime
    source_type: str
    source_id: str
    """来源身份：网页类 = 页面哈希；非网页类 = message/upload/provider/
    员工录入记录 identity（调用方提供）。"""
    extracted_by: str
    """提取者（模型版本标识或 "human"；Provenance 要求具体版本）。"""
    possible_need: str | None = None
    source_url: str | None = None
    page_hash: str | None = None
    snapshot_artifact_ref: str | None = None
    research_evidence: ResearchEvidence | None = None


@dataclass(frozen=True)
class EvidenceSummary:
    """证据摘要，供界面展示"为什么这么判断"。

    字段：
        level:        证据等级
        summary:      一句话说明
        source_url:   可点击的来源
        source_ref:   消息/上传的引用标识
        observed_at
    """

    level: str
    summary: str
    observed_at: datetime
    source_url: str | None = None
    source_ref: str | None = None


@dataclass(frozen=True)
class HypothesisView:
    """需求假设视图。

    界面必须把 ``reasoning`` 明确标注为**推断**，并可展开
    ``evidence`` 看依据。不能显示成和已验证需求一样的样式——
    那会让老板误以为这是事实。

    字段：
        hypothesis_id, account_id, account_name, category
        reasoning:        推断理由
        confidence_tier:  档位（离散值，不是小数）
        confidence_explanation: 为什么是这一档，人类可读
        evidence:         证据列表
        status, created_at
        is_inference:     恒为 True，提醒前端渲染成推断样式
    """

    hypothesis_id: str
    account_id: str
    account_name: str
    category: str
    reasoning: str
    confidence_tier: str
    confidence_explanation: str
    evidence: list[EvidenceSummary]
    status: str
    created_at: datetime
    is_inference: bool = True
    validated_need_id: str | None = None


@dataclass(frozen=True)
class NeedFieldView:
    """需求的单个字段 + 来源。

    字段：
        name:          字段名
        value:         显示值
        source_ref:    来源引用（消息 ID / 上传 ID）
        source_quote:  客户原话摘录——**最有说服力的展示**，
                       员工看到原话就知道该怎么接
        confirmed_by:  确认人，None 表示仅模型提取未经人工确认
    """

    name: str
    value: str
    source_ref: str
    source_quote: str | None = None
    confirmed_by: str | None = None


@dataclass(frozen=True)
class ValidatedNeedView:
    """已验证需求视图。

    字段：
        need_id, account_id, account_name
        product_category
        fields:              全部字段 + 来源
        completeness:        0–5
        missing_for_sourcing: 还缺什么才能寻源
        status, created_at
        quantity, destination, required_by, target_price
            —— 常用字段的便捷访问，值本身在 ``fields`` 里有完整来源
    """

    need_id: str
    account_id: str
    account_name: str
    product_category: str
    fields: list[NeedFieldView]
    completeness: int
    missing_for_sourcing: list[str]
    status: str
    created_at: datetime
    quantity: int | None = None
    destination: str | None = None
    required_by: date | None = None
    target_price: Money | None = None
    recurring_requirement: bool | None = None


@dataclass(frozen=True)
class NeedCatalogEventLocator:
    """目录事实事件的最小定位结果；租户只用于约束查询，不进入结果。"""

    need_id: ValidatedNeedId
    cluster_id: NeedClusterId | None


@dataclass(frozen=True)
class NeedClusterView:
    """需求簇视图。

    字段：
        cluster_id, category
        member_count, countries
        total_potential_quantity
        recurring_demand
        member_needs:  成员需求摘要
        suggests_catalog_product: 是否达到提议入正式目录的规模
    """

    cluster_id: str
    category: str
    member_count: int
    countries: list[str]
    member_needs: list[ValidatedNeedView]
    total_potential_quantity: int | None = None
    recurring_demand: bool | None = None
    suggests_catalog_product: bool = False


@dataclass(frozen=True)
class NeedClusterPriorityFacts:
    """经需求域核验的需求簇优先级事实。

    仅暴露准入排序需要的当前簇归属和成员累计，不暴露数量、账户或任何
    客户字段。未归簇的已验证需求固定视为其自身这个一成员簇；观察时间
    表示持久事实版本而非读取时间，因此相同底层版本重复读取必须完全相同。
    """

    need_id: str
    cluster_id: str | None
    cluster_member_count: int
    facts_observed_at: datetime
