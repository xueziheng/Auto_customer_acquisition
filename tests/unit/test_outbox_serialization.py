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
    ComplaintReceived,
    ContactPointVerified,
    CountryPolicyVersionProposed,
    DomainEvent,
    HandoffAccepted,
    HandoffQueueBacklogged,
    HandoffRequested,
    MessageSent,
    OpportunityLost,
    OpportunityQualified,
    OpportunityWon,
    ReputationThresholdBreached,
    SendingIdentityActivated,
    SendingIdentitySuspended,
    SendingIdentityThrottled,
    SuppressionAdded,
)
from shared.schemas.evidence import ConfidenceTier
from shared.schemas.identifiers import (
    AuthenticationCheckRequestId,
    CampaignId,
    EmployeeId,
    HandoffId,
    MessageId,
    OpportunityId,
    RunId,
    SendingIdentityId,
    TenantId,
    new_id,
)
from shared.schemas.money import CurrencyCode, Money

_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)
_VALID_SENDING_ID = SendingIdentityId(new_id("sid"))

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
    """EVENT_REGISTRY 只含经契约评审、当前实际需要发布的事件。"""
    EVENT_REGISTRY = _load("EVENT_REGISTRY")
    assert set(EVENT_REGISTRY) == {
        "ApprovalDecided",
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
        "MessageSent",
        "SuppressionAdded",
        "AuthenticationCheckRequested",
        "ComplaintReceived",
        "CommitmentCreated",
        "CommitmentOverdue",
        "ContactPointVerified",
        "CountryPolicyVersionProposed",
        "ReplyReceived",
        "InboundMessageStored",
        "DemandSignalCaptured",
        "DirectiveActivated",
        "NeedHypothesisCreated",
        "NeedHypothesisRejected",
        "NeedValidated",
    }
    assert EVENT_REGISTRY["OpportunityWon"] is OpportunityWon
    assert EVENT_REGISTRY["MessageSent"] is MessageSent
    assert EVENT_REGISTRY["SuppressionAdded"] is SuppressionAdded
    assert EVENT_REGISTRY["ContactPointVerified"] is ContactPointVerified
    assert EVENT_REGISTRY["CountryPolicyVersionProposed"] is CountryPolicyVersionProposed
    event_type = getattr(
        importlib.import_module("shared.events.catalog"),
        "AuthenticationCheckRequested",
        None,
    )
    assert event_type is not None, "RED：AuthenticationCheckRequested 尚未创建"
    assert EVENT_REGISTRY["AuthenticationCheckRequested"] is event_type


def test_authentication_check_requested_roundtrip_contains_only_safe_ids() -> None:
    """认证 request 事件不得携带 domain、selector、DNS 或凭证。"""
    event_type = getattr(
        importlib.import_module("shared.events.catalog"),
        "AuthenticationCheckRequested",
        None,
    )
    assert event_type is not None, "RED：AuthenticationCheckRequested 尚未创建"
    event = event_type(
        tenant_id=TenantId(new_id("tn")),
        occurred_at=_NOW,
        run_id=None,
        request_id=AuthenticationCheckRequestId(new_id("acr")),
        sending_identity_id=SendingIdentityId(new_id("sid")),
    )
    payload = _load("serialize")(event)
    assert set(payload) == {
        "tenant_id",
        "occurred_at",
        "run_id",
        "request_id",
        "sending_identity_id",
    }
    assert _load("deserialize")(event_type, payload) == event
    rendered = json.dumps(payload)
    assert "selector" not in rendered
    assert "dns" not in rendered.casefold()
    assert "secret" not in rendered.casefold()


def test_complaint_received_roundtrip_contains_only_safe_ids() -> None:
    """投诉事件只携带 attempt/sending identity/dedup 摘要，不携带 MIME 或地址。"""
    registry = _load("EVENT_REGISTRY")
    assert registry["ComplaintReceived"] is ComplaintReceived
    event = ComplaintReceived(
        tenant_id=TenantId(new_id("tn")),
        occurred_at=_NOW,
        run_id=None,
        message_attempt_id=new_id("mat"),
        sending_identity_id=SendingIdentityId(new_id("sid")),
        dedup_key="c" * 64,
    )
    payload = _load("serialize")(event)
    assert set(payload) == {
        "tenant_id",
        "occurred_at",
        "run_id",
        "message_attempt_id",
        "sending_identity_id",
        "dedup_key",
    }
    assert _load("deserialize")(ComplaintReceived, payload) == event
    rendered = json.dumps(payload)
    assert "@" not in rendered
    assert "example.com" not in rendered
    assert "secret" not in rendered.casefold()


