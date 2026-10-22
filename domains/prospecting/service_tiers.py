"""三档自动获客处理政策与可选来源摘要；不授予触达或需求晋升权限。"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

import tldextract
from pydantic import AwareDatetime, BaseModel, ConfigDict, PositiveInt, field_validator

from shared.public_page_url import canonical_public_page_url
from shared.schemas.identifiers import EmployeeId, ProspectAccountId, TenantId

ServiceTier = Literal["basic", "standard", "advanced"]
ScreeningEvidenceKind = Literal[
    "business_match",
    "recent_need",
    "contact_role",
    "independent_confirmation",
    "human_review",
]
ScreeningStatus = Literal["ready_for_enrichment", "ready_for_review", "needs_evidence"]
_PolicyVersion = Literal["2026-10-10-v1", "2026-10-10-v2"]
_POLICY_VERSION: _PolicyVersion = "2026-10-10-v2"
_EVIDENCE_ORDER: tuple[ScreeningEvidenceKind, ...] = (
    "business_match",
    "recent_need",
    "contact_role",
    "independent_confirmation",
    "human_review",
)
_PUBLIC_SUFFIX = tldextract.TLDExtract(
    suffix_list_urls=(),
    cache_dir=None,
    include_psl_private_domains=False,
)


class ServiceTierPolicy(BaseModel):
    """每 30 天及单轮的处理上限；不表示候选质量、发送许可或收益保证。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: ServiceTier
    policy_version: _PolicyVersion = _POLICY_VERSION
    period_days: Literal[30] = 30
    candidate_limit: PositiveInt
    search_limit: PositiveInt
    contact_limit: PositiveInt
    cadence_hours: PositiveInt
    max_pages_per_company: PositiveInt
    max_contacts_per_company: PositiveInt
    per_run_search_limit: PositiveInt
    per_run_page_limit: PositiveInt
    per_run_signal_limit: PositiveInt
    per_run_hypothesis_limit: PositiveInt
    # 保留旧响应字段以兼容历史客户端；v2 的来源归类不构成获客前置条件。
    required_requirements: tuple[ScreeningEvidenceKind, ...] = ()


class ScreeningEvidence(BaseModel):
    """上游服务绑定 canonical 来源后提交的证据，不接受模型通过布尔值。

    source_ref/source_url/source_quote 的绑定真实性由服务端核验；本契约只验证
    形状，不能证明原文真实性。人工审核的身份与时间也必须由服务端写入。
    occurred_at 是事件发生时间；未知时保留空值，不得用采集时间替代。
    """

    model_config = ConfigDict(
        frozen=True, extra="forbid", revalidate_instances="always"
    )

    tenant_id: TenantId
    account_id: ProspectAccountId
    kind: ScreeningEvidenceKind
    source_ref: str
    source_url: str
    source_quote: str
    occurred_at: AwareDatetime | None = None
    recorded_at: AwareDatetime
    reviewed_by: EmployeeId | None = None
    reviewed_at: AwareDatetime | None = None

    @field_validator(
        "tenant_id", "account_id", "source_ref", "source_quote", "reviewed_by"
    )
    @classmethod
    def _nonblank_text(cls, value: str | None) -> str | None:
        if value is not None:
            _require_text(value)
        return value

    @field_validator("source_url")
    @classmethod
    def _public_url(cls, value: str) -> str:
        return canonical_public_page_url(value)


