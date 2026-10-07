"""S3-13 scoped 机会列表与 pending 接管队列的服务层行为测试。

这些测试把 SQL scope 当成仓储边界合同，同时验证服务层仍逐行做 ABAC
防御、Provenance 全历史映射、一次时钟快照与授权审计顺序。
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self, cast

import pytest

from domains.opportunities import models
from domains.opportunities.permissions import (
    Actor,
    OpportunityAction,
    OpportunityScope,
    ScopeLevel,
)
from domains.opportunities.service_impl import OpportunityServiceImpl
from shared.errors import PermissionDenied, TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import (
    EmployeeId,
    HandoffId,
    OpportunityId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.provenance import Provenance, SourceType

HandoffPacket = models.HandoffPacket
HandoffPolicy = models.HandoffPolicy
HandoffState = models.HandoffState
HandoffTrigger = models.HandoffTrigger
Opportunity = models.Opportunity
OpportunityState = models.OpportunityState

_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
_TENANT = TenantId("tenant-list")


def _load(symbol: str):
    """延迟加载 S3-13 新合同；缺失表现为测试失败而非收集错误。"""
    modules = {
        "HandoffQueueItemView": "domains.opportunities.schemas",
        "ProvenanceSummary": "domains.opportunities.schemas",
    }
    try:
        return getattr(importlib.import_module(modules[symbol]), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{symbol} 尚未实现（{exc}）")


def _opp(
    opportunity_id: str,
    *,
    tenant_id: TenantId = _TENANT,
    owner: str | None = "sales-1",
    country: str = "US",
    category: str = "hinges",
    state: OpportunityState = OpportunityState.ASSIGNED,
) -> Opportunity:
    return Opportunity(
        opportunity_id=OpportunityId(opportunity_id),
        tenant_id=tenant_id,
        account_id=ProspectAccountId(f"account-{opportunity_id}"),
        need_id=ValidatedNeedId(f"need-{opportunity_id}"),
        product_category=category,
        created_at=_NOW - timedelta(days=1),
        account_name=f"Account {opportunity_id}",
        country=country,
        state=state,
        owner=EmployeeId(owner) if owner is not None else None,
    )


def _packet(
    handoff_id: str,
    opportunity_id: str,
    *,
    tenant_id: TenantId = _TENANT,
    assigned_to: str | None = "sales-1",
    country: str = "US",
    requested_at: datetime | None = None,
    state: HandoffState = HandoffState.REQUESTED,
) -> HandoffPacket:
    return HandoffPacket(
        handoff_id=HandoffId(handoff_id),
        tenant_id=tenant_id,
        opportunity_id=OpportunityId(opportunity_id),
        trigger=HandoffTrigger.QUOTE_REQUESTED,
        requested_at=requested_at or (_NOW - timedelta(hours=2)),
        account_name=f"Account {opportunity_id}",
        country=country,
        why_valuable="客户明确要求正式报价",
        customer_verbatim="Please send a formal quote.",
        state=state,
        assigned_to=EmployeeId(assigned_to) if assigned_to is not None else None,
        suggested_next_step="核对规格后准备报价",
        missing_information=["交货港口"],
        evidence_links=["/artifacts/message-1"],
    )


def _provenance(source_id: str, *, extracted_at: datetime) -> Provenance:
    return Provenance(
        source_type=SourceType.CONVERSATION,
        source_id=source_id,
        extracted_by="extractor-v1",
        extracted_at=extracted_at,
        confirmed_by=EmployeeId("manager-1"),
        confirmed_at=extracted_at + timedelta(minutes=1),
    )


class _FakeOpportunityRepo:
    def __init__(self, sequence: list[str]) -> None:
        self._sequence = sequence
        self.scoped_rows: list[Opportunity] = []
        self.rows: dict[OpportunityId, Opportunity] = {}
        self.scoped_calls: list[tuple[object, ...]] = []
        self.owner_calls: list[tuple[object, ...]] = []

    async def get(
        self, tenant_id: TenantId, opportunity_id: OpportunityId
    ) -> Opportunity | None:
        self._sequence.append("opportunity:get")
        return self.rows.get(opportunity_id)

    async def list_scoped(
        self,
        tenant_id: TenantId,
        scope: OpportunityScope,
        states: list[OpportunityState] | None,
        limit: int,
    ) -> list[Opportunity]:
        self._sequence.append("opportunity:list_scoped")
        self.scoped_calls.append((tenant_id, scope, states, limit))
        return list(self.scoped_rows)

    async def list_by_owner(
        self,
        tenant_id: TenantId,
        owner: EmployeeId,
        states: list[OpportunityState] | None,
        limit: int,
    ) -> list[Opportunity]:
        self._sequence.append("opportunity:list_by_owner")
        self.owner_calls.append((tenant_id, owner, states, limit))
        return list(self.scoped_rows)


class _FakeSnapshotRepo:
    async def latest_for_opportunity(self, tenant_id, opportunity_id):
        return None


class _FakeHandoffRepo:
    def __init__(self, sequence: list[str]) -> None:
        self._sequence = sequence
        self.scoped_rows: list[HandoffPacket] = []
        self.pending_by_opportunity: dict[OpportunityId, HandoffPacket] = {}
        self.scoped_calls: list[tuple[object, ...]] = []

    async def list_pending_scoped(
        self, tenant_id: TenantId, scope: OpportunityScope, limit: int
    ) -> list[HandoffPacket]:
        self._sequence.append("handoff:list_pending_scoped")
        self.scoped_calls.append((tenant_id, scope, limit))
        return list(self.scoped_rows)

    async def find_pending_for_opportunity(self, tenant_id, opportunity_id):
        return self.pending_by_opportunity.get(opportunity_id)


class _FakeLossRepo:
    pass


class _FakeProvenanceRepo:
    def __init__(self, sequence: list[str]) -> None:
        self._sequence = sequence
        self.rows: dict[tuple[str, str], list[tuple[str, Provenance]]] = {}
        self.calls: list[tuple[object, ...]] = []
        self.error: BaseException | None = None

    async def list_for_entity(
        self, tenant_id: TenantId, entity_type: str, entity_id: str
    ) -> list[tuple[str, Provenance]]:
        self._sequence.append("provenance:list")
        self.calls.append((tenant_id, entity_type, entity_id))
        if self.error is not None:
            raise self.error
        return list(self.rows.get((entity_type, entity_id), []))


class _FakeBus:
    pass


class _FakeUoW:
    def __init__(self, sequence: list[str]) -> None:
        self._sequence = sequence
        self.opportunities = _FakeOpportunityRepo(sequence)
        self.snapshots = _FakeSnapshotRepo()
        self.handoffs = _FakeHandoffRepo(sequence)
        self.loss_records = _FakeLossRepo()
        self.provenance = _FakeProvenanceRepo(sequence)
        self.bus = _FakeBus()

    async def __aenter__(self) -> Self:
        self._sequence.append("uow:enter")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._sequence.append("uow:exit")


class _Factory:
    def __init__(self, uow: _FakeUoW, sequence: list[str]) -> None:
        self._uow = uow
        self._sequence = sequence
        self.calls = 0

    def __call__(self) -> _FakeUoW:
        self.calls += 1
        self._sequence.append("uow:factory")
        return self._uow


class _Authorizer:
    def __init__(self, sequence: list[str], *, deny: bool = False) -> None:
        self._sequence = sequence
        self._deny = deny
        self.calls: list[tuple[object, ...]] = []

    def require(self, actor, action, scope, tenant_id) -> str:
        self._sequence.append("authorize")
        self.calls.append((actor, action, scope, tenant_id))
        if self._deny:
            raise PermissionDenied("固定拒绝")
        return "allow:test"


class _Audit:
    def __init__(self, sequence: list[str]) -> None:
        self._sequence = sequence
        self.rows: list[dict[str, object]] = []

    def log(self, **kwargs: object) -> None:
        self._sequence.append(f"audit:{kwargs['rule']}")
        self.rows.append(kwargs)


class _NeverScorer:
    async def score(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("列表查询不应调用 scorer")


class _Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> datetime:
        self.calls += 1
        return self.value


def _tenant_scope() -> OpportunityScope:
    return OpportunityScope(level=ScopeLevel.TENANT)


def _owner_scope(owner: str = "sales-1") -> OpportunityScope:
    return OpportunityScope(
        level=ScopeLevel.SELF,
        allowed_owners=frozenset({EmployeeId(owner)}),
    )


def _actor(scope: OpportunityScope | None = None) -> Actor:
    selected = scope or _tenant_scope()
    actor_id = "sales-1" if selected.level is ScopeLevel.SELF else "boss-1"
    return Actor(actor_id=actor_id, role="sales", scope=selected)


def _service(
    *,
    sequence: list[str] | None = None,
    deny: bool = False,
    clock: _Clock | None = None,
) -> tuple[OpportunityServiceImpl, _FakeUoW, _Factory, _Authorizer, _Audit, _Clock]:
    calls = sequence if sequence is not None else []
    uow = _FakeUoW(calls)
    factory = _Factory(uow, calls)
    authorizer = _Authorizer(calls, deny=deny)
    audit = _Audit(calls)
    now = clock or _Clock(_NOW)
    service = OpportunityServiceImpl(
        factory,
        cast(object, _NeverScorer()),
        HandoffPolicy(sla_seconds=3600, backlog_threshold=20),
        authorizer=authorizer,
        audit=audit,
        now=now,
    )
    return service, uow, factory, authorizer, audit, now


def test_public_list_dtos_are_frozen_and_lists_are_not_aliased() -> None:
    """捕获 DTO 可变、枚举/强类型 ID 外泄与共享 list 默认值。"""
    ProvenanceSummary = _load("ProvenanceSummary")
    HandoffQueueItemView = _load("HandoffQueueItemView")
    summary = ProvenanceSummary(
        field_name="quantity",
        source_type="conversation",
        source_id="message-1",
        extracted_by="extractor-v1",
        extracted_at=_NOW,
        confirmed_by="manager-1",
        confirmed_at=_NOW,
        source_url=None,
        page_hash=None,
    )
    missing = ["destination"]
    links = ["/artifacts/message-1"]
    item = HandoffQueueItemView(
        handoff_id="handoff-1",
        opportunity_id="opportunity-1",
        trigger="quote_requested",
        account_name="Acme",
        country="US",
        why_valuable="客户要求报价",
        customer_verbatim="Please quote.",
        requested_at=_NOW,
        wait_seconds=120,
        state="requested",
        assigned_to="sales-1",
        suggested_next_step="确认规格",
        missing_information=missing,
        evidence_links=links,
    )
    missing.append("quantity")
    links.append("/artifacts/message-2")
    assert item.missing_information == ["destination"]
    assert item.evidence_links == ["/artifacts/message-1"]
    assert summary.source_type == "conversation"
    assert summary.confirmed_by == "manager-1"
    with pytest.raises(FrozenInstanceError):
        summary.source_id = "changed"
    with pytest.raises(FrozenInstanceError):
        item.state = "accepted"


@pytest.mark.asyncio
async def test_list_opportunities_uses_requested_scope_and_full_provenance_history() -> None:
    """捕获把 actor.scope 偷换进 authorizer、丢 provenance 历史或跨 UoW 读取。"""
    sequence: list[str] = []
    service, uow, factory, authorizer, audit, _ = _service(sequence=sequence)
    scope = _tenant_scope()
    actor = _actor(scope)
    row = _opp("opp-1")
    uow.opportunities.scoped_rows = [row]
    newer = _provenance("message-new", extracted_at=_NOW)
    older = _provenance("message-old", extracted_at=_NOW - timedelta(days=1))
    uow.provenance.rows[("opportunity", "opp-1")] = [
        ("quantity", newer),
        ("quantity", older),
    ]

    result = await service.list_opportunities(
        _TENANT,
        actor,
        scope=scope,
        states=[OpportunityState.ASSIGNED],
        limit=7,
    )

    assert len(result) == 1
    assert [p.source_id for p in result[0].provenance] == [
        "message-new",
        "message-old",
    ]
    assert result[0].provenance[0].field_name == "quantity"
    assert uow.opportunities.scoped_calls == [
        (_TENANT, scope, [OpportunityState.ASSIGNED], 7)
    ]
    assert uow.provenance.calls == [(_TENANT, "opportunity", "opp-1")]
    assert authorizer.calls == [
        (actor, OpportunityAction.OPPORTUNITY_LIST, scope, _TENANT)
    ]
    assert factory.calls == 1
    assert sequence.index("provenance:list") < sequence.index("audit:allow:test")
    assert audit.rows[-1]["rule"] == "allow:test"


@pytest.mark.asyncio
async def test_get_and_legacy_employee_list_share_provenance_mapping() -> None:
    """捕获只给新列表加 provenance、详情/旧列表仍返回空摘要的回归。"""
    service, uow, _, _, _, _ = _service()
    actor = _actor()
    row = _opp("opp-shared")
    prov = _provenance("message-shared", extracted_at=_NOW)
    uow.opportunities.rows[row.opportunity_id] = row
    uow.opportunities.scoped_rows = [row]
    uow.provenance.rows[("opportunity", "opp-shared")] = [("country", prov)]

    detail = await service.get(_TENANT, row.opportunity_id, actor=actor)
    legacy = await service.list_for_employee(
        _TENANT,
        EmployeeId("sales-1"),
        actor=actor,
        limit=5,
    )

    assert [p.source_id for p in detail.provenance] == ["message-shared"]
    assert [p.source_id for p in legacy[0].provenance] == ["message-shared"]
    assert uow.provenance.calls == [
        (_TENANT, "opportunity", "opp-shared"),
        (_TENANT, "opportunity", "opp-shared"),
    ]


@pytest.mark.asyncio
async def test_list_opportunities_scope_mismatch_denies_after_authorizer_before_uow() -> None:
    """捕获调用方传更宽 scope 绕过 actor.scope 的越权。"""
    sequence: list[str] = []
    service, _, factory, authorizer, audit, _ = _service(sequence=sequence)
    actor = _actor(_owner_scope())
    requested = _tenant_scope()

    with pytest.raises(PermissionDenied, match="请求作用域与身份作用域不一致"):
        await service.list_opportunities(
            _TENANT, actor, scope=requested, states=None, limit=10
        )

    assert authorizer.calls == [
        (actor, OpportunityAction.OPPORTUNITY_LIST, requested, _TENANT)
    ]
    assert factory.calls == 0
    assert sequence == ["authorize", "audit:deny:scope_mismatch"]
    assert audit.rows == [
        {
            "actor": actor.actor_id,
            "action": OpportunityAction.OPPORTUNITY_LIST.value,
            "tenant_id": _TENANT,
            "scope": requested.label,
            "rule": "deny:scope_mismatch",
        }
    ]


@pytest.mark.parametrize(
    "scope",
    [
        OpportunityScope(),
        OpportunityScope(level=ScopeLevel.SYSTEM),
    ],
    ids=["missing-level", "unrestricted-system"],
)
@pytest.mark.asyncio
async def test_list_opportunities_rejects_fail_open_scope(scope: OpportunityScope) -> None:
    """捕获无级别或无任何维度的 SYSTEM 被误当 tenant-wide。"""
    sequence: list[str] = []
    service, _, factory, authorizer, audit, _ = _service(sequence=sequence)
    actor = Actor(actor_id="system-lookup", role="system", scope=scope)

    with pytest.raises(PermissionDenied, match="查询作用域未明确收窄"):
        await service.list_opportunities(_TENANT, actor, scope=scope)

    assert len(authorizer.calls) == 1
    assert factory.calls == 0
    assert audit.rows[-1]["rule"] == "deny:unsafe_scope"


@pytest.mark.asyncio
async def test_authorization_precedes_invalid_limit_and_states() -> None:
    """捕获未授权者用非法业务参数探测校验规则。"""
    sequence: list[str] = []
    service, _, factory, _, audit, _ = _service(sequence=sequence, deny=True)
    actor = _actor()

    with pytest.raises(PermissionDenied):
        await service.list_opportunities(
            _TENANT,
            actor,
            scope=actor.scope,
            states=cast(list[OpportunityState], ["not-a-state"]),
            limit=0,
        )
    with pytest.raises(PermissionDenied):
        await service.list_pending_handoffs(_TENANT, actor, limit=0)

    assert factory.calls == 0
    assert sequence == ["authorize", "audit:deny", "authorize", "audit:deny"]
    assert [row["rule"] for row in audit.rows] == ["deny", "deny"]


@pytest.mark.asyncio
async def test_authorized_invalid_states_and_limit_are_validation_errors() -> None:
    """捕获非法 state 在仓储层触发 AttributeError，或非正 limit 被 SQL 接受。"""
    service, _, factory, _, _, _ = _service()
    actor = _actor()

    with pytest.raises(ValidationError, match="states 必须是 OpportunityState"):
        await service.list_opportunities(
            _TENANT,
            actor,
            scope=actor.scope,
            states=cast(list[OpportunityState], ["bad"]),
        )
    with pytest.raises(ValidationError, match="limit 必须 > 0"):
        await service.list_pending_handoffs(_TENANT, actor, limit=0)

    assert factory.calls == 0


@pytest.mark.asyncio
async def test_empty_states_means_no_selection() -> None:
    """捕获 states=[] 被归一化成 None 后返回全部机会。"""
    service, uow, _, _, _, _ = _service()
    actor = _actor()
    uow.opportunities.scoped_rows = [_opp("should-not-leak")]

    result = await service.list_opportunities(
        _TENANT, actor, scope=actor.scope, states=[], limit=5
    )

    assert result == []
    assert uow.provenance.calls == []


@pytest.mark.asyncio
async def test_list_opportunities_rejects_any_out_of_scope_repository_row() -> None:
    """捕获 SQL 仓储回归时服务静默过滤越界行并返回部分结果。"""
    sequence: list[str] = []
    service, uow, _, _, audit, _ = _service(sequence=sequence)
    scope = OpportunityScope(
        level=ScopeLevel.MANAGER,
        allowed_countries=frozenset({"US"}),
    )
    actor = Actor(actor_id="manager-1", role="manager", scope=scope)
    uow.opportunities.scoped_rows = [
        _opp("allowed", country="US"),
        _opp("forbidden", country="DE"),
    ]

    with pytest.raises(PermissionDenied, match="国家/地区"):
        await service.list_opportunities(_TENANT, actor, scope=scope)

    assert uow.provenance.calls == []
    assert [row["rule"] for row in audit.rows] == ["deny:abac:country"]


@pytest.mark.asyncio
async def test_list_opportunities_rejects_cross_tenant_repository_row() -> None:
    """捕获仓储漏租户过滤后，服务把跨租户行组装成 DTO。"""
    service, uow, _, _, audit, _ = _service()
    actor = _actor()
    uow.opportunities.scoped_rows = [
        _opp("cross-tenant", tenant_id=TenantId("tenant-other"))
    ]

    with pytest.raises(TenantIsolationViolation) as caught:
        await service.list_opportunities(_TENANT, actor, scope=actor.scope)

    assert str(caught.value) == "机会列表数据租户不一致"
    assert uow.provenance.calls == []
    assert [row["rule"] for row in audit.rows] == ["deny:tenant_isolation"]


@pytest.mark.asyncio
async def test_opportunity_tenant_isolation_emits_fixed_critical_alert(caplog) -> None:
    """捕获跨租户机会只写 info 审计、没有高严重度安全告警。"""
    service, uow, _, _, _, _ = _service()
    actor = _actor()
    forbidden = ("secret-opportunity-id", "Account secret-opportunity-id")
    uow.opportunities.scoped_rows = [
        _opp(forbidden[0], tenant_id=TenantId("tenant-other"))
    ]

    with (
        caplog.at_level(logging.CRITICAL, logger="security.tenant_isolation"),
        pytest.raises(TenantIsolationViolation),
    ):
        await service.list_opportunities(_TENANT, actor, scope=actor.scope)

    records = [
        record
        for record in caplog.records
        if record.name == "security.tenant_isolation"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.CRITICAL
    assert record.getMessage() == "检测到跨租户数据隔离违规"
    assert record.actor == actor.actor_id
    assert record.action == OpportunityAction.OPPORTUNITY_LIST.value
    assert record.tenant_id == str(_TENANT)
    assert record.scope == actor.scope.label
    assert record.rule == "deny:tenant_isolation"
    rendered = f"{record.getMessage()} {record.__dict__}"
    assert all(value not in rendered for value in forbidden)


@pytest.mark.asyncio
async def test_allow_audit_waits_until_all_view_assembly_succeeds() -> None:
    """捕获 provenance 读取失败后仍留下虚假的 allow 审计。"""
    sequence: list[str] = []
    service, uow, _, _, audit, _ = _service(sequence=sequence)
    actor = _actor()
    uow.opportunities.scoped_rows = [_opp("opp-broken-prov")]
    uow.provenance.error = ValidationError("来源记录损坏")

    with pytest.raises(ValidationError, match="来源记录损坏"):
        await service.list_opportunities(_TENANT, actor, scope=actor.scope)

    assert audit.rows == []


@pytest.mark.asyncio
async def test_pending_handoffs_use_one_now_snapshot_and_copy_packet_lists() -> None:
    """捕获逐行取时钟造成 wait 漂移、重排队列或 DTO list 与实体别名。"""
    clock = _Clock(_NOW)
    service, uow, _, authorizer, audit, _ = _service(clock=clock)
    scope = _owner_scope()
    actor = _actor(scope)
    oldest = _packet(
        "handoff-old",
        "opp-old",
        requested_at=_NOW - timedelta(hours=3),
    )
    newer = _packet(
        "handoff-new",
        "opp-new",
        requested_at=_NOW - timedelta(hours=1),
    )
    uow.handoffs.scoped_rows = [oldest, newer]
    uow.opportunities.rows = {
        OpportunityId("opp-old"): _opp("opp-old"),
        OpportunityId("opp-new"): _opp("opp-new"),
    }

    result = await service.list_pending_handoffs(_TENANT, actor, limit=9)

    assert [item.handoff_id for item in result] == ["handoff-old", "handoff-new"]
    assert [item.wait_seconds for item in result] == [10800, 3600]
    assert all(type(item.wait_seconds) is int for item in result)
    assert result[0].assigned_to == "sales-1"
    assert uow.handoffs.scoped_calls == [(_TENANT, scope, 9)]
    assert authorizer.calls == [
        (actor, OpportunityAction.HANDOFF_QUEUE_READ, scope, _TENANT)
    ]
    assert clock.calls == 1
    oldest.missing_information.append("quantity")
    oldest.evidence_links.append("/artifacts/message-2")
    assert result[0].missing_information == ["交货港口"]
    assert result[0].evidence_links == ["/artifacts/message-1"]
    assert audit.rows[-1]["rule"] == "allow:test"


@pytest.mark.asyncio
async def test_pending_handoff_abac_uses_authoritative_opportunity_country() -> None:
    """捕获使用可改写的 packet.country 作为 ABAC 依据。"""
    service, uow, _, _, audit, _ = _service()
    scope = OpportunityScope(
        level=ScopeLevel.MANAGER,
        allowed_countries=frozenset({"US"}),
    )
    actor = Actor(actor_id="manager-1", role="manager", scope=scope)
    packet = _packet("handoff-country", "opp-country", country="US")
    uow.handoffs.scoped_rows = [packet]
    uow.opportunities.rows[OpportunityId("opp-country")] = _opp(
        "opp-country", country="DE"
    )

    with pytest.raises(PermissionDenied, match="国家/地区"):
        await service.list_pending_handoffs(_TENANT, actor)

    assert [row["rule"] for row in audit.rows] == ["deny:abac:country"]


@pytest.mark.asyncio
async def test_pending_handoff_missing_opportunity_is_fixed_validation_error() -> None:
    """捕获孤儿接管数据被部分返回，或错误消息反射业务 ID。"""
    service, uow, _, _, audit, _ = _service()
    actor = _actor()
    uow.handoffs.scoped_rows = [_packet("secret-handoff", "secret-opportunity")]

    with pytest.raises(ValidationError) as caught:
        await service.list_pending_handoffs(_TENANT, actor)

    assert str(caught.value) == "接管数据关联机会缺失"
    assert audit.rows == []


@pytest.mark.asyncio
async def test_pending_handoff_rejects_cross_tenant_packet_before_lookup() -> None:
    """捕获 scoped repo 错返跨租户 packet 后继续查关联机会。"""
    sequence: list[str] = []
    service, uow, _, _, audit, _ = _service(sequence=sequence)
    actor = _actor()
    uow.handoffs.scoped_rows = [
        _packet(
            "cross-packet",
            "cross-opportunity",
            tenant_id=TenantId("tenant-other"),
        )
    ]

    with pytest.raises(TenantIsolationViolation) as caught:
        await service.list_pending_handoffs(_TENANT, actor)

    assert str(caught.value) == "待接管列表数据租户不一致"
    assert "opportunity:get" not in sequence
    assert [row["rule"] for row in audit.rows] == ["deny:tenant_isolation"]


@pytest.mark.asyncio
async def test_pending_tenant_isolation_emits_fixed_critical_alert(caplog) -> None:
    """证明 pending 路径复用同一固定、脱敏、高严重度安全告警。"""
    service, uow, _, _, _, _ = _service()
    actor = _actor()
    packet = _packet(
        "secret-handoff-id",
        "secret-linked-opportunity",
        tenant_id=TenantId("tenant-other"),
    )
    uow.handoffs.scoped_rows = [packet]

    with (
        caplog.at_level(logging.CRITICAL, logger="security.tenant_isolation"),
        pytest.raises(TenantIsolationViolation),
    ):
        await service.list_pending_handoffs(_TENANT, actor)

    records = [
        record
        for record in caplog.records
        if record.name == "security.tenant_isolation"
    ]
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.CRITICAL
    assert record.getMessage() == "检测到跨租户数据隔离违规"
    assert record.action == OpportunityAction.HANDOFF_QUEUE_READ.value
    rendered = f"{record.getMessage()} {record.__dict__}"
    for forbidden in (
        "secret-handoff-id",
        "secret-linked-opportunity",
        packet.customer_verbatim,
        packet.why_valuable,
    ):
        assert forbidden not in rendered


@pytest.mark.asyncio
async def test_pending_handoff_rejects_non_requested_packet_before_lookup() -> None:
    """捕获 scoped repo 错返已接受行后被展示成 pending。"""
    sequence: list[str] = []
    service, uow, _, _, audit, _ = _service(sequence=sequence)
    actor = _actor()
    uow.handoffs.scoped_rows = [
        _packet(
            "accepted-packet",
            "accepted-opportunity",
            state=HandoffState.ACCEPTED,
        )
    ]

    with pytest.raises(ValidationError) as caught:
        await service.list_pending_handoffs(_TENANT, actor)

    assert str(caught.value) == "待接管列表包含非 requested 数据"
    assert "opportunity:get" not in sequence
    assert all(row["rule"] != "allow:test" for row in audit.rows)


@pytest.mark.asyncio
async def test_pending_handoff_rejects_cross_tenant_linked_opportunity() -> None:
    """捕获关联机会仓储错返别租户实体。"""
    service, uow, _, _, audit, _ = _service()
    actor = _actor()
    packet = _packet("linked-cross", "linked-cross-opportunity")
    uow.handoffs.scoped_rows = [packet]
    uow.opportunities.rows[packet.opportunity_id] = _opp(
        "linked-cross-opportunity",
        tenant_id=TenantId("tenant-other"),
    )

    with pytest.raises(TenantIsolationViolation) as caught:
        await service.list_pending_handoffs(_TENANT, actor)

    assert str(caught.value) == "接管关联机会租户不一致"
    assert [row["rule"] for row in audit.rows] == ["deny:tenant_isolation"]


@pytest.mark.asyncio
async def test_pending_handoff_rejects_mismatched_linked_opportunity_id() -> None:
    """捕获仓储按错误主键返回实体后继续组装。"""
    service, uow, _, _, audit, _ = _service()
    actor = _actor()
    packet = _packet("linked-mismatch", "expected-opportunity")
    uow.handoffs.scoped_rows = [packet]
    uow.opportunities.rows[packet.opportunity_id] = _opp("different-opportunity")

    with pytest.raises(ValidationError) as caught:
        await service.list_pending_handoffs(_TENANT, actor)

    assert str(caught.value) == "接管关联机会标识不一致"
    assert all(row["rule"] != "allow:test" for row in audit.rows)


@pytest.mark.asyncio
async def test_pending_handoff_rejects_former_owner_after_reassignment() -> None:
    service, uow, _, _, audit, _ = _service()
    uow.handoffs.scoped_rows = [_packet("transferred", "transferred", assigned_to="sales-1")]
    uow.opportunities.rows[OpportunityId("transferred")] = _opp("transferred", owner="sales-2")
    with pytest.raises(PermissionDenied):
        await service.list_pending_handoffs(_TENANT, _actor(_owner_scope("sales-1")))
    assert audit.rows[-1]["rule"] == "deny:abac:owner"


@pytest.mark.asyncio
async def test_pending_handoff_current_owner_can_read_stale_assignment_snapshot() -> None:
    service, uow, *_ = _service()
    uow.handoffs.scoped_rows = [_packet("transferred", "transferred", assigned_to="sales-1")]
    uow.opportunities.rows[OpportunityId("transferred")] = _opp("transferred", owner="sales-2")
    items = await service.list_pending_handoffs(_TENANT, Actor(actor_id="sales-2", role="sales", scope=_owner_scope("sales-2")))
    assert len(items) == 1
    assert items[0].assigned_to == "sales-2"
    assert items[0].customer_verbatim == "Please send a formal quote."