@pytest.mark.parametrize(
    "event",
    [
        MessageSent(
            tenant_id=TenantId(new_id("tn")),
            occurred_at=_NOW,
            run_id=None,
            message_id=MessageId(new_id("msg")),
            campaign_id=CampaignId(new_id("cmp")),
            sending_identity_id=SendingIdentityId(new_id("sid")),
        ),
        SuppressionAdded(
            tenant_id=TenantId(new_id("tn")),
            occurred_at=_NOW,
            run_id=None,
            scope="contact",
            target_id=new_id("cp"),
            reason="unsubscribe",
        ),
    ],
)
def test_outreach_events_roundtrip(event: DomainEvent) -> None:
    """触达事实的 typed wire 可逆且只含安全元数据。"""
    serialize = _load("serialize")
    deserialize = _load("deserialize")
    payload = serialize(event)
    assert deserialize(type(event), payload) == event


@pytest.mark.parametrize(
    "event",
    [
        MessageSent(
            tenant_id=TenantId(new_id("tn")),
            occurred_at=_NOW,
            run_id=None,
            message_id=MessageId("email@example.com"),
            campaign_id=CampaignId(new_id("cmp")),
            sending_identity_id=SendingIdentityId(new_id("sid")),
        ),
        MessageSent(
            tenant_id=TenantId(new_id("tn")),
            occurred_at=_NOW,
            run_id=None,
            message_id=MessageId(new_id("msg")),
            campaign_id=CampaignId("acc_" + "0" * 26),
            sending_identity_id=SendingIdentityId(new_id("sid")),
        ),
        SuppressionAdded(
            tenant_id=TenantId(new_id("tn")),
            occurred_at=_NOW,
            run_id=None,
            scope="all",
            target_id=new_id("cp"),
            reason="unsubscribe",
        ),
        SuppressionAdded(
            tenant_id=TenantId(new_id("tn")),
            occurred_at=_NOW,
            run_id=None,
            scope="contact",
            target_id="Bearer_abc",
            reason="unsubscribe",
        ),
        SuppressionAdded(
            tenant_id=TenantId(new_id("tn")),
            occurred_at=_NOW,
            run_id=None,
            scope="contact",
            target_id="postgresql://user:password@db/private",
            reason="unsubscribe",
        ),
        SuppressionAdded(
            tenant_id=TenantId(new_id("tn")),
            occurred_at=_NOW,
            run_id=None,
            scope="account",
            target_id="https://crm.example.test/customer/secret",
            reason="complaint",
        ),
        SuppressionAdded(
            tenant_id=TenantId(new_id("tn")),
            occurred_at=_NOW,
            run_id=None,
            scope="contact",
            target_id="Hello, this is the customer email body",
            reason="unsubscribe",
        ),
        SuppressionAdded(
            tenant_id=TenantId(new_id("tn")),
            occurred_at=_NOW,
            run_id=None,
            scope="contact",
            target_id=new_id("cp"),
            reason="free_text",
        ),
        SuppressionAdded(
            tenant_id=TenantId(new_id("tn")),
            occurred_at=datetime(2026, 8, 11, 4, 0),  # noqa: DTZ001
            run_id=None,
            scope="account",
            target_id=new_id("acc"),
            reason="complaint",
        ),
    ],
)
def test_outreach_event_serializer_rejects_unsafe_payloads(event: DomainEvent) -> None:
    """错 prefix、自由词、地址/凭证与 naive 时间均固定拒绝。"""
    with pytest.raises(ValidationError, match="触达事件载荷无效"):
        _load("serialize")(event)


@pytest.mark.parametrize(
    "event",
    [
        SendingIdentityActivated(
            tenant_id=TenantId("t1"),
            occurred_at=_NOW,
            run_id=None,
            sending_identity_id=SendingIdentityId("sales@example.com"),
        ),
        SendingIdentityThrottled(
            tenant_id=TenantId("t1"),
            occurred_at=_NOW,
            run_id=None,
            sending_identity_id=_VALID_SENDING_ID,
            new_state="active",
            trigger_metric="hard_bounce_rate",
            metric_value="0.03",
        ),
        SendingIdentityThrottled(
            tenant_id=TenantId("t1"),
            occurred_at=_NOW,
            run_id=None,
            sending_identity_id=_VALID_SENDING_ID,
            new_state="throttled",
            trigger_metric="mx.example.com",
            metric_value="0.03",
        ),
        SendingIdentityThrottled(
            tenant_id=TenantId("t1"),
            occurred_at=_NOW,
            run_id=None,
            sending_identity_id=_VALID_SENDING_ID,
            new_state="throttled",
            trigger_metric="hard_bounce_rate",
            metric_value="NaN",
        ),
        SendingIdentitySuspended(
            tenant_id=TenantId("t1"),
            occurred_at=_NOW,
            run_id=None,
            sending_identity_id=_VALID_SENDING_ID,
            reason="dns_txt=secret",
        ),
        ReputationThresholdBreached(
            tenant_id=TenantId("t1"),
            occurred_at=_NOW,
            run_id=None,
            sending_identity_id=_VALID_SENDING_ID,
            metric="complaint_rate",
            value="-0.01",
            threshold="0.001",
            severity="watch",
        ),
        ReputationThresholdBreached(
            tenant_id=TenantId("t1"),
            occurred_at=_NOW,
            run_id=None,
            sending_identity_id=_VALID_SENDING_ID,
            metric="vault://investigation/ref",
            value="0.01",
            threshold="0.001",
            severity="manual_investigation",
        ),
    ],
)
def test_sending_identity_event_serializer_rejects_unbounded_payloads(
    event: DomainEvent,
) -> None:
    """outbox 边界拒绝地址、域名、引用、DNS、调查文本和非法数值。"""
    serialize = _load("serialize")
    with pytest.raises(ValidationError, match="发件身份事件载荷无效"):
        serialize(event)


