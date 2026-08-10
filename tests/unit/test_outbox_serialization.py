"""S2-6 outbox 事件序列化单测（EVENT_REGISTRY 白名单 + JSON round-trip）。

覆盖：
- EVENT_REGISTRY 是显式白名单（与 opportunities PUBLISHES 一致），非反射扫描。
- 全部白名单事件可逆 round-trip（TenantId/RunId/其他 NewType、Enum、tz datetime、None）。
- 类型广度：Decimal/Money、嵌套 dataclass、list、dict、Optional。
- 未知事件类型：``resolve_event_type`` 抛 ``ValidationError``（发布/反序列化共同入口）。
- 序列化输出必须可直接 ``json.dumps``（禁止 pickle）。

RED 前置：``infra.db.outbox`` 尚未创建；经 importlib 延迟导入转行为失败。
不输出任何连接串/凭证。
"""
from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from shared.errors import ValidationError
from shared.events.catalog import (
    DomainEvent,
    HandoffAccepted,
    HandoffQueueBacklogged,
    HandoffRequested,
    OpportunityLost,
    OpportunityQualified,
    OpportunityWon,
)
from shared.schemas.evidence import ConfidenceTier
from shared.schemas.identifiers import (
    EmployeeId,
    HandoffId,
    OpportunityId,
    RunId,
    TenantId,
)
from shared.schemas.money import CurrencyCode, Money

_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)

_MODULE_BY_SYMBOL = {
    "EVENT_REGISTRY": "infra.db.outbox",
    "serialize": "infra.db.outbox",
    "deserialize": "infra.db.outbox",
    "resolve_event_type": "infra.db.outbox",
}


def _load(symbol: str):
    """按模块字符串导入符号；缺失转行为失败（RED 阶段 outbox 未建）。"""
    try:
        return getattr(importlib.import_module(_MODULE_BY_SYMBOL[symbol]), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：infra.db.outbox.{symbol} 尚未创建（{exc}）")


def test_event_registry_is_explicit_whitelist() -> None:
    """EVENT_REGISTRY 只含已批准的 opportunities 与 sending identity 事件。"""
    EVENT_REGISTRY = _load("EVENT_REGISTRY")
    assert set(EVENT_REGISTRY) == {
        "OpportunityQualified",
        "OpportunityLost",
        "OpportunityWon",
        "HandoffRequested",
        "HandoffAccepted",
        "HandoffQueueBacklogged",
        "SendingIdentityActivated",
        "SendingIdentityThrottled",
        "SendingIdentitySuspended",
        "ReputationThresholdBreached",
    }
    assert EVENT_REGISTRY["OpportunityWon"] is OpportunityWon


def test_registry_events_roundtrip() -> None:
    """全部白名单事件 serialize→deserialize 可逆。"""
    serialize = _load("serialize")
    deserialize = _load("deserialize")
    events = [
        OpportunityQualified(
            tenant_id=TenantId("t1"), occurred_at=_NOW, run_id=RunId("r1"),
            opportunity_id=OpportunityId("opp1"), rank_bucket="high",
        ),
        OpportunityLost(
            tenant_id=TenantId("t1"), occurred_at=_NOW, run_id=RunId("r1"),
            opportunity_id=OpportunityId("opp1"), loss_reason="price_too_high",
            died_at_state="quoted",
        ),
        OpportunityWon(
            tenant_id=TenantId("t1"), occurred_at=_NOW, run_id=RunId("r1"),
            opportunity_id=OpportunityId("opp1"), closed_by=EmployeeId("e1"),
        ),
        HandoffRequested(
            tenant_id=TenantId("t1"), occurred_at=_NOW, run_id=RunId("r1"),
            handoff_id=HandoffId("h1"), opportunity_id=OpportunityId("opp1"),
            assigned_to=EmployeeId("e1"), trigger="quote_requested",
        ),
        HandoffAccepted(
            tenant_id=TenantId("t1"), occurred_at=_NOW, run_id=None,
            handoff_id=HandoffId("h1"), accepted_by=EmployeeId("e1"),
        ),
        HandoffQueueBacklogged(
            tenant_id=TenantId("t1"), occurred_at=_NOW, run_id=RunId("r1"),
            queue_depth=5, oldest_wait_seconds=120,
        ),
    ]
    for evt in events:
        payload = serialize(evt)
        restored = deserialize(type(evt), payload)
        assert restored == evt


@dataclass(frozen=True)
class _NestedPayload:
    """嵌套 dataclass 载荷（serializer 类型广度用）。"""

    note: str
    quantity: int


@dataclass(frozen=True, kw_only=True)
class _RichEvent(DomainEvent):
    """类型广度事件：Money / Decimal / 嵌套 dataclass / list / dict / Optional / Enum / NewType。"""

    money: Money
    nested: _NestedPayload
    items: list[str]
    mapping: dict[str, int]
    raw_amount: Decimal
    maybe: str | None
    tier: ConfidenceTier | None
    owner: EmployeeId


def test_serializer_type_breadth_roundtrip() -> None:
    """Money/Decimal/嵌套 dataclass/list/dict/None/Enum/NewType 均可逆。"""
    serialize = _load("serialize")
    deserialize = _load("deserialize")
    evt = _RichEvent(
        tenant_id=TenantId("t1"),
        occurred_at=_NOW,
        run_id=None,
        money=Money(Decimal("1234.56"), CurrencyCode("USD")),
        nested=_NestedPayload(note="扩建第二座工厂", quantity=3),
        items=["a", "b"],
        mapping={"x": 1},
        raw_amount=Decimal("0.01"),
        maybe=None,
        tier=ConfidenceTier.HIGH,
        owner=EmployeeId("e1"),
    )
    payload = serialize(evt)
    restored = deserialize(_RichEvent, payload)
    assert restored == evt


def test_serialized_payload_is_json_serializable() -> None:
    """序列化输出可直接 json.dumps（JSON 协议，禁止 pickle）。"""
    serialize = _load("serialize")
    evt = _RichEvent(
        tenant_id=TenantId("t1"),
        occurred_at=_NOW,
        run_id=RunId("r1"),
        money=Money(Decimal("99.50"), CurrencyCode("CNY")),
        nested=_NestedPayload(note="x", quantity=1),
        items=[],
        mapping={"a": 2},
        raw_amount=Decimal("0.00"),
        maybe="ok",
        tier=ConfidenceTier.LOW,
        owner=EmployeeId("e2"),
    )
    payload = serialize(evt)
    encoded = json.dumps(payload)
    assert isinstance(encoded, str)


def test_unknown_event_type_rejected() -> None:
    """未注册事件类型：resolve_event_type 抛 ValidationError（发布/反序列化入口）。"""
    resolve_event_type = _load("resolve_event_type")
    with pytest.raises(ValidationError):
        resolve_event_type("NeedValidated")  # catalog 有类但不在白名单
    with pytest.raises(ValidationError):
        resolve_event_type("TotallyUnknownEvent")
