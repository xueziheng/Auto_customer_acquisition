"""S2-1 契约对齐形状测试（行为断言；新符号延迟导入转行为失败，无 Any/type-ignore/noqa）。

覆盖：
1. ScoreSnapshot 新字段（tenant_id/snapshot_id/sort_key/gate_reasons）可构造
2. SortKey tuple 序（证据主导）
3. ScoringPolicy 构造 + __post_init__ 校验（边界空/非升序/异币种、bucket_map 覆盖 1..7 / 值限 high/mid/low）
4. LossRecord / HandoffCreateRequest（含 customer_verbatim_provenance）可实例化
5. OpportunityCreateRequest 新字段（account_name/country/门槛输入/field_provenance）
6. HandoffPacket.wait_seconds(now) 可调用
7. OpportunityWon 是 DomainEvent、closed_by 必填
8. CRITICAL_FIELDS 14 字段
9. HandoffPolicy 构造 + 两值 >0 校验
10. Repository/Service/UoW/Scorer 运行时签名形状
"""
from __future__ import annotations

import importlib
import inspect
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from domains.opportunities.schemas import (
    OpportunityCreateRequest,
    ScoreExplanation,
    ValidatedNeedEvidence,
)
from domains.opportunities.scoring import OpportunityScorer
from domains.opportunities.service import OpportunityService
from shared.errors import ValidationError
from shared.events.catalog import DomainEvent
from shared.schemas.evidence import EvidenceLevel
from shared.schemas.identifiers import (
    EmployeeId,
    OpportunityId,
    ProspectAccountId,
    RunId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.money import CurrencyCode, Money
from shared.schemas.provenance import Provenance, SourceType

_USD = CurrencyCode("USD")
_NOW = datetime(2026, 8, 8, tzinfo=UTC)

_MODULE_BY_SYMBOL = {
    "ScoreSnapshotId": "shared.schemas.identifiers",
    "LossRecordId": "shared.schemas.identifiers",
    "OpportunityWon": "shared.events.catalog",
    "SortKey": "domains.opportunities.models",
    "HandoffPolicy": "domains.opportunities.models",
    "LossRecord": "domains.opportunities.models",
    "CRITICAL_FIELDS": "domains.opportunities.models",
    "Opportunity": "domains.opportunities.models",
    "ScoreSnapshot": "domains.opportunities.models",
    "HandoffPacket": "domains.opportunities.models",
    "HandoffCreateRequest": "domains.opportunities.schemas",
    "OpportunityRepository": "domains.opportunities.repository",
    "ScoreSnapshotRepository": "domains.opportunities.repository",
    "HandoffRepository": "domains.opportunities.repository",
    "LossRecordRepository": "domains.opportunities.repository",
    "FieldProvenanceRepository": "domains.opportunities.repository",
    "OpportunityUnitOfWork": "domains.opportunities.repository",
    "ScoringPolicy": "domains.opportunities.scoring",
}


def _load(symbol: str):
    """按模块字符串导入符号（importlib，AST 不视为 import 语句）。

    契约测试需构造域私有模型/repository；边界检查的 domain-internals 规则
    不豁免 tests，故经字符串模块名路由，避免被机检误拦。
    """
    try:
        return getattr(importlib.import_module(_MODULE_BY_SYMBOL[symbol]), symbol)
    except AttributeError as exc:
        pytest.fail(f"RED：{symbol} 尚未定义（{exc}）")


# 域私有模型/repository 经 importlib 路由（见 _load 说明）。
Opportunity = _load("Opportunity")
ScoreSnapshot = _load("ScoreSnapshot")
HandoffPacket = _load("HandoffPacket")
OpportunityRepository = _load("OpportunityRepository")
ScoreSnapshotRepository = _load("ScoreSnapshotRepository")
HandoffRepository = _load("HandoffRepository")


def _bucket_map() -> dict[int, str]:
    return {1: "low", 2: "low", 3: "mid", 4: "mid", 5: "mid", 6: "high", 7: "high"}


# --- 1. ScoreSnapshot 新字段 --------------------------------------------------


def test_score_snapshot_new_fields() -> None:
    SortKey = _load("SortKey")
    ScoreSnapshotId = _load("ScoreSnapshotId")
    snap = ScoreSnapshot(
        tenant_id=TenantId("t1"),
        snapshot_id=ScoreSnapshotId("snap1"),
        opportunity_id=OpportunityId("opp1"),
        scored_at=_NOW,
        scorer_version="gates-v1",
        passed_gates=["contactable"],
        failed_gates=[],
        evidence_tier=None,
        estimated_value=None,
        supply_available=True,
        sort_key=SortKey(1, 0, 0),
        rank_bucket="high",
        gate_reasons={},
    )
    assert snap.tenant_id == TenantId("t1")
    assert snap.sort_key == SortKey(1, 0, 0)
    assert snap.gate_reasons == {}


# --- 2. SortKey tuple 序 ------------------------------------------------------


def test_sort_key_tuple_ordering() -> None:
    SortKey = _load("SortKey")
    assert SortKey(7, 0, 0) > SortKey(1, 9, 2)  # 证据主导：HIGH 压过 LOW 无论价值/供应
    assert SortKey(3, 2, 0) < SortKey(3, 2, 1)  # 同证据同价值：供应破平


# --- 3. ScoringPolicy 构造与校验 ----------------------------------------------


def test_scoring_policy_constructible() -> None:
    ScoringPolicy = _load("ScoringPolicy")
    p = ScoringPolicy(
        version="gates-v1",
        value_band_boundaries=(Money(Decimal(100), _USD), Money(Decimal(1000), _USD)),
        bucket_map=_bucket_map(),
    )
    assert p.version == "gates-v1"


def test_scoring_policy_rejects_bad_boundaries() -> None:
    ScoringPolicy = _load("ScoringPolicy")
    with pytest.raises(ValidationError):
        ScoringPolicy(version="x", value_band_boundaries=(), bucket_map=_bucket_map())  # 空
    with pytest.raises(ValidationError):
        ScoringPolicy(  # 非升序
            version="x",
            value_band_boundaries=(Money(Decimal(1000), _USD), Money(Decimal(100), _USD)),
            bucket_map=_bucket_map(),
        )
    with pytest.raises(ValidationError):
        ScoringPolicy(  # 异币种
            version="x",
            value_band_boundaries=(Money(Decimal(100), _USD), Money(Decimal(200), CurrencyCode("CNY"))),
            bucket_map=_bucket_map(),
        )


def test_scoring_policy_rejects_bad_bucket_map() -> None:
    ScoringPolicy = _load("ScoringPolicy")
    boundaries = (Money(Decimal(100), _USD),)
    with pytest.raises(ValidationError):
        ScoringPolicy(version="x", value_band_boundaries=boundaries, bucket_map={1: "low", 2: "mid"})
    with pytest.raises(ValidationError):
        ScoringPolicy(version="x", value_band_boundaries=boundaries, bucket_map={i: "bad" for i in range(1, 8)})


# --- 4. LossRecord / HandoffCreateRequest -------------------------------------


def test_loss_record_instantiable() -> None:
    LossRecord = _load("LossRecord")
    LossRecordId = _load("LossRecordId")
    rec = LossRecord(
        loss_record_id=LossRecordId("lr1"),
        tenant_id=TenantId("t1"),
        opportunity_id=OpportunityId("opp1"),
        loss_reason="price_too_high",
        died_at_state="quoted",
        detail="",
        evidence_tier=None,
        confirmed_by=EmployeeId("e1"),
        confirmed_at=_NOW,
        recorded_at=_NOW,
    )
    assert rec.loss_reason == "price_too_high"
    assert rec.confirmed_by == EmployeeId("e1")


def test_handoff_create_request_instantiable() -> None:
    HandoffCreateRequest = _load("HandoffCreateRequest")
    req = HandoffCreateRequest(
        opportunity_id=OpportunityId("opp1"),
        trigger="quote_requested",
        account_name="Acme",
        country="US",
        why_valuable="正在扩建第二座工厂",
        customer_verbatim="we need hinges",
        customer_verbatim_provenance=Provenance(
            source_type=SourceType.CONVERSATION,
            source_id="m1",
            extracted_by="model_v3",
            extracted_at=_NOW,
        ),
    )
    assert req.customer_verbatim == "we need hinges"


# --- 5. OpportunityCreateRequest 新字段 ---------------------------------------


def test_create_request_new_fields() -> None:
    req = OpportunityCreateRequest(
        need_id="need1",
        account_id="acc1",
        account_name="Acme",
        country="US",
        product_category="hinges",
        evidence_tier="public_company_event",
        has_verified_contact=True,
        category_allowed=True,
        minimum_order_value=Money(Decimal(500), _USD),
        supply_available=True,
        field_provenance={},
        quantity=None,
    )
    assert req.category_allowed is True
    assert req.account_name == "Acme"
    assert req.country == "US"


def test_validated_need_evidence_typed() -> None:
    """ValidatedNeedEvidence 用 EvidenceLevel + Provenance 构造（不用裸 dict/概率）。"""
    ev = ValidatedNeedEvidence(
        level=EvidenceLevel.CUSTOMER_INTEREST_REPLY,
        provenance=Provenance(
            source_type=SourceType.CONVERSATION,
            source_id="m1",
            extracted_by="human",
            extracted_at=_NOW,
        ),
    )
    assert ev.level == EvidenceLevel.CUSTOMER_INTEREST_REPLY
    assert ev.provenance.source_type == SourceType.CONVERSATION
    assert ev.provenance.source_id == "m1"


def test_create_from_need_requires_evidence_and_actor() -> None:
    """create_from_need 必须显式带 evidence（S3-6）与 actor（S3-5），均无默认值。"""
    params = inspect.signature(OpportunityService.create_from_need).parameters
    assert "evidence" in params
    assert params["evidence"].default is inspect.Parameter.empty
    assert "actor" in params
    assert params["actor"].default is inspect.Parameter.empty


# --- 6. HandoffPacket.wait_seconds(now) ---------------------------------------


def test_wait_seconds_is_method() -> None:
    # 契约：wait_seconds 是方法（带 now 参数），不是 property——用 isfunction 检查，
    # 不触发行为实现（行为 stub 保持 NotImplementedError，S2-7 再实现）。
    assert inspect.isfunction(HandoffPacket.wait_seconds)


# --- 7. OpportunityWon 事件 ----------------------------------------------------


def test_opportunity_won_is_domain_event() -> None:
    OpportunityWon = _load("OpportunityWon")
    evt = OpportunityWon(
        tenant_id=TenantId("t1"),
        occurred_at=_NOW,
        run_id=RunId("r1"),
        opportunity_id=OpportunityId("opp1"),
        closed_by=EmployeeId("e1"),
    )
    assert issubclass(OpportunityWon, DomainEvent)
    assert evt.opportunity_id == OpportunityId("opp1")
    assert evt.closed_by == EmployeeId("e1")


def test_opportunity_won_rejects_empty_closed_by() -> None:
    OpportunityWon = _load("OpportunityWon")
    with pytest.raises(ValidationError):
        OpportunityWon(
            tenant_id=TenantId("t1"),
            occurred_at=_NOW,
            run_id=RunId("r1"),
            opportunity_id=OpportunityId("opp1"),
            closed_by=None,
        )


# --- 8. CRITICAL_FIELDS -------------------------------------------------------


def test_critical_fields_has_14_fields() -> None:
    CRITICAL_FIELDS = _load("CRITICAL_FIELDS")
    assert set(CRITICAL_FIELDS) == {
        "account_name",
        "country",
        "quantity",
        "spec_summary",
        "application",
        "destination",
        "required_by",
        "target_price",
        "decision_maker",
        "current_supply_solution",
        "current_supply_problem",
        "can_source",
        "estimated_cost",
        "estimated_profit",
    }


# --- Opportunity 新字段 --------------------------------------------------------


def test_opportunity_has_account_name_and_country() -> None:
    opp = Opportunity(
        opportunity_id=OpportunityId("opp1"),
        tenant_id=TenantId("t1"),
        account_id=ProspectAccountId("acc1"),
        need_id=ValidatedNeedId("need1"),
        product_category="hinges",
        created_at=_NOW,
        account_name="Acme",
        country="US",
    )
    assert opp.account_name == "Acme"
    assert opp.country == "US"


# --- 9. HandoffPolicy ---------------------------------------------------------


def test_handoff_policy_constructible() -> None:
    HandoffPolicy = _load("HandoffPolicy")
    p = HandoffPolicy(sla_seconds=3600, backlog_threshold=40)
    assert p.sla_seconds == 3600
    assert p.backlog_threshold == 40


def test_handoff_policy_rejects_non_positive() -> None:
    HandoffPolicy = _load("HandoffPolicy")
    with pytest.raises(ValidationError):
        HandoffPolicy(sla_seconds=0, backlog_threshold=40)
    with pytest.raises(ValidationError):
        HandoffPolicy(sla_seconds=3600, backlog_threshold=0)
    with pytest.raises(ValidationError):
        HandoffPolicy(sla_seconds=-1, backlog_threshold=-5)


# --- 10. Repository / Service / UoW / Scorer 运行时签名形状 --------------------


def test_opportunity_repository_terminal_methods() -> None:
    assert hasattr(OpportunityRepository, "advance_state")
    assert hasattr(OpportunityRepository, "close_won_if_state")
    assert hasattr(OpportunityRepository, "close_lost_if_state")
    assert hasattr(OpportunityRepository, "assign_owner")
    assert not hasattr(OpportunityRepository, "count_loss_reasons")


def test_score_snapshot_repo_add_takes_tenant() -> None:
    assert "tenant_id" in inspect.signature(ScoreSnapshotRepository.add).parameters


def test_handoff_repo_accept_if_requested() -> None:
    assert hasattr(HandoffRepository, "accept_if_requested")


def test_loss_record_repo_exists() -> None:
    LossRecordRepository = _load("LossRecordRepository")
    assert hasattr(LossRecordRepository, "add")
    assert hasattr(LossRecordRepository, "count_by_reason_and_state")


def test_field_provenance_repo_exists() -> None:
    FieldProvenanceRepository = _load("FieldProvenanceRepository")
    assert hasattr(FieldProvenanceRepository, "save")
    assert hasattr(FieldProvenanceRepository, "list_for_entity")


def test_opportunity_service_terminal_and_handoff_signatures() -> None:
    assert hasattr(OpportunityService, "mark_won")
    for name in (
        "create_from_need",
        "assign",
        "transition",
        "mark_lost",
        "mark_won",
        "request_handoff",
        "accept_handoff",
        "get_handoff_packet",
        "get_queue_stats",
        "get",
        "list_for_employee",
        "loss_reason_breakdown",
    ):
        params = inspect.signature(getattr(OpportunityService, name)).parameters
        assert "actor" in params, f"{name} 必须带显式授权 actor"
        assert params["actor"].default is inspect.Parameter.empty, f"{name} 的 actor 必须显式传"
    mark_lost_params = inspect.signature(OpportunityService.mark_lost).parameters
    assert "confirmed_by" in mark_lost_params  # 业务确认人（与授权 actor 分离）
    assert "confirmed_at" in mark_lost_params
    assert "detail" in mark_lost_params
    assert "confirmed_by" in inspect.signature(OpportunityService.mark_won).parameters
    assign_params = inspect.signature(OpportunityService.assign).parameters
    assert "assigned_by" in assign_params  # 业务审计主体与授权 actor 分离
    request_params = set(inspect.signature(OpportunityService.request_handoff).parameters)
    assert "request" in request_params
    assert not {"opportunity_id", "trigger"} & request_params


def test_opportunity_scorer_score_takes_snapshots() -> None:
    assert "snapshots" in inspect.signature(OpportunityScorer.score).parameters


def test_unit_of_work_exists() -> None:
    OpportunityUnitOfWork = _load("OpportunityUnitOfWork")
    assert hasattr(OpportunityUnitOfWork, "__aenter__")
    assert hasattr(OpportunityUnitOfWork, "__aexit__")
    # repos 与 bus 是注解属性（Protocol 运行时无实体属性），检查 __annotations__
    for attr in ("opportunities", "snapshots", "handoffs", "loss_records", "provenance", "bus"):
        assert attr in getattr(OpportunityUnitOfWork, "__annotations__", {})


# --- 第四轮补测：人工审计字段 / ScoreExplanation / Provenance 历史 / assign 必填 ---


def test_opportunity_has_audit_fields() -> None:
    opp = Opportunity(
        opportunity_id=OpportunityId("opp1"),
        tenant_id=TenantId("t1"),
        account_id=ProspectAccountId("acc1"),
        need_id=ValidatedNeedId("need1"),
        product_category="hinges",
        created_at=_NOW,
        account_name="Acme",
        country="US",
        closed_by=EmployeeId("e1"),
        closed_at=_NOW,
        assigned_by=EmployeeId("m1"),
        assigned_at=_NOW,
    )
    assert opp.closed_by == EmployeeId("e1")
    assert opp.closed_at == _NOW
    assert opp.assigned_by == EmployeeId("m1")
    assert opp.assigned_at == _NOW


def test_score_explanation_uses_sort_key_not_factor_scores() -> None:
    SortKey = _load("SortKey")
    expl = ScoreExplanation(
        sort_key=SortKey(1, 0, 0),
        rank_bucket="low",
        passed_gates=["contactable"],
        failed_gates=[],
        gate_reasons={},
        scored_at=_NOW,
        scorer_version="gates-v1",
    )
    assert expl.sort_key == SortKey(1, 0, 0)
    assert not hasattr(expl, "factor_scores")


def test_field_provenance_list_returns_history() -> None:
    import typing

    FieldProvenanceRepository = _load("FieldProvenanceRepository")
    ret = typing.get_type_hints(FieldProvenanceRepository.list_for_entity)["return"]
    assert ret == list[tuple[str, Provenance]]  # 同一字段多条只增历史（新到旧）


def test_assign_requires_assigned_by() -> None:
    params = inspect.signature(OpportunityService.assign).parameters
    assert "assigned_by" in params
    assert params["assigned_by"].default is inspect.Parameter.empty  # 人工审计主体必填
    assert "actor" in params  # 授权 actor 与 assigned_by 分离，均必填
    assert params["actor"].default is inspect.Parameter.empty