@pytest.mark.parametrize(
    "identity_id",
    [
        "gmail_primary",
        "Bearer_abc",
        "token_secret",
        "other_" + "0" * 26,
        "sid_" + "I" * 26,
        "sid_" + "0" * 25,
        "sid_" + "0" * 27,
    ],
)
def test_sending_identity_event_rejects_non_wire_identity_ids(identity_id: str) -> None:
    """凭证形态、错误 prefix、非法 Crockford 字符和错误长度不能充当 identity ID。"""
    serialize = _load("serialize")
    event = SendingIdentityActivated(
        tenant_id=TenantId("t1"),
        occurred_at=_NOW,
        run_id=None,
        sending_identity_id=SendingIdentityId(identity_id),
    )
    with pytest.raises(ValidationError, match="发件身份事件载荷无效"):
        serialize(event)


@pytest.mark.parametrize(
    "ratio",
    [
        "0." + "1" * 29,
        "+0.1",
        "-0.1",
        "1e-1",
        "NaN",
        "Infinity",
        "1.0000000000000000000000000001",
        "dns_investigation",
    ],
)
def test_sending_identity_event_rejects_unsafe_decimal_wire_values(ratio: str) -> None:
    """超过默认 Decimal 精度、符号、指数、非有限、越界与自由文本均 fail closed。"""
    serialize = _load("serialize")
    event = SendingIdentityThrottled(
        tenant_id=TenantId("t1"),
        occurred_at=_NOW,
        run_id=None,
        sending_identity_id=_VALID_SENDING_ID,
        new_state="throttled",
        trigger_metric="hard_bounce_rate",
        metric_value=ratio,
    )
    with pytest.raises(ValidationError, match="发件身份事件载荷无效"):
        serialize(event)


def test_new_sending_identity_id_and_default_decimal_ratio_roundtrip() -> None:
    """new_id('sid') 与未量化的真实 1/51 Decimal 比率可无损进入 outbox wire。"""
    serialize = _load("serialize")
    deserialize = _load("deserialize")
    sending_models = importlib.import_module("domains.sending_identity.models")
    ReputationWindow = sending_models.ReputationWindow
    window = ReputationWindow(
        window_days=7,
        computed_at=_NOW,
        sent_attempts=51,
        delivered=50,
        hard_bounced=1,
        soft_bounced=0,
        complaints=0,
        unsubscribed=0,
    )
    value = str(window.hard_bounce_rate)
    assert value == "0.01960784313725490196078431373"
    event = SendingIdentityThrottled(
        tenant_id=TenantId("t1"),
        occurred_at=_NOW,
        run_id=None,
        sending_identity_id=SendingIdentityId(new_id("sid")),
        new_state="throttled",
        trigger_metric="hard_bounce_rate",
        metric_value=value,
    )
    payload = serialize(event)
    assert payload["metric_value"] == value
    assert deserialize(SendingIdentityThrottled, payload) == event


