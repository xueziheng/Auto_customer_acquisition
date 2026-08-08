"""S2-11 OpportunityService handoff 与查询单测（严格 fake UoW/repo/bus）。

行为断言，不依赖实现细节：
- request_handoff：account_name/why_valuable/customer_verbatim 空串或纯空白 →
  IncompleteHandoffPacketError（不保存/不发事件）；customer_verbatim 来源只允许
  conversation/upload/employee_input（WEB_PAGE/EXTERNAL_API/AGENT_INFERENCE → ValidationError）；
  provenance.save(tenant_id,"handoff",str(handoff_id),"customer_verbatim",prov) 顺序
  prov_save→handoff_add→publish(HandoffRequested)；tenant+opportunity 已有 pending 幂等返回；
  机会不存在/不属于租户 → ValidationError；非法 trigger → ValidationError（不泄漏 ValueError）；
  packet 复制 list 字段避免入参别名。
- accept_handoff：accept_if_requested 原子更新；False → HandoffAlreadyAcceptedError；
  True → 发布 HandoffAccepted（accepted_at/occurred_at 同一次注入 now、带 accepted_by）。
- get_queue_stats：全部基于同一 now 调 wait_seconds；等号边界 wait==sla 不 breached、
  depth==threshold 不 backlogged；超过才 breached/backlogged 且发 HandoffQueueBacklogged；
  oldest=max 不依赖输入顺序；by_employee 完整；技术扫描上限非业务阈值。
- get/get_handoff_packet：最新快照完整复制成 ScoreExplanation（无快照 score=None）；
  packet 完整映射、list 字段复制、wait_seconds 用注入 now；查不到 → ValidationError。
- list_for_employee：limit<=0 → ValidationError；同 UoW 构造全部 View。
- loss_reason_breakdown：since_days<=0 → ValidationError；行→{reason:{state:count}} 同键累加。

RED：service_impl 的 handoff/查询方法尚未实现 → AttributeError（行为失败）。
"""
from __future__ import annotations

import importlib
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import Self

import pytest

