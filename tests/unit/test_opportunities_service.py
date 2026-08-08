"""S2-10 OpportunityService 核心单测（严格 fake UoW/repo/bus/scorer）。

行为断言，不依赖实现细节：
- create：幂等（重复 need 返回既有）；唯一并发（UoW __aexit__/commit 抛 IntegrityError →
  新 UoW 重查既有，找不到才原异常重抛）；门槛失败返回 None、只保留失败快照、不建机会/不发事件；
  通过则建机会、保存 present 关键字段 provenance、发布 OpportunityQualified（含 rank_bucket）；
  校验在打分前——AGENT_INFERENCE 抛 AgentInferenceProvenanceError、present 关键字段缺来源抛
  MissingFieldProvenanceError；account_name/country 落库且参与校验。
- transition：拒绝 WON/LOST 走普通转换；读当前态→状态机校验→advance_state(expected,target)；
  并发失败抛 InvalidStateTransition（不静默）。
- assign：assign_owner 落 owner/assigned_by/注入时钟 assigned_at；失败不静默。
- mark_lost：reason=None 先抛 MissingLossReasonError；顺序 close_lost_if_state →
  loss_records.add（died_at_state=关闭前、confirmed_by/at 一致、recorded_at=注入时钟）→
  bus.publish(OpportunityLost)。禁止普通 update。
- mark_won：仅 NEGOTIATING → close_won_if_state → 发布带 closed_by 的 OpportunityWon。

RED：``domains/opportunities.service_impl`` 尚未创建；经 importlib 延迟导入转行为失败。
模型类型经 ``from domains.opportunities import models`` 引入（真实类型，check_boundaries
不拦该模块名），避免 importlib Any 无法作注解。
"""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import Self

import pytest
from sqlalchemy.exc import IntegrityError

from domains.opportunities import models
from domains.opportunities.permissions import Actor, OpportunityScope, ScopeLevel
from domains.opportunities.schemas import (
    OpportunityCreateRequest,
    ValidatedNeedEvidence,
)
from domains.opportunities.scoring import ScoringInput
from shared.errors import InvalidStateTransition
from shared.events.catalog import (
    DomainEvent,
    OpportunityLost,
    OpportunityQualified,
    OpportunityWon,
)
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
OpportunityState = models.OpportunityState
LossReason = models.LossReason
LossRecord = models.LossRecord
ScoreSnapshot = models.ScoreSnapshot
SortKey = models.SortKey
HandoffPolicy = models.HandoffPolicy

_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
_USD = CurrencyCode("USD")

_MODULE_BY_SYMBOL = {
    "OpportunityServiceImpl": "domains.opportunities.service_impl",
}