class ScreeningAssessment(BaseModel):
    """自动补全状态与可选来源摘要；不表示已验证需求或发送授权。

    evidence_refs 保留全部已检查来源引用（含未满足时效的材料），去重并稳定排序。
    旧状态仅为历史序列化兼容保留，v2 不产生待人工补证或审核状态。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: ServiceTier
    policy_version: _PolicyVersion = _POLICY_VERSION
    status: ScreeningStatus
    satisfied_requirements: tuple[ScreeningEvidenceKind, ...]
    missing_requirements: tuple[ScreeningEvidenceKind, ...]
    evidence_refs: tuple[str, ...]


def tier_policy(tier: ServiceTier) -> ServiceTierPolicy:
    """按显式档位返回固定版本政策；未知档位拒绝，避免静默放宽处理额度。"""

    if tier == "basic":
        return ServiceTierPolicy(
            tier=tier,
            candidate_limit=100,
            search_limit=200,
            contact_limit=20,
            cadence_hours=168,
            max_pages_per_company=2,
            max_contacts_per_company=1,
            per_run_search_limit=10,
            per_run_page_limit=10,
            per_run_signal_limit=10,
            per_run_hypothesis_limit=10,
        )
    if tier == "standard":
        return ServiceTierPolicy(
            tier=tier,
            candidate_limit=300,
            search_limit=600,
            contact_limit=120,
            cadence_hours=56,
            max_pages_per_company=4,
            max_contacts_per_company=1,
            per_run_search_limit=20,
            per_run_page_limit=25,
            per_run_signal_limit=20,
            per_run_hypothesis_limit=20,
        )
    if tier == "advanced":
        return ServiceTierPolicy(
            tier=tier,
            candidate_limit=600,
            search_limit=1200,
            contact_limit=240,
            cadence_hours=24,
            max_pages_per_company=6,
            max_contacts_per_company=2,
            per_run_search_limit=30,
            per_run_page_limit=40,
            per_run_signal_limit=30,
            per_run_hypothesis_limit=30,
        )
    raise ValueError("未知服务档位")


def evaluate_screening(
    tier: ServiceTier,
    tenant_id: TenantId,
    account_id: ProspectAccountId,
    evidence: Sequence[ScreeningEvidence],
    now: datetime,
) -> ScreeningAssessment:
    """汇总已有来源，但不因未归类、未复核或缺少来源类别而阻塞联系人补全。

    跨租户/企业、未来时间及无时区时间直接拒绝。近期需求只认不晚于 now 的
    90 天内事件；独立佐证须与近期需求来自不同可注册域名，不执行网络查询。
    human_review 仅供历史记录展示，来源类别不代表客户已明确需求。
    """

    policy = tier_policy(tier)
    _require_text(tenant_id)
    _require_text(account_id)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("筛选时间必须包含时区")

    checked: list[ScreeningEvidence] = []
    for item in evidence:
        item = ScreeningEvidence.model_validate(item)
        if item.tenant_id != tenant_id or item.account_id != account_id:
            raise ValueError("筛选证据必须属于当前租户和企业")
        for timestamp in (item.occurred_at, item.recorded_at, item.reviewed_at):
            if timestamp is not None and timestamp > now:
                raise ValueError("筛选证据时间不得晚于筛选时间")
        checked.append(item)

    recent_need = [
        item
        for item in checked
        if item.kind == "recent_need"
        and item.occurred_at is not None
        and now - timedelta(days=90) <= item.occurred_at <= now
    ]
    satisfied: set[ScreeningEvidenceKind] = {
        item.kind for item in checked if item.kind in {"business_match", "contact_role"}
    }
    if recent_need:
        satisfied.add("recent_need")
    recent_domains = {
        domain
        for item in recent_need
        if (domain := _registered_domain(item.source_url))
    }
    if any(
        domain and any(domain != recent_domain for recent_domain in recent_domains)
        for item in checked
        if item.kind == "independent_confirmation"
        for domain in [_registered_domain(item.source_url)]
    ):
        satisfied.add("independent_confirmation")
    if any(
        item.kind == "human_review"
        and item.reviewed_by is not None
        and item.reviewed_at is not None
        for item in checked
    ):
        satisfied.add("human_review")
    return ScreeningAssessment(
        tier=tier,
        policy_version=policy.policy_version,
        status="ready_for_enrichment",
        satisfied_requirements=tuple(
            kind for kind in _EVIDENCE_ORDER if kind in satisfied
        ),
        missing_requirements=(),
        evidence_refs=tuple(sorted({item.source_ref for item in checked})),
    )


def _registered_domain(url: str) -> str | None:
    """使用随库 PSL 快照；未知后缀与 IP 不能证明来源独立。"""

    host = urlsplit(url).hostname
    if host is None:
        return None
    extracted = _PUBLIC_SUFFIX(host)
    return extracted.top_domain_under_public_suffix or None


def evidence_source_domain(url: str) -> str | None:
    """校验公开证据 URL 后返回可注册域名；子域/路径不构成独立来源。

    使用离线 PSL；IP 与未知后缀返回 None，不安全或无效 URL 抛 ValueError。
    """

    return _registered_domain(canonical_public_page_url(url))


def _require_text(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("证据来源及身份字段不能为空")


__all__ = (
    "ScreeningAssessment",
    "ScreeningEvidence",
    "ScreeningEvidenceKind",
    "ScreeningStatus",
    "ServiceTier",
    "ServiceTierPolicy",
    "evaluate_screening",
    "evidence_source_domain",
    "tier_policy",
)