from domains.opportunities import models
from domains.opportunities.errors import (
    HandoffAlreadyAcceptedError,
    IncompleteHandoffPacketError,
)
from domains.opportunities.schemas import HandoffCreateRequest
from shared.errors import ValidationError
from shared.events.catalog import (
    DomainEvent,
    HandoffAccepted,
    HandoffQueueBacklogged,
    HandoffRequested,
)
from shared.schemas.evidence import ConfidenceTier
from shared.schemas.identifiers import (
    EmployeeId,
    HandoffId,
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
HandoffPacket = models.HandoffPacket
HandoffState = models.HandoffState
HandoffTrigger = models.HandoffTrigger
ScoreSnapshot = models.ScoreSnapshot
SortKey = models.SortKey
HandoffPolicy = models.HandoffPolicy

_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
_USD = CurrencyCode("USD")

_MODULE_BY_SYMBOL = {
    "OpportunityServiceImpl": "domains.opportunities.service_impl",
}


def _load(symbol: str):
    """按模块字符串导入符号；方法缺失转行为失败（RED 阶段 handoff/查询未实现）。"""
    try:
        return getattr(importlib.import_module(_MODULE_BY_SYMBOL[symbol]), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{symbol} 尚未创建（{exc}）")


def _conv_prov(*, source_type: SourceType = SourceType.CONVERSATION) -> Provenance:
    if source_type == SourceType.WEB_PAGE:
        return Provenance(
            source_type=source_type,
            source_id="m1",
            extracted_by="model_v3",
            extracted_at=_NOW,
            source_url="https://example.com/company",
            page_hash="abc123",
        )
    return Provenance(
        source_type=source_type, source_id="m1", extracted_by="model_v3", extracted_at=_NOW
    )


def _request(
    *,
    opportunity_id: str = "opp-1",
    trigger: str = "quote_requested",
    account_name: str = "Acme",
    country: str = "US",
    why_valuable: str = "扩建第二座工厂",
    customer_verbatim: str = "we need hinges",
    customer_verbatim_provenance: Provenance | None = None,
    missing_information: list[str] | None = None,
    already_sent: list[str] | None = None,
    commitments_made: list[str] | None = None,
    evidence_links: list[str] | None = None,
) -> HandoffCreateRequest:
    return HandoffCreateRequest(
        opportunity_id=opportunity_id,
        trigger=trigger,
        account_name=account_name,
        country=country,
        why_valuable=why_valuable,
        customer_verbatim=customer_verbatim,
        customer_verbatim_provenance=customer_verbatim_provenance or _conv_prov(),
        missing_information=missing_information or [],
        already_sent=already_sent or [],
        commitments_made=commitments_made or [],
        evidence_links=evidence_links or [],
    )


def _packet(
    handoff_id: str = "ho-1",
    *,
    tenant_id: str = "t1",
    opportunity_id: str = "opp-1",
    state: str = "requested",
    requested_at: datetime = _NOW,
    assigned_to: EmployeeId | None = None,
    **overrides,
) -> HandoffPacket:
    return HandoffPacket(
        handoff_id=HandoffId(handoff_id),
        tenant_id=TenantId(tenant_id),
        opportunity_id=OpportunityId(opportunity_id),
        trigger=HandoffTrigger.QUOTE_REQUESTED,
        requested_at=requested_at,
        account_name="Acme",
        country="US",
        why_valuable="扩建",
        customer_verbatim="we need hinges",
        state=HandoffState(state),
        assigned_to=assigned_to,
        **overrides,
    )


def _opp(opp_id: str = "opp-1", **overrides) -> Opportunity:
    return Opportunity(
        opportunity_id=OpportunityId(opp_id),
        tenant_id=TenantId("t1"),
        account_id=ProspectAccountId("acc-1"),
        need_id=ValidatedNeedId("need-1"),
        product_category="hinges",
        created_at=_NOW,
        account_name="Acme",
        country="US",
        **overrides,
    )


def _snap(
    *,
    passed_gates: list[str] | None = None,
    failed_gates: list[str] | None = None,
    sort_key: SortKey | None = None,
    rank_bucket: str | None = None,
    gate_reasons: dict[str, str] | None = None,
    **overrides,
) -> ScoreSnapshot:
    return ScoreSnapshot(
        tenant_id=TenantId("t1"),
        snapshot_id=ScoreSnapshotId("snap-1"),
        opportunity_id=OpportunityId("opp-1"),
        scored_at=_NOW,
        scorer_version="gates-v1",
        passed_gates=passed_gates if passed_gates is not None else ["category_allowed"],
        failed_gates=failed_gates if failed_gates is not None else [],
        evidence_tier=ConfidenceTier.HIGH,
        estimated_value=Money(Decimal(1500), _USD),
        supply_available=True,
        sort_key=sort_key if sort_key is not None else SortKey(5, 2, 2),
        rank_bucket=rank_bucket if rank_bucket is not None else "high",
        gate_reasons=gate_reasons if gate_reasons is not None else {},
        **overrides,
    )


# --- 严格 fake ------------------------------------------------------------


class _FakeOpportunityRepo:
    def __init__(self, sequence: list[str]) -> None:
        self.sequence = sequence
        self.row: Opportunity | None = None
        self.owner_rows: list[Opportunity] = []
        self.list_calls: list[tuple[object, ...]] = []

    async def add(self, opportunity: Opportunity) -> None:
        raise AssertionError("handoff/查询不应调用 opportunities.add")

    async def get(self, tenant_id: TenantId, opportunity_id: OpportunityId) -> Opportunity | None:
        return self.row

    async def update(self, opportunity: Opportunity) -> None:
        raise AssertionError("handoff/查询不应调用 opportunities.update")

    async def find_by_need(self, tenant_id: TenantId, need_id: ValidatedNeedId) -> Opportunity | None:
        raise AssertionError("handoff/查询不应调用 find_by_need")

    async def list_by_owner(
        self,
        tenant_id: TenantId,
        owner: EmployeeId,
        states: list[OpportunityState] | None,
        limit: int,
    ) -> list[Opportunity]:
        self.list_calls.append((tenant_id, owner, states, limit))
        return self.owner_rows

    async def list_by_state(self, tenant_id: TenantId, state: OpportunityState, limit: int) -> list[Opportunity]:
        raise AssertionError("handoff/查询不应调用 list_by_state")

    async def advance_state(self, tenant_id: TenantId, opportunity_id: OpportunityId, expected: object, target: object) -> bool:
        raise AssertionError("handoff/查询不应调用 advance_state")

    async def close_lost_if_state(self, *args: object) -> bool:
        raise AssertionError("handoff/查询不应调用 close_lost_if_state")

    async def close_won_if_state(self, *args: object) -> bool:
        raise AssertionError("handoff/查询不应调用 close_won_if_state")

    async def assign_owner(self, *args: object) -> bool:
        raise AssertionError("handoff/查询不应调用 assign_owner")


class _FakeSnapshotRepo:
    def __init__(self, sequence: list[str]) -> None:
        self.sequence = sequence
        self.latest: ScoreSnapshot | None = None

    async def add(self, tenant_id: TenantId, snapshot: ScoreSnapshot) -> None:
        raise AssertionError("handoff/查询不应调用 snapshots.add")

    async def latest_for_opportunity(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> ScoreSnapshot | None:
        return self.latest

    async def list_for_backtest(self, tenant_id: TenantId, since_days: int) -> list[ScoreSnapshot]:
        raise AssertionError("handoff/查询不应调用 list_for_backtest")


class _FakeHandoffRepo:
    def __init__(self, sequence: list[str]) -> None:
        self.sequence = sequence
        self.added: list[HandoffPacket] = []
        self.pending: HandoffPacket | None = None
        self.pending_list: list[HandoffPacket] = []
        self.pending_limits: list[int] = []
        self.row: HandoffPacket | None = None
        self.accept_result = True
        self.accept_calls: list[tuple[object, ...]] = []
        self.count_rows: dict[str, int] = {}
        self.count_calls: list[tuple[object, ...]] = []

    async def add(self, packet: HandoffPacket) -> None:
        self.sequence.append("handoff_add")
        self.added.append(packet)

    async def get(self, tenant_id: TenantId, handoff_id: HandoffId) -> HandoffPacket | None:
        return self.row

    async def update(self, packet: HandoffPacket) -> None:
        raise AssertionError("handoff/查询不应调用 handoffs.update")

    async def find_pending_for_opportunity(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> HandoffPacket | None:
        return self.pending

    async def list_pending(self, tenant_id: TenantId, limit: int) -> list[HandoffPacket]:
        self.pending_limits.append(limit)
        return self.pending_list

    async def count_pending_by_employee(self, tenant_id: TenantId) -> dict[str, int]:
        self.count_calls.append((tenant_id,))
        return self.count_rows

    async def accept_if_requested(
        self,
        tenant_id: TenantId,
        handoff_id: HandoffId,
        accepted_by: EmployeeId,
        accepted_at: datetime,
    ) -> bool:
        self.sequence.append("accept")
        self.accept_calls.append((tenant_id, handoff_id, accepted_by, accepted_at))
        return self.accept_result


class _FakeLossRepo:
    def __init__(self, sequence: list[str]) -> None:
        self.sequence = sequence
        self.rows: list[tuple[str, str, int]] = []
        self.calls: list[tuple[object, object]] = []

    async def add(self, tenant_id: TenantId, record: object) -> None:
        raise AssertionError("handoff/查询不应调用 loss_records.add")

    async def count_by_reason_and_state(
        self, tenant_id: TenantId, since_days: int
    ) -> list[tuple[str, str, int]]:
        self.calls.append((tenant_id, since_days))
        return self.rows


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

    async def list_for_entity(self, tenant_id: TenantId, entity_type: str, entity_id: str) -> list[object]:
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


class _ScorerNeverUsed:
    async def score(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("S2-11 handoff/查询不应调用 scorer")


class _FakeUoW:
    def __init__(self) -> None:
        self.sequence: list[str] = []
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
            self.committed += 1
        else:
            self.rolled_back += 1


class _UoWFactory:
    def __init__(self) -> None:
        self.created: list[_FakeUoW] = []
        self.default_accept_result = True
        self.seed_opp_row: Opportunity | None = None
        self.seed_pending_packet: HandoffPacket | None = None
        self.pending_packets: list[HandoffPacket] = []
        self.seed_row_packet: HandoffPacket | None = None
        self.seed_latest_snap: ScoreSnapshot | None = None
        self.owner_rows: list[Opportunity] = []
        self.loss_rows: list[tuple[str, str, int]] = []
        self.count_rows: dict[str, int] = {}

    def seed_opp(self, opp: Opportunity) -> None:
        self.seed_opp_row = opp

    def seed_pending(self, packet: HandoffPacket) -> None:
        self.seed_pending_packet = packet

    def seed_pending_list(self, packets: list[HandoffPacket]) -> None:
        self.pending_packets = list(packets)

    def seed_row(self, packet: HandoffPacket) -> None:
        self.seed_row_packet = packet

    def seed_latest(self, snap: ScoreSnapshot) -> None:
        self.seed_latest_snap = snap

    def seed_owner_rows(self, opps: list[Opportunity]) -> None:
        self.owner_rows = list(opps)

    def seed_loss_rows(self, rows: list[tuple[str, str, int]]) -> None:
        self.loss_rows = list(rows)

    def seed_count_rows(self, rows: dict[str, int]) -> None:
        self.count_rows = dict(rows)

    def __call__(self) -> _FakeUoW:
        uow = _FakeUoW()
        uow.opportunities.row = self.seed_opp_row
        uow.opportunities.owner_rows = self.owner_rows
        uow.handoffs.pending = self.seed_pending_packet
        uow.handoffs.pending_list = self.pending_packets
        uow.handoffs.row = self.seed_row_packet
        uow.handoffs.accept_result = self.default_accept_result
        uow.handoffs.count_rows = self.count_rows
        uow.snapshots.latest = self.seed_latest_snap
        uow.loss_records.rows = self.loss_rows
        self.created.append(uow)
        return uow


def _make_service(factory: _UoWFactory, **overrides):
    OpportunityServiceImpl = _load("OpportunityServiceImpl")
    return OpportunityServiceImpl(
        factory,
        _ScorerNeverUsed(),
        HandoffPolicy(sla_seconds=3600, backlog_threshold=40),
        now=lambda: _NOW,
        **overrides,
    )


# --- request_handoff ---------------------------------------------------------


async def test_request_handoff_incomplete_rejected() -> None:
    """account_name/why_valuable/customer_verbatim 空串或纯空白 → IncompleteHandoffPacketError。

    无 pending 时进入单个 UoW 后回滚：不保存、不发事件、不 commit。
    """
    factory = _UoWFactory()
    service = _make_service(factory)

    with pytest.raises(IncompleteHandoffPacketError):
        await service.request_handoff(TenantId("t1"), _request(account_name="   "))
    with pytest.raises(IncompleteHandoffPacketError):
        await service.request_handoff(TenantId("t1"), _request(why_valuable=" "))
    with pytest.raises(IncompleteHandoffPacketError):
        await service.request_handoff(TenantId("t1"), _request(customer_verbatim=""))

    uow = factory.created[0]
    assert uow.rolled_back == 1  # 单个 UoW 回滚
    assert uow.committed == 0
    assert uow.provenance.saved == []
    assert uow.handoffs.added == []
    assert uow.bus.published == []


async def test_request_handoff_requires_verbatim_provenance() -> None:
    """customer_verbatim 来源非 conversation/upload/employee_input → ValidationError（回滚无副作用）。"""
    factory = _UoWFactory()
    service = _make_service(factory)
    for source in (SourceType.WEB_PAGE, SourceType.EXTERNAL_API, SourceType.AGENT_INFERENCE):
        with pytest.raises(ValidationError):
            await service.request_handoff(
                TenantId("t1"), _request(customer_verbatim_provenance=_conv_prov(source_type=source))
            )

    uow = factory.created[0]
    assert uow.rolled_back == 1
    assert uow.committed == 0
    assert uow.provenance.saved == []
    assert uow.handoffs.added == []
    assert uow.bus.published == []


async def test_request_handoff_saves_verbatim_provenance() -> None:
    """顺序 prov_save → handoff_add → publish(HandoffRequested)；save 参数精确。"""
    factory = _UoWFactory()
    service = _make_service(factory)
    factory.seed_opp(_opp())

    result = await service.request_handoff(TenantId("t1"), _request())

    uow = factory.created[0]
    assert result == uow.handoffs.added[0].handoff_id
    assert uow.sequence == ["prov_save", "handoff_add", "publish:HandoffRequested"]
    saved = uow.provenance.saved[0]
    assert saved[0] == TenantId("t1")
    assert saved[1] == "handoff"
    assert saved[2] == str(result)
    assert saved[3] == "customer_verbatim"
    assert saved[4] == _conv_prov()
    evt = uow.bus.published[0]
    assert isinstance(evt, HandoffRequested)
    assert evt.tenant_id == TenantId("t1")
    assert evt.handoff_id == result
    assert evt.trigger == "quote_requested"
    assert uow.committed == 1


async def test_request_handoff_idempotent() -> None:
    """tenant+opportunity 已有 pending → 返回既有，不重复 save/add/event。"""
    factory = _UoWFactory()
    service = _make_service(factory)
    existing = _packet("ho-1", opportunity_id="opp-1")
    factory.seed_pending(existing)

    result = await service.request_handoff(TenantId("t1"), _request())

    assert result == HandoffId("ho-1")
    uow = factory.created[0]
    assert uow.provenance.saved == []
    assert uow.handoffs.added == []
    assert uow.bus.published == []


async def test_request_handoff_opportunity_not_found() -> None:
    """机会不存在或不属于租户 → ValidationError（不把 FK 错误推迟到 commit）。"""
    factory = _UoWFactory()
    service = _make_service(factory)

    with pytest.raises(ValidationError):
        await service.request_handoff(TenantId("t1"), _request())
    uow = factory.created[0]
    assert uow.rolled_back == 1
    assert uow.handoffs.added == []
    assert uow.provenance.saved == []
    assert uow.bus.published == []


async def test_request_handoff_invalid_trigger() -> None:
    """非法 trigger → ValidationError（不泄漏内置 ValueError；回滚无副作用）。"""
    factory = _UoWFactory()
    service = _make_service(factory)

    with pytest.raises(ValidationError):
        await service.request_handoff(TenantId("t1"), _request(trigger="not_a_trigger"))
    uow = factory.created[0]
    assert uow.rolled_back == 1
    assert uow.committed == 0
    assert uow.provenance.saved == []
    assert uow.handoffs.added == []
    assert uow.bus.published == []


async def test_request_handoff_idempotent_before_validation() -> None:
    """已有 pending：完全跳过本次输入校验/解析/ID 生成/副作用，直接返回既有。"""
    factory = _UoWFactory()
    service = _make_service(factory)
    existing = _packet("ho-1", opportunity_id="opp-1")
    factory.seed_pending(existing)
    bad_request = _request(
        account_name="   ",  # 空白关键字段
        customer_verbatim_provenance=_conv_prov(source_type=SourceType.AGENT_INFERENCE),  # 非法来源
        trigger="not_a_trigger",  # 非法 trigger
    )

    result = await service.request_handoff(TenantId("t1"), bad_request)

    assert result == HandoffId("ho-1")
    uow = factory.created[0]
    assert uow.provenance.saved == []
    assert uow.handoffs.added == []
    assert uow.bus.published == []


async def test_request_handoff_id_format() -> None:
    """HandoffId 用 new_id("hand")：startswith("hand_") 且 len<=32（DB String(32)）。"""
    factory = _UoWFactory()
    service = _make_service(factory)
    factory.seed_opp(_opp())

    result = await service.request_handoff(TenantId("t1"), _request())

    s = str(result)
    assert s.startswith("hand_")
    assert len(s) <= 32


@pytest.mark.parametrize(
    "source", [SourceType.CONVERSATION, SourceType.UPLOAD, SourceType.EMPLOYEE_INPUT]
)
async def test_request_handoff_allows_verbatim_sources(source: SourceType) -> None:
    """conversation/upload/employee_input 三种来源都允许创建接管包。"""
    factory = _UoWFactory()
    service = _make_service(factory)
    factory.seed_opp(_opp())

    result = await service.request_handoff(
        TenantId("t1"), _request(customer_verbatim_provenance=_conv_prov(source_type=source))
    )

    assert result is not None
    assert factory.created[0].committed == 1


async def test_request_handoff_copies_all_list_fields() -> None:
    """构造 packet 时四个 list 字段全部复制，避免入参可变列表别名。"""
    factory = _UoWFactory()
    service = _make_service(factory)
    factory.seed_opp(_opp())
    missing = ["spec 表"]
    already = ["初版报价"]
    commits = ["样品免费"]
    links = ["https://example.com/1"]

    await service.request_handoff(
        TenantId("t1"),
        _request(
            missing_information=missing,
            already_sent=already,
            commitments_made=commits,
            evidence_links=links,
        ),
    )

    packet = factory.created[0].handoffs.added[0]
    assert packet.missing_information is not missing
    assert packet.missing_information == ["spec 表"]
    assert packet.already_sent is not already
    assert packet.commitments_made is not commits
    assert packet.evidence_links is not links


# --- accept_handoff -----------------------------------------------------------


async def test_accept_handoff_concurrent() -> None:
    """accept_if_requested True → 发布 HandoffAccepted（同一次注入 now、带 accepted_by）；False → 抛错。"""
    factory = _UoWFactory()
    service = _make_service(factory)

    await service.accept_handoff(TenantId("t1"), HandoffId("ho-1"), EmployeeId("e1"))

    uow = factory.created[0]
    assert uow.handoffs.accept_calls[0] == (
        TenantId("t1"), HandoffId("ho-1"), EmployeeId("e1"), _NOW,
    )
    evt = uow.bus.published[0]
    assert isinstance(evt, HandoffAccepted)
    assert evt.occurred_at == _NOW  # accepted_at/occurred_at 同一次注入 now
    assert evt.accepted_by == EmployeeId("e1")
    assert evt.handoff_id == HandoffId("ho-1")
    assert uow.committed == 1

    factory2 = _UoWFactory()
    factory2.default_accept_result = False
    service2 = _make_service(factory2)
    with pytest.raises(HandoffAlreadyAcceptedError):
        await service2.accept_handoff(TenantId("t1"), HandoffId("ho-1"), EmployeeId("e1"))
    assert factory2.created[0].bus.published == []


# --- get_queue_stats -----------------------------------------------------------


async def test_get_queue_stats_uses_policy() -> None:
    """等号边界：wait==sla 不 breached；by_employee 来自 count_pending_by_employee 聚合；
    list_pending 用 sys.maxsize 技术扫描上限（非业务阈值）。"""
    factory = _UoWFactory()
    service = _make_service(factory)  # sla=3600, backlog=40
    factory.seed_pending_list([
        _packet("ho-1", requested_at=_NOW - timedelta(seconds=3600), assigned_to=EmployeeId("e1")),
    ])
    factory.seed_count_rows({"e1": 3, "e2": 5})  # 与 pending 内容故意不同 → 证明走聚合

    stats = await service.get_queue_stats(TenantId("t1"))

    assert stats.queue_depth == 1
    assert stats.oldest_wait_seconds == 3600
    assert stats.breached_count == 0  # wait == sla 不算
    assert stats.by_employee == {"e1": 3, "e2": 5}  # 来自 count_pending_by_employee
    assert factory.created[0].handoffs.count_calls[0] == (TenantId("t1"),)
    assert factory.created[0].handoffs.pending_limits[0] == sys.maxsize
    assert factory.created[0].bus.published == []  # 未超不发布

    factory2 = _UoWFactory()
    service2 = _make_service(factory2)
    factory2.seed_pending_list([
        _packet("ho-2", requested_at=_NOW - timedelta(seconds=3601), assigned_to=EmployeeId("e1")),
    ])
    factory2.seed_count_rows({"e1": 1})
    stats2 = await service2.get_queue_stats(TenantId("t1"))
    assert stats2.breached_count == 1  # 超过 sla 才算


async def test_get_queue_stats_depth_equals_threshold() -> None:
    """depth == backlog_threshold（40）→ 不 backlogged 且不发事件；41 才 true。"""
    factory = _UoWFactory()
    service = _make_service(factory)
    factory.seed_pending_list([_packet(f"ho-{i}") for i in range(40)])  # depth == 40
    factory.seed_count_rows({"e1": 40})

    stats = await service.get_queue_stats(TenantId("t1"))

    assert stats.queue_depth == 40
    assert stats.is_backlogged is False  # 等于不算
    assert factory.created[0].bus.published == []

    factory2 = _UoWFactory()
    service2 = _make_service(factory2)
    factory2.seed_pending_list([_packet(f"ho-{i}") for i in range(41)])  # depth 41 > 40
    factory2.seed_count_rows({"e1": 41})
    stats2 = await service2.get_queue_stats(TenantId("t1"))
    assert stats2.is_backlogged is True
    evt = factory2.created[0].bus.published[0]
    assert isinstance(evt, HandoffQueueBacklogged)
    assert evt.queue_depth == 41
    assert evt.tenant_id == TenantId("t1")


async def test_get_queue_stats_oldest_is_max_not_order() -> None:
    """最久等待 = max，不依赖待接管输入顺序。"""
    factory = _UoWFactory()
    service = _make_service(factory)
    factory.seed_pending_list([
        _packet("ho-new", requested_at=_NOW),
        _packet("ho-old", requested_at=_NOW - timedelta(hours=5)),
        _packet("ho-mid", requested_at=_NOW - timedelta(hours=2)),
    ])

    stats = await service.get_queue_stats(TenantId("t1"))

    assert stats.oldest_wait_seconds == 5 * 3600
    assert stats.queue_depth == 3


# --- get / get_handoff_packet ----------------------------------------------------


async def test_get_view_gate_explanation() -> None:
    """get 从最新快照完整复制 ScoreExplanation；列表/字典不别名。"""
    factory = _UoWFactory()
    service = _make_service(factory)
    snap = _snap(
        sort_key=SortKey(5, 2, 2),
        rank_bucket="high",
        passed_gates=["contactable", "category_allowed"],
        failed_gates=[],
        gate_reasons={"contactable": "ok"},
    )
    factory.seed_opp(_opp())
    factory.seed_latest(snap)

    view = await service.get(TenantId("t1"), OpportunityId("opp-1"))

    assert view.score is not None
    assert view.score.sort_key == SortKey(5, 2, 2)
    assert view.score.rank_bucket == "high"
    assert view.score.passed_gates == ["contactable", "category_allowed"]
    assert view.score.failed_gates == []
    assert view.score.gate_reasons == {"contactable": "ok"}
    assert view.score.scored_at == _NOW
    assert view.score.scorer_version == "gates-v1"
    assert view.score.passed_gates is not snap.passed_gates  # 复制，非别名


async def test_get_view_includes_score_summary() -> None:
    """无快照 → score=None；has_pending_handoff 来自 repo。"""
    factory = _UoWFactory()
    service = _make_service(factory)
    factory.seed_opp(_opp())

    view = await service.get(TenantId("t1"), OpportunityId("opp-1"))
    assert view.score is None
    assert view.state == "qualified"
    assert view.owner is None
    assert view.has_pending_handoff is False

    factory2 = _UoWFactory()
    service2 = _make_service(factory2)
    factory2.seed_opp(_opp())
    factory2.seed_pending(_packet("ho-1", opportunity_id="opp-1"))
    view2 = await service2.get(TenantId("t1"), OpportunityId("opp-1"))
    assert view2.has_pending_handoff is True


async def test_get_handoff_packet_maps_and_copies() -> None:
    """get_handoff_packet 完整映射、wait_seconds 用注入 now、四个 list 字段复制、assigned_to_name=None。"""
    factory = _UoWFactory()
    service = _make_service(factory)
    missing = ["spec 表"]
    already = ["初版报价"]
    commits = ["样品免费"]
    links = ["https://example.com/1"]
    packet = _packet(
        "ho-1",
        requested_at=_NOW - timedelta(seconds=90),
        missing_information=missing,
        already_sent=already,
        commitments_made=commits,
        evidence_links=links,
    )
    factory.seed_row(packet)

    view = await service.get_handoff_packet(TenantId("t1"), HandoffId("ho-1"))

    assert view.handoff_id == HandoffId("ho-1")
    assert view.wait_seconds == 90
    assert view.missing_information == ["spec 表"]
    assert view.missing_information is not missing
    assert view.already_sent is not already
    assert view.commitments_made is not commits
    assert view.evidence_links is not links
    assert view.state == "requested"
    assert view.trigger == "quote_requested"
    assert view.assigned_to_name is None


async def test_get_not_found_raises() -> None:
    """get / get_handoff_packet 查不到 → ValidationError。"""
    factory = _UoWFactory()
    service = _make_service(factory)

    with pytest.raises(ValidationError):
        await service.get(TenantId("t1"), OpportunityId("opp-1"))
    with pytest.raises(ValidationError):
        await service.get_handoff_packet(TenantId("t1"), HandoffId("ho-1"))


# --- list_for_employee / loss_reason_breakdown ------------------------------------


async def test_list_for_employee_validates_limit_and_single_uow() -> None:
    """limit<=0 → ValidationError；states/limit 原样传 repo；单 UoW 构造全部 View；
    两个不同机会返回两个不同 opportunity_id（不按 ID 重查）。"""
    factory = _UoWFactory()
    service = _make_service(factory)
    with pytest.raises(ValidationError):
        await service.list_for_employee(TenantId("t1"), EmployeeId("e1"), limit=0)

    factory2 = _UoWFactory()
    service2 = _make_service(factory2)
    factory2.seed_owner_rows([_opp("opp-1"), _opp("opp-2")])  # 不 seed opportunities.get
    views = await service2.list_for_employee(
        TenantId("t1"), EmployeeId("e1"), states=[OpportunityState.ASSIGNED], limit=10
    )

    assert [v.opportunity_id for v in views] == [OpportunityId("opp-1"), OpportunityId("opp-2")]
    call = factory2.created[0].opportunities.list_calls[0]
    assert call[0] == TenantId("t1")
    assert call[1] == EmployeeId("e1")
    assert call[2] == [OpportunityState.ASSIGNED]
    assert call[3] == 10
    assert len(factory2.created) == 1  # 单 UoW，每项不另开


async def test_loss_reason_breakdown_2d() -> None:
    """repo 行 → {reason:{state:count}}；同键安全累加。"""
    factory = _UoWFactory()
    service = _make_service(factory)
    factory.seed_loss_rows([
        ("price_too_high", "quoted", 2),
        ("price_too_high", "quoted", 1),
        ("no_reply", "contacted", 3),
    ])

    breakdown = await service.loss_reason_breakdown(TenantId("t1"), since_days=30)

    assert breakdown == {
        "price_too_high": {"quoted": 3},
        "no_reply": {"contacted": 3},
    }
    call = factory.created[0].loss_records.calls[0]
    assert call[0] == TenantId("t1")
    assert call[1] == 30


async def test_loss_reason_breakdown_validates_since_days() -> None:
    """since_days<=0 → ValidationError。"""
    service = _make_service(_UoWFactory())
    with pytest.raises(ValidationError):
        await service.loss_reason_breakdown(TenantId("t1"), since_days=0)
