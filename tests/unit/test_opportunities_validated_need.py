"""S3-6 ValidatedNeedEvidence 证据门禁单测（R5/F6）。

覆盖：
- ``ValidatedNeedEvidence`` typed 契约：``EvidenceLevel`` + ``Provenance``，
  不用裸 dict / 置信度数字；
- 等级门禁：≥ ``CUSTOMER_INTEREST_REPLY`` 接受，三个更弱等级（行业推断 /
  公开企业事件 / 员工猜测）拒绝；
- 来源白名单：仅 conversation / upload / employee_input；WEB_PAGE /
  AGENT_INFERENCE / EXTERNAL_API 拒绝；
- ``EMPLOYEE_INPUT`` 必须带真实人工确认（confirmed_by/confirmed_at）；
  conversation/upload 可直接指向具体来源 ID，无需确认对；
- 复用 ``Provenance`` 不变量（source_id 非空、confirmed 对同有同无）；
- service 强制：invalid evidence 在 UoW / 打分 / 幂等查询 / 事件之前拒绝，
  即使同一 need_id 已存在也不返回既有机会、不发任何事件。

RED：``ValidatedNeedEvidence`` 尚未定义 → import 即 AttributeError。
"""
from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Self

import pytest

from domains.opportunities import models
from domains.opportunities.permissions import Actor, OpportunityScope, ScopeLevel
from domains.opportunities.schemas import (
    OpportunityCreateRequest,
    ValidatedNeedEvidence,
)
from domains.opportunities.service import OpportunityService
from domains.opportunities.service_impl import (
    OpportunityServiceImpl,
    validate_validated_need_evidence,
)
from shared.errors import ValidationError
from shared.schemas.evidence import ConfidenceTier, EvidenceLevel
from shared.schemas.identifiers import (
    EmployeeId,
    OpportunityId,
    ProspectAccountId,
    ScoreSnapshotId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.money import CurrencyCode, Money
from shared.schemas.provenance import Provenance, SourceType

Opportunity = models.Opportunity
HandoffPolicy = models.HandoffPolicy
ScoreSnapshot = models.ScoreSnapshot
SortKey = models.SortKey

_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
_USD = CurrencyCode("USD")


def _conv_prov() -> Provenance:
    """会话来源 Provenance：无需人工确认对，直接指向具体消息 ID。"""
    return Provenance(
        source_type=SourceType.CONVERSATION,
        source_id="m1",
        extracted_by="human",
        extracted_at=_NOW,
    )


def _employee_confirmed_prov() -> Provenance:
    """员工录入 + 真实人工确认对（confirmed_by/confirmed_at 同有）。"""
    return Provenance(
        source_type=SourceType.EMPLOYEE_INPUT,
        source_id="emp-input-1",
        extracted_by="human",
        extracted_at=_NOW,
        confirmed_by=EmployeeId("e1"),
        confirmed_at=_NOW,
    )


def _evidence(
    level: EvidenceLevel = EvidenceLevel.CUSTOMER_INTEREST_REPLY,
    *,
    prov: Provenance | None = None,
) -> ValidatedNeedEvidence:
    return ValidatedNeedEvidence(
        level=level,
        provenance=prov if prov is not None else _conv_prov(),
    )


def _request(*, need_id: str = "need-1") -> OpportunityCreateRequest:
    return OpportunityCreateRequest(
        need_id=need_id,
        account_id="acc-1",
        account_name="Acme",
        country="US",
        product_category="hinges",
        evidence_tier="high",
        has_verified_contact=True,
        category_allowed=True,
        minimum_order_value=Money(Decimal(100), _USD),
        field_provenance={"account_name": _conv_prov(), "country": _conv_prov()},
    )


def _actor(actor_id: str = "e1") -> Actor:
    return Actor(actor_id=actor_id, scope=OpportunityScope(level=ScopeLevel.TENANT))


# --- 证据等级门禁 -----------------------------------------------------------


def test_minimum_customer_interest_level_accepted() -> None:
    """最低等级 CUSTOMER_INTEREST_REPLY 接受（客户第一次从本人处表达）。"""
    validate_validated_need_evidence(_evidence(level=EvidenceLevel.CUSTOMER_INTEREST_REPLY))


@pytest.mark.parametrize(
    "level",
    [
        EvidenceLevel.CUSTOMER_SPECIFICATION,
        EvidenceLevel.CUSTOMER_QUANTITY_AND_TIMING,
        EvidenceLevel.CUSTOMER_SAMPLE_OR_QUOTE_REQUEST,
    ],
)
def test_stronger_customer_levels_accepted(level: EvidenceLevel) -> None:
    """三个更强的客户等级全部接受。"""
    validate_validated_need_evidence(_evidence(level=level))


@pytest.mark.parametrize(
    "level",
    [
        EvidenceLevel.AGENT_INDUSTRY_INFERENCE,
        EvidenceLevel.PUBLIC_COMPANY_EVENT,
        EvidenceLevel.EMPLOYEE_GUESS,
    ],
)
def test_weaker_levels_rejected(level: EvidenceLevel) -> None:
    """三个更弱等级（推断）拒绝：LOW_MID/公开企业事件不是已验证需求。"""
    with pytest.raises(ValidationError):
        validate_validated_need_evidence(_evidence(level=level))


# --- 来源白名单 -------------------------------------------------------------


@pytest.mark.parametrize(
    "source_type",
    [SourceType.WEB_PAGE, SourceType.AGENT_INFERENCE, SourceType.EXTERNAL_API],
)
def test_forbidden_source_types_rejected(source_type: SourceType) -> None:
    """来源只允许 conversation/upload/employee_input；其余一律拒绝。"""
    prov = Provenance(
        source_type=source_type,
        source_id="x1",
        extracted_by="human",
        extracted_at=_NOW,
        source_url="https://example.com" if source_type is SourceType.WEB_PAGE else None,
        page_hash="abc" if source_type is SourceType.WEB_PAGE else None,
    )
    with pytest.raises(ValidationError):
        validate_validated_need_evidence(_evidence(prov=prov))


@pytest.mark.parametrize(
    "source_type",
    [SourceType.CONVERSATION, SourceType.UPLOAD],
)
def test_conversation_and_upload_sources_accepted(source_type: SourceType) -> None:
    """conversation/upload 可直接指向具体来源 ID，无需人工确认对。"""
    prov = Provenance(
        source_type=source_type,
        source_id="m1",
        extracted_by="human",
        extracted_at=_NOW,
    )
    validate_validated_need_evidence(_evidence(prov=prov))


# --- Provenance 不变量与员工确认 --------------------------------------------


def test_blank_source_id_rejected() -> None:
    """复用 Provenance 不变量：source_id 空白在构造期即拒绝。"""
    with pytest.raises(ValidationError):
        Provenance(
            source_type=SourceType.CONVERSATION,
            source_id="   ",
            extracted_by="human",
            extracted_at=_NOW,
        )


def test_employee_input_without_confirmation_rejected() -> None:
    """EMPLOYEE_INPUT 缺人工确认对 → 拒绝（员工录入本身不算客户明确表达）。"""
    prov = Provenance(
        source_type=SourceType.EMPLOYEE_INPUT,
        source_id="emp-input-1",
        extracted_by="human",
        extracted_at=_NOW,
    )
    with pytest.raises(ValidationError):
        validate_validated_need_evidence(_evidence(prov=prov))


def test_employee_input_with_confirmation_accepted() -> None:
    """EMPLOYEE_INPUT 带真实人工确认对 → 接受。"""
    validate_validated_need_evidence(_evidence(prov=_employee_confirmed_prov()))


# --- typed DTO 契约 --------------------------------------------------------


def test_validated_need_evidence_is_typed() -> None:
    """DTO 用 EvidenceLevel + Provenance，不是裸 dict / 置信度数字。"""
    ev = _evidence(level=EvidenceLevel.CUSTOMER_INTEREST_REPLY)
    assert ev.level is EvidenceLevel.CUSTOMER_INTEREST_REPLY
    assert isinstance(ev.provenance, Provenance)
    assert ev.provenance.source_id == "m1"


def test_protocol_signature_requires_evidence_and_actor() -> None:
    """public Protocol：create_from_need 必须显式带 evidence 与 actor。"""
    params = inspect.signature(OpportunityService.create_from_need).parameters
    assert "evidence" in params
    assert params["evidence"].default is inspect.Parameter.empty
    assert "actor" in params
    assert params["actor"].default is inspect.Parameter.empty


# --- service 强制：invalid evidence 在一切副作用之前拒绝 -----------------------


class _AllowAuthorizer:
    def require(self, actor: Actor, action: object, scope: object, tenant_id: object) -> str:
        return "test:allow"


class _NoopAudit:
    def log(self, **kwargs: object) -> None:
        pass


class _NeverScorer:
    async def score(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("证据校验必须在打分前；scorer 不应被调用")


class _NeverUoWFactory:
    def __call__(self) -> object:
        raise AssertionError("证据校验必须在进入 UoW 前；不应创建 UoW")


class _FakeOpportunityRepo:
    def __init__(self) -> None:
        self.rows: dict[OpportunityId, Opportunity] = {}
        self.added: list[Opportunity] = []

    async def add(self, opportunity: Opportunity) -> None:
        self.added.append(opportunity)
        self.rows[opportunity.opportunity_id] = opportunity

    async def find_by_need(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> Opportunity | None:
        for opp in self.rows.values():
            if opp.need_id == need_id and opp.tenant_id == tenant_id:
                return opp
        return None


class _FakeSnapshotRepo:
    def __init__(self) -> None:
        self.added: list[tuple[TenantId, ScoreSnapshot]] = []

    async def add(self, tenant_id: TenantId, snapshot: ScoreSnapshot) -> None:
        self.added.append((tenant_id, snapshot))


class _FakeProvenanceRepo:
    def __init__(self) -> None:
        self.saved: list[tuple[object, object, object, object, Provenance]] = []

    async def save(
        self,
        tenant_id: TenantId,
        entity_type: str,
        entity_id: str,
        field_name: str,
        provenance: Provenance,
    ) -> None:
        self.saved.append((tenant_id, entity_type, entity_id, field_name, provenance))


class _FakeBus:
    def __init__(self) -> None:
        self.published: list[object] = []

    async def publish(self, event: object) -> None:
        self.published.append(event)


class _FakeUoW:
    def __init__(self) -> None:
        self.opportunities = _FakeOpportunityRepo()
        self.snapshots = _FakeSnapshotRepo()
        self.provenance = _FakeProvenanceRepo()
        self.bus = _FakeBus()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class _FakeUoWFactory:
    def __init__(self) -> None:
        self.created: list[_FakeUoW] = []
        self.seed_opp: Opportunity | None = None

    def seed(self, opp: Opportunity) -> None:
        self.seed_opp = opp

    def __call__(self) -> _FakeUoW:
        uow = _FakeUoW()
        if self.seed_opp is not None:
            uow.opportunities.rows[self.seed_opp.opportunity_id] = self.seed_opp
        self.created.append(uow)
        return uow


class _PassScorer:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object, object, object]] = []

    async def score(
        self, snapshots: object, tenant_id: object, opportunity_id: object, input: object
    ) -> ScoreSnapshot:
        self.calls.append((snapshots, tenant_id, opportunity_id, input))
        snap = ScoreSnapshot(
            tenant_id=tenant_id,
            snapshot_id=ScoreSnapshotId("snap-1"),
            opportunity_id=opportunity_id,
            scored_at=_NOW,
            scorer_version="gates-v1",
            passed_gates=["contactable", "evidence_sufficient", "category_allowed", "value_above_floor"],
            failed_gates=[],
            evidence_tier=ConfidenceTier.MID_HIGH,
            estimated_value=Money(Decimal(1500), _USD),
            supply_available=True,
            sort_key=SortKey(4, 2, 2),
            rank_bucket="mid",
            gate_reasons={},
        )
        await snapshots.add(tenant_id, snap)  # type: ignore[attr-defined]
        return snap


def _make_service(*, factory: object = None, scorer: object = None) -> OpportunityServiceImpl:
    return OpportunityServiceImpl(
        factory if factory is not None else _NeverUoWFactory(),
        scorer if scorer is not None else _NeverScorer(),
        HandoffPolicy(sla_seconds=3600, backlog_threshold=40),
        authorizer=_AllowAuthorizer(),
        audit=_NoopAudit(),
        now=lambda: _NOW,
    )


def _opp(opp_id: str, tenant_id: str, need_id: str) -> Opportunity:
    return Opportunity(
        opportunity_id=OpportunityId(opp_id),
        tenant_id=TenantId(tenant_id),
        account_id=ProspectAccountId("acc-1"),
        need_id=ValidatedNeedId(need_id),
        product_category="hinges",
        created_at=_NOW,
        account_name="Acme",
        country="US",
    )


async def test_service_rejects_invalid_evidence_before_any_side_effect() -> None:
    """invalid evidence → ValidationError，UoW/打分/幂等查询/事件全都不发生。"""
    service = _make_service()  # _NeverUoWFactory + _NeverScorer：被调用即断言失败
    bad = _evidence(level=EvidenceLevel.PUBLIC_COMPANY_EVENT)
    with pytest.raises(ValidationError):
        await service.create_from_need(TenantId("t1"), _request(), bad, actor=_actor())


async def test_service_rejects_invalid_evidence_even_when_need_exists() -> None:
    """幂等不豁免证据校验：同一 need 已有机会，invalid evidence 仍拒绝、不返回既有。"""
    factory = _FakeUoWFactory()
    factory.seed(_opp("opp-1", "t1", "need-1"))
    service = _make_service(factory=factory, scorer=_NeverScorer())
    bad = _evidence(level=EvidenceLevel.EMPLOYEE_GUESS)
    with pytest.raises(ValidationError):
        await service.create_from_need(
            TenantId("t1"), _request(need_id="need-1"), bad, actor=_actor()
        )
    assert factory.created == []  # 未进入 UoW：幂等查询不该被走到


async def test_service_accepts_valid_evidence_and_creates() -> None:
    """valid evidence 通过证据门禁并创建机会（证据门禁不误伤有效需求）。"""
    factory = _FakeUoWFactory()
    service = _make_service(factory=factory, scorer=_PassScorer())
    result = await service.create_from_need(
        TenantId("t1"), _request(), _evidence(), actor=_actor()
    )
    assert result is not None
    uow = factory.created[0]
    assert len(uow.opportunities.added) == 1
    assert len(uow.bus.published) == 1  # OpportunityQualified
