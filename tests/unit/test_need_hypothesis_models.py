"""NeedHypothesis 纯域内核单测（2026-08-17 计划 Task 1；无 DB 零 mock）。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime

import pytest

from shared.errors import ValidationError
from shared.schemas.evidence import EvidenceItem, EvidenceLevel
from shared.schemas.identifiers import (
    DemandSignalId,
    NeedHypothesisId,
    ProspectAccountId,
    TenantId,
    new_id,
)
from shared.schemas.provenance import SourceType

_models = importlib.import_module("domains.demand.models")

NOW = datetime(2026, 8, 17, 9, 0, tzinfo=UTC)


def _evidence(
    level: EvidenceLevel, source_type: str, source_id: str = "src-1"
) -> EvidenceItem:
    return EvidenceItem(
        level=level,
        source_type=source_type,
        source_id=source_id,
        observed_at=NOW,
        summary="Acme 扩建公告 SECRET-SUMMARY-1",
    )


def _hypothesis(evidence: list[EvidenceItem]):
    return _models.NeedHypothesis(
        hypothesis_id=NeedHypothesisId(new_id("hyp")),
        tenant_id=TenantId(new_id("tn")),
        account_id=ProspectAccountId(new_id("acc")),
        category="stainless steel hinges",
        reasoning=_models.InferredField(
            value="可能需要耐腐蚀五金",
            based_on=evidence,
            inferred_by="model-v1",
            inferred_at=NOW,
        ),
        signal_ids=[DemandSignalId(new_id("sig"))],
        created_at=NOW,
    )


def test_evidence_dedup_same_source_keeps_highest_level() -> None:
    """同一 (source_type, source_id) 只留等级最高一条（同页面/同消息不重复计）。"""
    hypothesis = _hypothesis(
        [
            _evidence(EvidenceLevel.PUBLIC_COMPANY_EVENT, "web_page", "sha256:same"),
            _evidence(EvidenceLevel.AGENT_INDUSTRY_INFERENCE, "web_page", "sha256:same"),
        ]
    )
    assert len(hypothesis.evidence()) == 1
    assert hypothesis.evidence()[0].level is EvidenceLevel.PUBLIC_COMPANY_EVENT


def test_evidence_keeps_distinct_sources_in_first_seen_order() -> None:
    """跨 source_id 保留全部，输出保持组首现顺序。"""
    hypothesis = _hypothesis(
        [
            _evidence(EvidenceLevel.PUBLIC_COMPANY_EVENT, "web_page", "sha256:a"),
            _evidence(EvidenceLevel.CUSTOMER_SPECIFICATION, "conversation", "msg-2"),
            _evidence(EvidenceLevel.AGENT_INDUSTRY_INFERENCE, "web_page", "sha256:c"),
        ]
    )
    ids = [item.source_id for item in hypothesis.evidence()]
    assert ids == ["sha256:a", "msg-2", "sha256:c"]


def test_can_promote_requires_customer_evidence_from_whitelisted_sources() -> None:
    """唯一通过条件：≥CUSTOMER_INTEREST_REPLY 且 source_type ∈ 白名单。"""
    agent_only = _hypothesis([_evidence(EvidenceLevel.AGENT_INDUSTRY_INFERENCE, "web_page")])
    assert not agent_only.can_promote_to_validated()
    public_event = _hypothesis(
        [_evidence(EvidenceLevel.PUBLIC_COMPANY_EVENT, "web_page", "sha256:p")]
    )
    assert not public_event.can_promote_to_validated()
    web_reply = _hypothesis(
        [_evidence(EvidenceLevel.CUSTOMER_INTEREST_REPLY, "web_page", "sha256:w")]
    )
    assert not web_reply.can_promote_to_validated()
    for source in ("conversation", "upload", "employee_input"):
        ok = _hypothesis(
            [_evidence(EvidenceLevel.CUSTOMER_INTEREST_REPLY, source, f"src-{source}")]
        )
        assert ok.can_promote_to_validated(), source


def test_demand_signal_evidence_level_mapping_only_three_direct_tiers() -> None:
    """映射：RFQ/入站询盘/招标 → CUSTOMER_INTEREST_REPLY；企业变化 → 公开事件；
    其余 → 行业推断；含规格内容也不升级（finding 4）。"""
    direct = {
        "public_rfq",
        "inbound_inquiry",
        "tender_notice",
    }
    company = {
        "product_line_expansion",
        "facility_expansion",
        "new_market_entry",
        "procurement_role_hiring",
        "distributor_change",
        "new_certification",
        "large_contract_won",
        "funding_or_merger",
    }
    for member in _models.SignalType.__members__.values():
        value = member.value
        if value in direct:
            expected = EvidenceLevel.CUSTOMER_INTEREST_REPLY
        elif value in company:
            expected = EvidenceLevel.PUBLIC_COMPANY_EVENT
        else:
            expected = EvidenceLevel.AGENT_INDUSTRY_INFERENCE
        signal = _models.DemandSignal(
            signal_id=DemandSignalId(new_id("sig")),
            tenant_id=TenantId(new_id("tn")),
            signal_type=member,
            entity_name="Acme Manufacturing",
            raw_observation="Acme 发布含详细规格的公开招标 SECRET-MARKER",
            observed_at=NOW,
            provenance=_models.Provenance(
                source_type=SourceType.WEB_PAGE,
                source_id="sha256:pagehash001",
                extracted_by="model-v1",
                extracted_at=NOW,
                source_url="https://example.com/tender",
                page_hash="sha256:pagehash001",
            ),
        )
        assert signal.evidence_level is expected, value
    # P2-2：三集合对枚举全集构成划分（新增类型必须显式归类）
    all_values = {member.value for member in _models.SignalType}
    assert (
        direct | company | set(_models._AGENT_INFERENCE_SIGNAL_TYPES)
        == all_values
    )


def test_unknown_signal_type_evidence_level_rejected() -> None:
    signal = _models.DemandSignal(
        signal_id=DemandSignalId(new_id("sig")),
        tenant_id=TenantId(new_id("tn")),
        signal_type="bogus_type",  # type: ignore[arg-type]
        entity_name="Acme Manufacturing",
        raw_observation="Acme 扩建公告",
        observed_at=NOW,
        provenance=_models.Provenance(
            source_type=SourceType.WEB_PAGE,
            source_id="sha256:pagehash001",
            extracted_by="model-v1",
            extracted_at=NOW,
            source_url="https://example.com/acme",
            page_hash="sha256:pagehash001",
        ),
    )
    with pytest.raises(ValidationError):
        _ = signal.evidence_level