def _load(symbol: str):
    """按模块字符串导入符号；缺失转行为失败（RED 阶段 service_impl 未建）。"""
    try:
        return getattr(importlib.import_module(_MODULE_BY_SYMBOL[symbol]), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{symbol} 尚未创建（{exc}）")


class _FakeDBOrig(Exception):
    """模拟 DB 驱动异常的 orig（asyncpg/psycopg 的 sqlstate/pgcode）。"""

    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate
        self.pgcode = sqlstate


def _integrity_error(sqlstate: str = "23505") -> IntegrityError:
    """构造 SQLAlchemy IntegrityError；orig 带 SQLSTATE（默认 unique_violation 23505）。"""
    return IntegrityError(
        "INSERT INTO opportunities ...",
        {},
        _FakeDBOrig(sqlstate),
    )


def _conv_prov() -> Provenance:
    return Provenance(
        source_type=SourceType.CONVERSATION,
        source_id="m1",
        extracted_by="model_v3",
        extracted_at=_NOW,
    )


def _evidence() -> ValidatedNeedEvidence:
    """S3-6：默认有效证据（客户明确表达 + 会话来源），供 create_from_need 显式传。"""
    return ValidatedNeedEvidence(
        level=EvidenceLevel.CUSTOMER_INTEREST_REPLY,
        provenance=_conv_prov(),
    )


def _request(
    *,
    need_id: str = "need-1",
    account_id: str = "acc-1",
    account_name: str = "Acme",
    country: str = "US",
    quantity: int | None = None,
    is_repeat_buyer_likely: bool = False,
    field_provenance: dict[str, Provenance] | None = None,
) -> OpportunityCreateRequest:
    return OpportunityCreateRequest(
        need_id=need_id,
        account_id=account_id,
        account_name=account_name,
        country=country,
        product_category="hinges",
        evidence_tier="high",
        has_verified_contact=True,
        category_allowed=True,
        minimum_order_value=Money(Decimal(100), _USD),
        quantity=quantity,
        is_repeat_buyer_likely=is_repeat_buyer_likely,
        field_provenance=(
            field_provenance
            if field_provenance is not None
            else {"account_name": _conv_prov(), "country": _conv_prov()}
        ),
    )


def _opp(opp_id: str, tenant_id: str, need_id: str, **overrides) -> Opportunity:
    return Opportunity(
        opportunity_id=OpportunityId(opp_id),
        tenant_id=TenantId(tenant_id),
        account_id=ProspectAccountId("acc-1"),
        need_id=ValidatedNeedId(need_id),
        product_category="hinges",
        created_at=_NOW,
        account_name="Acme",
        country="US",
        **overrides,
    )


def _snap(
    *,
    failed_gates: list[str] | None = None,
    sort_key: SortKey | None = None,
    rank_bucket: str | None = None,
    **overrides,
) -> ScoreSnapshot:
    return ScoreSnapshot(
        tenant_id=TenantId("t1"),
        snapshot_id=ScoreSnapshotId("snap-1"),
        opportunity_id=OpportunityId("opp-1"),
        scored_at=_NOW,
        scorer_version="gates-v1",
        passed_gates=[] if failed_gates else ["category_allowed"],
        failed_gates=failed_gates or [],
        evidence_tier=ConfidenceTier.HIGH,
        estimated_value=Money(Decimal(1500), _USD),
        supply_available=True,
        sort_key=sort_key or SortKey(5, 2, 2),
        rank_bucket=rank_bucket or "high",
        gate_reasons={},
        **overrides,
    )


# --- 严格 fake：共享调用序列，能证明顺序与无多余副作用 ----------------------------


class _FakeOpportunityRepo:
    """OpportunityRepository fake：记录调用与顺序；读方法不落序列。"""

    def __init__(self, sequence: list[str]) -> None:
        self.sequence = sequence
        self.rows: dict[OpportunityId, Opportunity] = {}
        self.added: list[Opportunity] = []
        self.advance_calls: list[tuple[object, ...]] = []
        self.close_lost_calls: list[tuple[object, ...]] = []
        self.close_won_calls: list[tuple[object, ...]] = []
        self.assign_calls: list[tuple[object, ...]] = []
        self.advance_result = True
        self.close_lost_result = True
        self.close_won_result = True
        self.assign_result = True

    async def add(self, opportunity: Opportunity) -> None:
        self.sequence.append("opp_add")
        self.added.append(opportunity)
        self.rows[opportunity.opportunity_id] = opportunity

    async def get(self, tenant_id: TenantId, opportunity_id: OpportunityId) -> Opportunity | None:
        return self.rows.get(opportunity_id)

    async def update(self, opportunity: Opportunity) -> None:
        raise AssertionError("服务不应调用普通 update（终态只能走 close_*）")

    async def find_by_need(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> Opportunity | None:
        for opp in self.rows.values():
            if opp.need_id == need_id and opp.tenant_id == tenant_id:
                return opp
        return None

    async def list_by_owner(
        self,
        tenant_id: TenantId,
        owner: EmployeeId,
        states: list[OpportunityState] | None,
        limit: int,
    ) -> list[Opportunity]:
        raise AssertionError("服务不应调用 list_by_owner")

    async def list_by_state(
        self, tenant_id: TenantId, state: OpportunityState, limit: int
    ) -> list[Opportunity]:
        raise AssertionError("服务不应调用 list_by_state")

    async def advance_state(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        expected: OpportunityState,
        target: OpportunityState,
    ) -> bool:
        self.sequence.append("advance")
        self.advance_calls.append((tenant_id, opportunity_id, expected, target))
        return self.advance_result

    async def close_lost_if_state(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        expected: OpportunityState,
        reason: LossReason,
        detail: str | None,
        actor: EmployeeId,
        confirmed_at: datetime,
    ) -> bool:
        self.sequence.append("close_lost")
        self.close_lost_calls.append(
            (tenant_id, opportunity_id, expected, reason, detail, actor, confirmed_at)
        )
        return self.close_lost_result

    async def close_won_if_state(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        actor: EmployeeId,
        confirmed_at: datetime,
    ) -> bool:
        self.sequence.append("close_won")
        self.close_won_calls.append((tenant_id, opportunity_id, actor, confirmed_at))
        return self.close_won_result

    async def assign_owner(
        self,
        tenant_id: TenantId,
        opportunity_id: OpportunityId,
        owner: EmployeeId,
        assigned_by: EmployeeId,
        assigned_at: datetime,
    ) -> bool:
        self.sequence.append("assign")
        self.assign_calls.append((tenant_id, opportunity_id, owner, assigned_by, assigned_at))
        return self.assign_result


class _FakeSnapshotRepo:
    def __init__(self, sequence: list[str]) -> None:
        self.sequence = sequence
        self.added: list[tuple[TenantId, ScoreSnapshot]] = []

    async def add(self, tenant_id: TenantId, snapshot: ScoreSnapshot) -> None:
        self.sequence.append("snap_add")
        self.added.append((tenant_id, snapshot))

    async def latest_for_opportunity(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> ScoreSnapshot | None:
        raise AssertionError("服务不应调用 latest_for_opportunity")

    async def list_for_backtest(self, tenant_id: TenantId, since_days: int) -> list[ScoreSnapshot]:
        raise AssertionError("服务不应调用 list_for_backtest")


class _FakeHandoffRepo:
    def __init__(self, sequence: list[str]) -> None:
        self.sequence = sequence

    async def add(self, packet: object) -> None:
        raise AssertionError("S2-10 服务不应调用 handoff")

    async def get(self, tenant_id: TenantId, handoff_id: object) -> object:
        raise AssertionError("S2-10 服务不应调用 handoff")

    async def update(self, packet: object) -> None:
        raise AssertionError("S2-10 服务不应调用 handoff")

    async def find_pending_for_opportunity(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> object:
        raise AssertionError("S2-10 服务不应调用 handoff")

    async def list_pending(self, tenant_id: TenantId, limit: int) -> list[object]:
        raise AssertionError("S2-10 服务不应调用 handoff")

    async def count_pending_by_employee(self, tenant_id: TenantId) -> dict[str, int]:
        raise AssertionError("S2-10 服务不应调用 handoff")

    async def accept_if_requested(
        self, tenant_id: TenantId, handoff_id: object, accepted_by: EmployeeId, accepted_at: datetime
    ) -> bool:
        raise AssertionError("S2-10 服务不应调用 handoff")


class _FakeLossRepo:
    def __init__(self, sequence: list[str]) -> None:
        self.sequence = sequence
        self.added: list[LossRecord] = []

    async def add(self, tenant_id: TenantId, record: LossRecord) -> None:
        self.sequence.append("loss_add")
        self.added.append(record)

    async def count_by_reason_and_state(
        self, tenant_id: TenantId, since_days: int
    ) -> list[tuple[str, str, int]]:
        raise AssertionError("S2-10 服务不应调用 count_by_reason_and_state")


class _FakeProvenanceRepo:
    def __init__(self, sequence: list[str]) -> None:
        self.sequence = sequence
        self.saved: list[tuple[object, object, object, object, object]] = []

    async def save(
        self,
        tenant_id: TenantId,
        entity_type: str,
        entity_id: str,
        field_name: str,
        provenance: Provenance,
    ) -> None:
        self.sequence.append("prov_save")
        self.saved.append((tenant_id, entity_type, entity_id, field_name, provenance))

    async def list_for_entity(
        self, tenant_id: TenantId, entity_type: str, entity_id: str
    ) -> list[tuple[str, Provenance]]:
        raise AssertionError("服务不应调用 list_for_entity")


class _FakeBus:
    def __init__(self, sequence: list[str]) -> None:
        self.sequence = sequence
        self.published: list[DomainEvent] = []

    async def publish(self, event: DomainEvent) -> None:
        self.sequence.append(f"publish:{type(event).__name__}")
        self.published.append(event)

    async def publish_many(self, events: list[DomainEvent]) -> None:
        for event in events:
            self.sequence.append(f"publish:{type(event).__name__}")
            self.published.append(event)

    def subscribe(self, event_type: object, handler: object) -> None:
        raise AssertionError("服务不应调用 subscribe")


class _FakeUoW:
    """OpportunityUnitOfWork fake：可配置 commit 抛错；记录 enter/exit/commit/rollback 与调用序列。"""

    def __init__(self) -> None:
        self.sequence: list[str] = []
        self.commit_error: Exception | None = None
        self.exits = 0
        self.committed = 0
        self.rolled_back = 0
        self.opportunities = _FakeOpportunityRepo(self.sequence)
        self.snapshots = _FakeSnapshotRepo(self.sequence)
        self.handoffs = _FakeHandoffRepo(self.sequence)
        self.loss_records = _FakeLossRepo(self.sequence)
        self.provenance = _FakeProvenanceRepo(self.sequence)
        self.bus = _FakeBus(self.sequence)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.exits += 1
        if exc_type is None:
            if self.commit_error is not None:
                raise self.commit_error
            self.committed += 1
        else:
            self.rolled_back += 1


class _UoWFactory:
    """返回全新 _FakeUoW 的工厂；可配置首个 commit 抛错、幂等种子、重查种子。"""

    def __init__(self) -> None:
        self.created: list[_FakeUoW] = []
        self.seed_all: list[Opportunity] = []
        self.seed_after_first: Opportunity | None = None
        self.commit_error_on_first: Exception | None = None
        self.default_advance_result = True
        self.default_close_lost_result = True
        self.default_close_won_result = True
        self.default_assign_result = True

    def seed(self, opp: Opportunity) -> None:
        self.seed_all.append(opp)

    def __call__(self) -> _FakeUoW:
        uow = _FakeUoW()
        for opp in self.seed_all:
            uow.opportunities.rows[opp.opportunity_id] = opp
        if self.seed_after_first is not None and len(self.created) >= 1:
            uow.opportunities.rows[self.seed_after_first.opportunity_id] = self.seed_after_first
        if self.commit_error_on_first is not None and len(self.created) == 0:
            uow.commit_error = self.commit_error_on_first
        uow.opportunities.advance_result = self.default_advance_result
        uow.opportunities.close_lost_result = self.default_close_lost_result
        uow.opportunities.close_won_result = self.default_close_won_result
        uow.opportunities.assign_result = self.default_assign_result
        self.created.append(uow)
        return uow


class _FakeScorer:
    """OpportunityScorer fake：记录调用并在传入的 snapshots 内落快照。"""

    def __init__(self, snapshot: ScoreSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[tuple[object, object, object, ScoringInput]] = []

    async def score(self, snapshots, tenant_id, opportunity_id, input: ScoringInput) -> ScoreSnapshot:
        self.calls.append((snapshots, tenant_id, opportunity_id, input))
        await snapshots.add(tenant_id, self.snapshot)
        return self.snapshot


class _AllowAuthorizer:
    """放行 authorizer：本文件测业务行为；授权契约由 test_opportunities_permissions 覆盖。"""

    def require(self, actor, action, scope, tenant_id) -> str:
        return "test:allow"


class _NoopAudit:
    def log(self, **kwargs) -> None:
        pass


def _actor(actor_id: str = "e1") -> Actor:
    """显式授权 actor：本文件调用方统一显式传 actor（tenanted 作用域，无 ABAC 限制）。"""
    return Actor(actor_id=actor_id, scope=OpportunityScope(level=ScopeLevel.TENANT))


def _make_service(factory: _UoWFactory, scorer: _FakeScorer):
    OpportunityServiceImpl = _load("OpportunityServiceImpl")
    return OpportunityServiceImpl(
        factory,
        scorer,
        HandoffPolicy(sla_seconds=3600, backlog_threshold=40),
        authorizer=_AllowAuthorizer(),
        audit=_NoopAudit(),
        now=lambda: _NOW,
    )


# --- create_from_need -------------------------------------------------------------


async def test_create_idempotent() -> None:
    """重复 need_id：返回既有机会，不重复创建/打分/发事件。"""
    factory = _UoWFactory()
    scorer = _FakeScorer(_snap())
    service = _make_service(factory, scorer)
    existing = _opp("opp-1", "t1", "need-1")
    factory.seed(existing)

    result = await service.create_from_need(
        TenantId("t1"), _request(), evidence=_evidence(), actor=_actor()
    )

    assert result == OpportunityId("opp-1")
    uow = factory.created[0]
    assert uow.opportunities.added == []
    assert uow.bus.published == []
    assert uow.snapshots.added == []
    assert scorer.calls == []


async def test_create_unique_race_returns_existing() -> None:
    """UoW __aexit__/commit 抛 IntegrityError → 新 UoW 重查既有；只有找到才返回。"""
    factory = _UoWFactory()
    scorer = _FakeScorer(_snap())
    service = _make_service(factory, scorer)
    factory.commit_error_on_first = _integrity_error()
    existing = _opp("opp-race", "t1", "need-race")
    factory.seed_after_first = existing

    result = await service.create_from_need(
    TenantId("t1"),
    _request(need_id="need-race", account_id="acc-race"),
    evidence=_evidence(),
    actor=_actor(),
)

    assert result == OpportunityId("opp-race")
    assert len(factory.created) == 2
    assert factory.created[0].committed == 0  # 首个 commit 被 IntegrityError 中断
    assert scorer.calls[0][1] == TenantId("t1")  # 只在首个 UoW 打分一次


async def test_create_unique_race_not_found_re_raises() -> None:
    """重查找不到既有记录 → 原 IntegrityError 重抛。"""
    factory = _UoWFactory()
    scorer = _FakeScorer(_snap())
    service = _make_service(factory, scorer)
    factory.commit_error_on_first = _integrity_error()

    with pytest.raises(IntegrityError):
        await service.create_from_need(
            TenantId("t1"), _request(need_id="need-race2"), evidence=_evidence(), actor=_actor()
        )


async def test_create_failed_gates_returns_none_no_event() -> None:
    """门槛失败：返回 None，只保留失败快照，不建机会/不发事件。"""
    factory = _UoWFactory()
    failed_snap = _snap(
        failed_gates=["contactable", "evidence_sufficient"],
        sort_key=SortKey(0, 0, 0),
        rank_bucket="low",
    )
    scorer = _FakeScorer(failed_snap)
    service = _make_service(factory, scorer)

    result = await service.create_from_need(
        TenantId("t1"), _request(), evidence=_evidence(), actor=_actor()
    )

    assert result is None
    uow = factory.created[0]
    assert uow.snapshots.added[0][1] is failed_snap  # 失败快照保留（只增）
    assert uow.opportunities.added == []
    assert uow.bus.published == []
    assert uow.committed == 1  # 快照正常提交


async def test_create_passed_publishes_and_saves_provenance() -> None:
    """通过：建机会、保存 present 关键字段 provenance、发布 OpportunityQualified。"""
    factory = _UoWFactory()
    scorer = _FakeScorer(_snap(failed_gates=[], rank_bucket="high"))
    service = _make_service(factory, scorer)

    result = await service.create_from_need(
        TenantId("t1"),
        _request(
            quantity=500,
            field_provenance={
                "account_name": _conv_prov(),
                "country": _conv_prov(),
                "quantity": _conv_prov(),
            },
        ),
        evidence=_evidence(),
        actor=_actor(),
    )

    assert result is not None
    uow = factory.created[0]
    opp = uow.opportunities.added[0]
    assert opp.opportunity_id == result
    assert opp.account_name == "Acme"
    assert opp.country == "US"
    saved_fields = {f for (t, et, ei, f, p) in uow.provenance.saved}
    assert {"account_name", "country", "quantity"} <= saved_fields
    evt = uow.bus.published[0]
    assert isinstance(evt, OpportunityQualified)
    assert evt.tenant_id == TenantId("t1")
    assert evt.opportunity_id == result
    assert evt.rank_bucket == "high"
    assert uow.committed == 1


async def test_create_rejects_agent_inference_provenance() -> None:
    """关键字段来源是 AGENT_INFERENCE → AgentInferenceProvenanceError（校验先于打分/建机会）。"""
    from domains.opportunities.errors import AgentInferenceProvenanceError

    factory = _UoWFactory()
    scorer = _FakeScorer(_snap())
    service = _make_service(factory, scorer)

    bad = Provenance(
        source_type=SourceType.AGENT_INFERENCE,
        source_id="m1",
        extracted_by="model_v3",
        extracted_at=_NOW,
    )
    with pytest.raises(AgentInferenceProvenanceError):
        await service.create_from_need(
            TenantId("t1"),
            _request(field_provenance={"account_name": bad, "country": _conv_prov()}),
            evidence=_evidence(),
            actor=_actor(),
        )
    assert scorer.calls == []
    assert factory.created[0].opportunities.added == []


async def test_create_requires_provenance_for_present_critical_fields() -> None:
    """present 关键字段缺来源 → MissingFieldProvenanceError。"""
    from domains.opportunities.errors import MissingFieldProvenanceError

    factory = _UoWFactory()
    service = _make_service(factory, _FakeScorer(_snap()))

    # quantity 为 present 关键字段但无 provenance
    with pytest.raises(MissingFieldProvenanceError):
        await service.create_from_need(
            TenantId("t1"), _request(quantity=500), evidence=_evidence(), actor=_actor()
        )


async def test_create_saves_account_name_and_country() -> None:
    """account_name/country 落库且必然参与 Provenance 校验（缺来源即拒绝）。"""
    factory = _UoWFactory()
    scorer = _FakeScorer(_snap())
    service = _make_service(factory, scorer)

    result = await service.create_from_need(
        TenantId("t1"), _request(account_name="Acme", country="US"), evidence=_evidence(), actor=_actor()
    )

    opp = factory.created[0].opportunities.added[0]
    assert opp.account_name == "Acme"
    assert opp.country == "US"
    saved_fields = {f for (t, et, ei, f, p) in factory.created[0].provenance.saved}
    assert {"account_name", "country"} <= saved_fields
    assert result is not None


# --- transition -------------------------------------------------------------------


async def test_transition_rejects_won_and_lost() -> None:
    """普通 transition 拒绝 WON/LOST（终态只能走 mark_lost/mark_won）。"""
    factory = _UoWFactory()
    service = _make_service(factory, _FakeScorer(_snap()))

    with pytest.raises(InvalidStateTransition):
        await service.transition(
            TenantId("t1"), OpportunityId("opp-1"), OpportunityState.WON, actor=_actor()
        )
    with pytest.raises(InvalidStateTransition):
        await service.transition(
            TenantId("t1"), OpportunityId("opp-1"), OpportunityState.LOST, actor=_actor()
        )
    assert factory.created == []  # 守卫在进入 UoW 前


async def test_transition_atomic() -> None:
    """合法转换：读当前状态 → 状态机校验 → advance_state(expected, target)。"""
    factory = _UoWFactory()
    service = _make_service(factory, _FakeScorer(_snap()))
    factory.seed(_opp("opp-1", "t1", "need-1", state=OpportunityState.QUALIFIED))

    await service.transition(
        TenantId("t1"), OpportunityId("opp-1"), OpportunityState.ASSIGNED, actor=_actor()
    )

    uow = factory.created[0]
    assert uow.opportunities.advance_calls[0] == (
        TenantId("t1"),
        OpportunityId("opp-1"),
        OpportunityState.QUALIFIED,
        OpportunityState.ASSIGNED,
    )
    assert uow.committed == 1


async def test_transition_concurrent_failure_not_silent() -> None:
    """advance_state 返回 False（并发已变）→ 抛 InvalidStateTransition，不静默。"""
    factory = _UoWFactory()
    factory.default_advance_result = False
    service = _make_service(factory, _FakeScorer(_snap()))
    factory.seed(_opp("opp-1", "t1", "need-1", state=OpportunityState.QUALIFIED))

    with pytest.raises(InvalidStateTransition):
        await service.transition(
        TenantId("t1"), OpportunityId("opp-1"), OpportunityState.ASSIGNED, actor=_actor()
    )


# --- assign -------------------------------------------------------------------------


async def test_assign_records_actor() -> None:
    """assign：assign_owner 落 owner/assigned_by/注入时钟 assigned_at；失败不静默。"""
    factory = _UoWFactory()
    service = _make_service(factory, _FakeScorer(_snap()))
    factory.seed(_opp("opp-1", "t1", "need-1"))

    await service.assign(
        TenantId("t1"),
        OpportunityId("opp-1"),
        EmployeeId("emp-1"),
        EmployeeId("mgr-1"),
        actor=_actor(),
    )

    uow = factory.created[0]
    assert uow.opportunities.assign_calls[0] == (
        TenantId("t1"),
        OpportunityId("opp-1"),
        EmployeeId("emp-1"),
        EmployeeId("mgr-1"),
        _NOW,
    )
    assert uow.committed == 1

    factory2 = _UoWFactory()
    factory2.default_assign_result = False
    service2 = _make_service(factory2, _FakeScorer(_snap()))
    factory2.seed(_opp("opp-1", "t1", "need-1"))
    with pytest.raises(InvalidStateTransition):
        await service2.assign(
            TenantId("t1"),
            OpportunityId("opp-1"),
            EmployeeId("emp-1"),
            EmployeeId("mgr-1"),
            actor=_actor(),
        )


# --- mark_lost -----------------------------------------------------------------------


async def test_mark_lost_none_reason_raises_missing() -> None:
    """reason=None → MissingLossReasonError（反馈闭环，不进入 UoW）。"""
    from domains.opportunities.errors import MissingLossReasonError

    factory = _UoWFactory()
    service = _make_service(factory, _FakeScorer(_snap()))

    with pytest.raises(MissingLossReasonError):
        await service.mark_lost(
            TenantId("t1"),
            OpportunityId("opp-1"),
            None,
            actor=_actor(),
            confirmed_by=EmployeeId("e1"),
            confirmed_at=_NOW,
        )
    assert factory.created == []


async def test_mark_lost_order_and_persistence() -> None:
    """严格顺序 close_lost_if_state → loss_records.add → bus.publish；人工确认字段一致落库。"""
    factory = _UoWFactory()
    service = _make_service(factory, _FakeScorer(_snap()))
    factory.seed(_opp("opp-1", "t1", "need-1", state=OpportunityState.CONTACTED))
    confirmed = _NOW + timedelta(hours=1)

    await service.mark_lost(
        TenantId("t1"),
        OpportunityId("opp-1"),
        LossReason.PRICE_TOO_HIGH,
        actor=_actor(),
        confirmed_by=EmployeeId("e1"),
        confirmed_at=confirmed,
        detail="输在价格",
    )

    uow = factory.created[0]
    assert uow.sequence == ["close_lost", "loss_add", "publish:OpportunityLost"]
    assert uow.opportunities.close_lost_calls[0] == (
        TenantId("t1"),
        OpportunityId("opp-1"),
        OpportunityState.CONTACTED,
        LossReason.PRICE_TOO_HIGH,
        "输在价格",
        EmployeeId("e1"),
        confirmed,
    )
    rec = uow.loss_records.added[0]
    assert rec.died_at_state == OpportunityState.CONTACTED  # 关闭前状态
    assert rec.confirmed_by == EmployeeId("e1")
    assert rec.confirmed_at == confirmed
    assert rec.recorded_at == _NOW  # 注入时钟
    assert rec.loss_reason == LossReason.PRICE_TOO_HIGH
    evt = uow.bus.published[0]
    assert isinstance(evt, OpportunityLost)
    assert evt.tenant_id == TenantId("t1")
    assert evt.opportunity_id == OpportunityId("opp-1")
    assert evt.loss_reason == "price_too_high"
    assert evt.died_at_state == "contacted"
    assert uow.committed == 1


# --- mark_won -------------------------------------------------------------------------


async def test_mark_won_only_from_negotiating() -> None:
    """仅从 NEGOTIATING 可 mark_won；否则抛 InvalidStateTransition，close_won 不被调用。"""
    factory = _UoWFactory()
    service = _make_service(factory, _FakeScorer(_snap()))
    factory.seed(_opp("opp-1", "t1", "need-1", state=OpportunityState.QUALIFIED))

    with pytest.raises(InvalidStateTransition):
        await service.mark_won(
            TenantId("t1"),
            OpportunityId("opp-1"),
            actor=_actor(),
            confirmed_by=EmployeeId("e1"),
            confirmed_at=_NOW,
        )
    assert factory.created[0].opportunities.close_won_calls == []


async def test_mark_won_publishes_opportunity_won() -> None:
    """从 NEGOTIATING mark_won：close_won_if_state → 发布带 closed_by 的 OpportunityWon。"""
    factory = _UoWFactory()
    service = _make_service(factory, _FakeScorer(_snap()))
    factory.seed(_opp("opp-1", "t1", "need-1", state=OpportunityState.NEGOTIATING))
    confirmed = _NOW + timedelta(hours=2)

    await service.mark_won(
        TenantId("t1"),
        OpportunityId("opp-1"),
        actor=_actor(),
        confirmed_by=EmployeeId("e1"),
        confirmed_at=confirmed,
    )

    uow = factory.created[0]
    assert uow.sequence == ["close_won", "publish:OpportunityWon"]
    assert uow.opportunities.close_won_calls[0] == (
        TenantId("t1"),
        OpportunityId("opp-1"),
        EmployeeId("e1"),
        confirmed,
    )
    evt = uow.bus.published[0]
    assert isinstance(evt, OpportunityWon)
    assert evt.tenant_id == TenantId("t1")
    assert evt.opportunity_id == OpportunityId("opp-1")
    assert evt.closed_by == EmployeeId("e1")
    assert uow.committed == 1


# --- 强化验收：唯一并发恢复收窄 / Provenance 只存 present / ScoringInput 完整 / 转换可诊断 ---


async def test_create_unique_race_runtime_error_not_swallowed() -> None:
    """首个 UoW __aexit__ 抛普通 RuntimeError（非唯一冲突）→ 原样抛出，即使重查有既有。"""
    factory = _UoWFactory()
    scorer = _FakeScorer(_snap())
    service = _make_service(factory, scorer)
    factory.commit_error_on_first = RuntimeError("boom")
    factory.seed_after_first = _opp("opp-race", "t1", "need-race")

    with pytest.raises(RuntimeError):
        await service.create_from_need(
            TenantId("t1"), _request(need_id="need-race"), evidence=_evidence(), actor=_actor()
        )
    assert len(factory.created) == 1  # 不进入重查，原异常立即上抛


async def test_create_unique_race_non_unique_integrity_error_re_raises() -> None:
    """IntegrityError 但 SQLSTATE 非 unique_violation（23514）→ 原样抛出，即使重查有既有。"""
    factory = _UoWFactory()
    scorer = _FakeScorer(_snap())
    service = _make_service(factory, scorer)
    factory.commit_error_on_first = _integrity_error(sqlstate="23514")
    factory.seed_after_first = _opp("opp-race", "t1", "need-race")

    with pytest.raises(IntegrityError):
        await service.create_from_need(
            TenantId("t1"), _request(need_id="need-race"), evidence=_evidence(), actor=_actor()
        )
    assert len(factory.created) == 1  # 非 23505 不进入重查


async def test_create_unique_race_impersonated_integrity_error_re_raises() -> None:
    """非 sqlalchemy 模块、类名也叫 IntegrityError、SQLSTATE=23505 → 原样抛出（防冒充）。"""
    class IntegrityError(Exception):  # 本地类：__name__ 同、__module__ 非 sqlalchemy.exc
        def __init__(self, orig: object) -> None:
            super().__init__("fake integrity error")
            self.orig = orig

    factory = _UoWFactory()
    scorer = _FakeScorer(_snap())
    service = _make_service(factory, scorer)
    factory.commit_error_on_first = IntegrityError(_FakeDBOrig("23505"))
    factory.seed_after_first = _opp("opp-race", "t1", "need-race")

    with pytest.raises(IntegrityError):
        await service.create_from_need(
            TenantId("t1"), _request(need_id="need-race"), evidence=_evidence(), actor=_actor()
        )
    assert len(factory.created) == 1  # 模块不符 → 不进入重查，原异常上抛


async def test_create_does_not_save_unknown_provenance_keys() -> None:
    """field_provenance 含未知键 → 该键不保存（只保存 present 的 CRITICAL_FIELDS）。"""
    factory = _UoWFactory()
    scorer = _FakeScorer(_snap())
    service = _make_service(factory, scorer)

    result = await service.create_from_need(
        TenantId("t1"),
        _request(
            quantity=500,
            field_provenance={
                "account_name": _conv_prov(),
                "country": _conv_prov(),
                "quantity": _conv_prov(),
                "mystery_field": _conv_prov(),  # 非 CRITICAL_FIELDS 的未知键
            },
        ),
        evidence=_evidence(),
        actor=_actor(),
    )

    assert result is not None
    saved_fields = {f for (t, et, ei, f, p) in factory.created[0].provenance.saved}
    assert saved_fields == {"account_name", "country", "quantity"}
    assert "mystery_field" not in saved_fields


async def test_create_passes_is_repeat_buyer_likely_to_scorer() -> None:
    """is_repeat_buyer_likely 传入 ScoringInput（避免调用边界丢字段）。"""
    factory = _UoWFactory()
    scorer = _FakeScorer(_snap())
    service = _make_service(factory, scorer)

    await service.create_from_need(
        TenantId("t1"), _request(is_repeat_buyer_likely=True), evidence=_evidence(), actor=_actor()
    )

    passed_input = scorer.calls[0][3]
    assert isinstance(passed_input, ScoringInput)
    assert passed_input.is_repeat_buyer_likely is True


async def test_transition_error_includes_states_and_allowed() -> None:
    """非法转换消息含当前态、目标态、允许转换列表（shared.errors 约定，精确断言关键信息）。"""
    factory = _UoWFactory()
    service = _make_service(factory, _FakeScorer(_snap()))
    factory.seed(_opp("opp-1", "t1", "need-1", state=OpportunityState.QUALIFIED))

    with pytest.raises(InvalidStateTransition) as excinfo:
        await service.transition(
            TenantId("t1"),
            OpportunityId("opp-1"),
            OpportunityState.NEGOTIATING,
            actor=_actor(),
        )

    msg = str(excinfo.value)
    assert "qualified" in msg  # 当前态
    assert "negotiating" in msg  # 目标态
    assert "assigned" in msg and "lost" in msg  # QUALIFIED 的允许转换列表