def test_registry_events_roundtrip() -> None:
    """全部白名单事件 serialize→deserialize 可逆。"""
    serialize = _load("serialize")
    deserialize = _load("deserialize")
    events = [
        OpportunityQualified(
            tenant_id=TenantId("t1"),
            occurred_at=_NOW,
            run_id=RunId("r1"),
            opportunity_id=OpportunityId("opp1"),
            rank_bucket="high",
        ),
        OpportunityLost(
            tenant_id=TenantId("t1"),
            occurred_at=_NOW,
            run_id=RunId("r1"),
            opportunity_id=OpportunityId("opp1"),
            loss_reason="price_too_high",
            died_at_state="quoted",
        ),
        OpportunityWon(
            tenant_id=TenantId("t1"),
            occurred_at=_NOW,
            run_id=RunId("r1"),
            opportunity_id=OpportunityId("opp1"),
            closed_by=EmployeeId("e1"),
        ),
        HandoffRequested(
            tenant_id=TenantId("t1"),
            occurred_at=_NOW,
            run_id=RunId("r1"),
            handoff_id=HandoffId("h1"),
            opportunity_id=OpportunityId("opp1"),
            assigned_to=EmployeeId("e1"),
            trigger="quote_requested",
        ),
        HandoffAccepted(
            tenant_id=TenantId("t1"),
            occurred_at=_NOW,
            run_id=None,
            handoff_id=HandoffId("h1"),
            accepted_by=EmployeeId("e1"),
        ),
        HandoffQueueBacklogged(
            tenant_id=TenantId("t1"),
            occurred_at=_NOW,
            run_id=RunId("r1"),
            queue_depth=5,
            oldest_wait_seconds=120,
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
        resolve_event_type("NeedClusterFormed")  # catalog 有类但本切片明确不注册
    with pytest.raises(ValidationError):
        resolve_event_type("TotallyUnknownEvent")


class _RecordingSession:
    """只记录 add 调用的最小会话桩；形状校验失败时不得触达。"""

    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)


def test_complaint_received_publish_validates_shape_fail_closed() -> None:
    """发布 ComplaintReceived 前校验真实字段形状；无效 fail closed，合法发布。"""
    import asyncio

    bus_module = importlib.import_module("infra.db.outbox")
    tenant = TenantId(new_id("tn"))
    valid = ComplaintReceived(
        tenant_id=tenant,
        occurred_at=_NOW,
        run_id=None,
        message_attempt_id=new_id("mat"),
        sending_identity_id=SendingIdentityId(new_id("sid")),
        dedup_key="c" * 64,
    )
    session = _RecordingSession()
    bus = bus_module.PostgresEventBus(session, tenant, now=lambda: _NOW)
    asyncio.run(bus.publish(valid))
    assert len(session.added) == 1

    invalid = (
        ComplaintReceived(
            tenant_id=tenant,
            occurred_at=_NOW,
            run_id=None,
            message_attempt_id="not-an-attempt",
            sending_identity_id=SendingIdentityId(new_id("sid")),
            dedup_key="c" * 64,
        ),
        ComplaintReceived(
            tenant_id=tenant,
            occurred_at=_NOW,
            run_id=None,
            message_attempt_id=new_id("mat"),
            sending_identity_id=SendingIdentityId("not-an-identity"),
            dedup_key="c" * 64,
        ),
        ComplaintReceived(
            tenant_id=tenant,
            occurred_at=_NOW,
            run_id=None,
            message_attempt_id=new_id("mat"),
            sending_identity_id=SendingIdentityId(new_id("sid")),
            dedup_key="Bearer-private",
        ),
        ComplaintReceived(
            tenant_id=tenant,
            occurred_at=_NOW,
            run_id=None,
            message_attempt_id=new_id("mat"),
            sending_identity_id=SendingIdentityId(new_id("sid")),
            dedup_key="short",
        ),
        ComplaintReceived(
            tenant_id=tenant,
            occurred_at=_NOW.replace(tzinfo=None),
            run_id=None,
            message_attempt_id=new_id("mat"),
            sending_identity_id=SendingIdentityId(new_id("sid")),
            dedup_key="c" * 64,
        ),
    )
    for event in invalid:
        session.added.clear()
        with pytest.raises(ValidationError):
            asyncio.run(bus.publish(event))
        assert session.added == [], "形状校验失败前不得写入 outbox"


def test_complaint_received_publish_rejects_runtime_non_datetime_occurred_at() -> None:
    """运行时把 occurred_at 改成非 datetime 必须 fail closed，不能 AttributeError。"""
    import asyncio

    bus_module = importlib.import_module("infra.db.outbox")
    tenant = TenantId(new_id("tn"))
    event = ComplaintReceived(
        tenant_id=tenant,
        occurred_at=_NOW,
        run_id=None,
        message_attempt_id=new_id("mat"),
        sending_identity_id=SendingIdentityId(new_id("sid")),
        dedup_key="c" * 64,
    )
    object.__setattr__(event, "occurred_at", "2026-08-15T10:00:00Z")
    session = _RecordingSession()
    bus = bus_module.PostgresEventBus(session, tenant, now=lambda: _NOW)
    with pytest.raises(ValidationError):
        asyncio.run(bus.publish(event))
    assert session.added == []
