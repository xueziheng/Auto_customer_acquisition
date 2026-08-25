"""outbox 事件写入（P1：发布写入与业务同一事务）。

- ``EVENT_REGISTRY``：显式发布白名单——只有经契约评审、当前实际需要发布的
  事件类型才允许写入 outbox。它**不是**由各域 PUBLISHES 声明自动推导的集合：
  各域 ``events.py`` 的 PUBLISHES 是文档性声明，白名单以这里手工维护为准；
  新增事件类型必须先经契约评审再加白名单，禁止反射扫描 catalog 自动放行。
- 事件序列化用 JSON：NewType→str、datetime→ISO、Enum→value、Money→{amount,currency}
  （Decimal 用 str 保精度）、嵌套 dataclass 递归。**禁止 pickle / 动态导入 /
  不可信类型构造**——不可序列化类型直接抛 ``ValidationError``。
- ``PostgresEventBus``：白名单/租户校验后 INSERT outbox_events；``subscribe`` 仅内存
  注册；只写不投递（投递轮询延后）。
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import UnionType
from typing import Union, cast, get_args, get_origin, get_type_hints

from sqlalchemy.ext.asyncio import AsyncSession

from domains.sending_identity.models import (
    IdentityState,
    ReputationMetric,
    ReputationSeverity,
    SuspensionCategory,
)
from infra.db.tables import OutboxEventRow
from shared.errors import ValidationError
from shared.events.bus import E_contra, EventEnvelope, EventHandler
from shared.events.catalog import (
    ApprovalDecided,
    AuthenticationCheckRequested,
    CommitmentCreated,
    CommitmentOverdue,
    ComplaintReceived,
    ContactPointVerified,
    CountryPolicyVersionProposed,
    DemandSignalCaptured,
    DirectiveActivated,
    DomainEvent,
    HandoffAccepted,
    HandoffQueueBacklogged,
    HandoffRequested,
    InboundMessageStored,
    MessageSent,
    NeedHypothesisCreated,
    NeedHypothesisRejected,
    NeedValidated,
    OpportunityLost,
    OpportunityQualified,
    OpportunityWon,
    ReplyReceived,
    ReputationThresholdBreached,
    SendingIdentityActivated,
    SendingIdentitySuspended,
    SendingIdentityThrottled,
    SuppressionAdded,
)
from shared.schemas.identifiers import TenantId, new_id
from shared.schemas.money import CurrencyCode, Money

EVENT_REGISTRY: dict[str, type[DomainEvent]] = {
    "ApprovalDecided": ApprovalDecided,
    "AuthenticationCheckRequested": AuthenticationCheckRequested,
    "OpportunityQualified": OpportunityQualified,
    "OpportunityLost": OpportunityLost,
    "OpportunityWon": OpportunityWon,
    "HandoffRequested": HandoffRequested,
    "HandoffAccepted": HandoffAccepted,
    "HandoffQueueBacklogged": HandoffQueueBacklogged,
    "SendingIdentityActivated": SendingIdentityActivated,
    "SendingIdentityThrottled": SendingIdentityThrottled,
    "SendingIdentitySuspended": SendingIdentitySuspended,
    "ReputationThresholdBreached": ReputationThresholdBreached,
    "MessageSent": MessageSent,
    # conversations 入站消息落库即发布（切片 6 producer）：metadata-only，
    # 下一片 scheduler 订阅后按 outbound_message_id 解析并启动 reply run
    "InboundMessageStored": InboundMessageStored,
    "SuppressionAdded": SuppressionAdded,
    "ComplaintReceived": ComplaintReceived,
    "CommitmentCreated": CommitmentCreated,
    "CommitmentOverdue": CommitmentOverdue,
    # prospecting 验证状态首次进入 VERIFIED 的 metadata-only 事件。
    "ContactPointVerified": ContactPointVerified,
    "CountryPolicyVersionProposed": CountryPolicyVersionProposed,
    # demand 信号捕获即发布（切片 7 producer）：metadata-only（signal_id/
    # entity_name/signal_type）；完整 URL 仅在 tenant-bound demand provenance。
    "DemandSignalCaptured": DemandSignalCaptured,
    "DirectiveActivated": DirectiveActivated,
    "NeedHypothesisCreated": NeedHypothesisCreated,
    "NeedHypothesisRejected": NeedHypothesisRejected,
    "NeedValidated": NeedValidated,
    # scheduler 已订阅 ReplyReceived（停序列 + 唤醒 wait_for_reply）：共享
    # outbox 入口对回复管道（切片 6 producer）开放，接线可端到端验证
    "ReplyReceived": ReplyReceived,
}
"""显式发布白名单（手工维护，见模块 docstring）：新事件必须先经契约评审。"""

_LOWER_HEX_64_RE = re.compile(r"[0-9a-f]{64}")
_ATTEMPT_ID_RE = re.compile(r"mat_[0-7][0-9A-HJKMNP-TV-Z]{25}")
_IDENTITY_ID_RE = re.compile(r"sid_[0-7][0-9A-HJKMNP-TV-Z]{25}")


def _is_utc_aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0)


def _validate_complaint_received_shape(event: ComplaintReceived) -> None:
    """发布前校验 ComplaintReceived 真实字段的安全形状；无效 fail closed。

    投诉是最高风险反馈：attempt/身份/dedup 任一形状异常都不允许写入 outbox
    （否则下游反序列化或消费方会拿到未定型数据）。规则与既有安全 ID 类型
    （``mat_``/``sid_`` + ULID 字符集、64-lower-hex digest）保持一致。
    """
    if not isinstance(event.occurred_at, datetime) or not _is_utc_aware(
        event.occurred_at
    ):
        raise ValidationError("ComplaintReceived occurred_at 必须为 UTC datetime")
    if (
        not isinstance(event.message_attempt_id, str)
        or _ATTEMPT_ID_RE.fullmatch(event.message_attempt_id) is None
    ):
        raise ValidationError("ComplaintReceived message_attempt_id 无效")
    if (
        not isinstance(event.sending_identity_id, str)
        or _IDENTITY_ID_RE.fullmatch(event.sending_identity_id) is None
    ):
        raise ValidationError("ComplaintReceived sending_identity_id 无效")
    if (
        not isinstance(event.dedup_key, str)
        or _LOWER_HEX_64_RE.fullmatch(event.dedup_key) is None
    ):
        raise ValidationError("ComplaintReceived dedup_key 无效")


def resolve_event_type(name: str) -> type[DomainEvent]:
    """按名字查白名单；未注册抛 ``ValidationError``（发布/反序列化共同入口）。"""
    cls = EVENT_REGISTRY.get(name)
    if cls is None:
        raise ValidationError(f"事件类型 {name} 不在 EVENT_REGISTRY 白名单")
    return cls


def _to_jsonable(value: object) -> object:
    """事件字段 → JSON 可序列化值（按运行时类型；NewType 即 str）。"""
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Money):
        return {"amount": str(value.amount), "currency": value.currency}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _to_jsonable(getattr(value, f.name))
            for f in dataclasses.fields(value)
        }
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    raise ValidationError(f"事件字段含不可序列化类型：{type(value).__name__}")


_SAFE_SENDING_ID = re.compile(r"sid_[0-7][0-9A-HJKMNP-TV-Z]{25}\Z")
_SAFE_RATIO = re.compile(r"(?:0|1)(?:\.[0-9]+)?\Z")


def _invalid_sending_event() -> ValidationError:
    return ValidationError("发件身份事件载荷无效")


def _validate_ratio(value: str) -> None:
    if not isinstance(value, str) or _SAFE_RATIO.fullmatch(value) is None:
        raise _invalid_sending_event()
    decimal_value = Decimal(value)
    if (
        not decimal_value.is_finite()
        or decimal_value < 0
        or decimal_value > 1
        or len(decimal_value.as_tuple().digits) > 28
    ):
        raise _invalid_sending_event()


def _validate_sending_identity_event(event: DomainEvent) -> None:
    if isinstance(event, AuthenticationCheckRequested):
        if (
            not isinstance(event.request_id, str)
            or re.fullmatch(r"acr_[0-7][0-9A-HJKMNP-TV-Z]{25}", event.request_id)
            is None
            or not isinstance(event.sending_identity_id, str)
            or _SAFE_SENDING_ID.fullmatch(event.sending_identity_id) is None
        ):
            raise _invalid_sending_event()
        return
    if not isinstance(
        event,
        (
            SendingIdentityActivated,
            SendingIdentityThrottled,
            SendingIdentitySuspended,
            ReputationThresholdBreached,
        ),
    ):
        return
    if (
        not isinstance(event.sending_identity_id, str)
        or _SAFE_SENDING_ID.fullmatch(event.sending_identity_id) is None
    ):
        raise _invalid_sending_event()
    try:
        if isinstance(event, SendingIdentityThrottled):
            if event.new_state != IdentityState.THROTTLED.value:
                raise _invalid_sending_event()
            ReputationMetric(event.trigger_metric)
            _validate_ratio(event.metric_value)
        elif isinstance(event, SendingIdentitySuspended):
            SuspensionCategory(event.reason)
        elif isinstance(event, ReputationThresholdBreached):
            ReputationMetric(event.metric)
            ReputationSeverity(event.severity)
            _validate_ratio(event.value)
            _validate_ratio(event.threshold)
    except ValueError:
        raise _invalid_sending_event() from None


_SAFE_OUTREACH_IDS = {
    "tenant": re.compile(r"tn_[0-7][0-9A-HJKMNP-TV-Z]{25}\Z"),
    "message": re.compile(r"msg_[0-7][0-9A-HJKMNP-TV-Z]{25}\Z"),
    "campaign": re.compile(r"cmp_[0-7][0-9A-HJKMNP-TV-Z]{25}\Z"),
    "identity": _SAFE_SENDING_ID,
    "contact": re.compile(r"cp_[0-7][0-9A-HJKMNP-TV-Z]{25}\Z"),
    "account": re.compile(r"acc_[0-7][0-9A-HJKMNP-TV-Z]{25}\Z"),
}
_OUTREACH_SUPPRESSION_REASONS = frozenset(
    {
        "unsubscribe",
        "complaint",
        "hard_bounce",
        "manual_block",
        "competitor",
        "existing_customer_conflict",
    }
)


def _invalid_outreach_event() -> ValidationError:
    return ValidationError("触达事件载荷无效")


def _matches_wire(value: object, kind: str) -> bool:
    return (
        isinstance(value, str) and _SAFE_OUTREACH_IDS[kind].fullmatch(value) is not None
    )


def _validate_outreach_event(event: DomainEvent) -> None:
    if not isinstance(event, (MessageSent, SuppressionAdded)):
        return
    if (
        not _matches_wire(event.tenant_id, "tenant")
        or not isinstance(event.occurred_at, datetime)
        or event.occurred_at.tzinfo is None
        or event.occurred_at.utcoffset() != UTC.utcoffset(event.occurred_at)
    ):
        raise _invalid_outreach_event()
    if isinstance(event, MessageSent):
        if (
            not _matches_wire(event.message_id, "message")
            or not _matches_wire(event.campaign_id, "campaign")
            or not _matches_wire(event.sending_identity_id, "identity")
        ):
            raise _invalid_outreach_event()
        return
    target_kind = {"contact": "contact", "account": "account"}.get(event.scope)
    if (
        target_kind is None
        or not _matches_wire(event.target_id, target_kind)
        or event.reason not in _OUTREACH_SUPPRESSION_REASONS
    ):
        raise _invalid_outreach_event()


def serialize(event: DomainEvent) -> dict[str, object]:
    """事件 → JSON 可序列化 dict（Round-trip 的序列化半边）。"""
    _validate_sending_identity_event(event)
    _validate_outreach_event(event)
    return {
        f.name: _to_jsonable(getattr(event, f.name)) for f in dataclasses.fields(event)
    }


def _from_jsonable(value: object, expected: object) -> object:
    """按目标类型把 JSON 值还原为事件字段类型（类型安全，不接受任意类型构造）。"""
    origin = get_origin(expected)

    if origin is Union or origin is UnionType:
        args = get_args(expected)
        if value is None and type(None) in args:
            return None
        for arg in args:
            if arg is not type(None):
                return _from_jsonable(value, arg)
        return None

    if value is None:
        return None

    if origin is list:
        (elem_type,) = get_args(expected)
        return [_from_jsonable(item, elem_type) for item in cast(list[object], value)]
    if origin is dict:
        _, val_type = get_args(expected)
        return {
            str(key): _from_jsonable(item, val_type)
            for key, item in cast(dict[str, object], value).items()
        }

    if hasattr(expected, "__supertype__"):
        # NewType：调用 NewType 构造器还原强类型（runtime 仍是 str，保型标注）。
        return cast(Callable[[object], object], expected)(value)
    if expected is datetime:
        return datetime.fromisoformat(cast(str, value))
    if expected is Decimal:
        return Decimal(cast(str, value))
    if expected is Money:
        raw = cast(dict[str, object], value)
        return Money(
            Decimal(cast(str, raw["amount"])),
            CurrencyCode(cast(str, raw["currency"])),
        )
    if isinstance(expected, type) and issubclass(expected, Enum):
        return cast(Callable[[str], object], expected)(cast(str, value))
    if isinstance(expected, type) and dataclasses.is_dataclass(expected):
        hints = get_type_hints(expected)
        raw = cast(dict[str, object], value)
        kwargs = {
            f.name: _from_jsonable(raw[f.name], hints[f.name])
            for f in dataclasses.fields(cast(type, expected))
        }
        return cast(Callable[..., object], expected)(**kwargs)
    return value


def deserialize(
    event_cls: type[DomainEvent], payload: dict[str, object]
) -> DomainEvent:
    """Round-trip 的反序列化半边：按目标事件类还原字段。"""
    hints = get_type_hints(event_cls)
    kwargs = {
        f.name: _from_jsonable(payload[f.name], hints[f.name])
        for f in dataclasses.fields(event_cls)
    }
    return cast(DomainEvent, cast(Callable[..., object], event_cls)(**kwargs))


class PostgresEventBus:
    """outbox 事件总线（P1 发布写入半边；投递轮询延后）。

    与业务写入共享同一 ``AsyncSession``，保证「业务改了事件也在」的原子性
    （提交与否由调用方/UoW 决定）。只写 outbox，不投递——投递由后续
    ``scheduler_worker`` 轮询负责。``subscribe`` 仅内存注册，无外部副作用。
    """

    def __init__(
        self,
        session: AsyncSession,
        tenant_id: TenantId,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._now = now if now is not None else lambda: datetime.now(UTC)
        self._handlers: dict[str, list[EventHandler[DomainEvent]]] = {}

    async def publish(self, event: DomainEvent) -> None:
        """白名单校验 → 租户一致校验 → 形状校验 → 构造 EventEnvelope → INSERT。

        事件类型必须在 ``EVENT_REGISTRY``（类身份一致，防伪造）；事件租户必须与
        总线绑定租户一致（硬边界 8，避免业务 repo 与事件 tenant 不一致写入）。
        ``ComplaintReceived`` 额外做发布前形状校验（fail closed）。
        """
        name = type(event).__name__
        if EVENT_REGISTRY.get(name) is not type(event):
            raise ValidationError(f"事件类型 {name} 不在 EVENT_REGISTRY 白名单")
        if event.tenant_id != self._tenant_id:
            raise ValueError(
                f"事件租户 {event.tenant_id} 与总线绑定租户 {self._tenant_id} "
                "不一致：拒绝发布（硬边界 8）"
            )
        if name == "ComplaintReceived":
            _validate_complaint_received_shape(cast(ComplaintReceived, event))
        envelope = EventEnvelope(
            event=event,
            event_id=new_id("evt"),
            attempt=1,
            published_at=self._now(),
            trace_id=new_id("trc"),
        )
        self._session.add(
            OutboxEventRow(
                event_id=envelope.event_id,
                tenant_id=envelope.event.tenant_id,
                event_type=name,
                event_payload=serialize(envelope.event),
                attempt=envelope.attempt,
                published_at=envelope.published_at,
                trace_id=envelope.trace_id,
                run_id=envelope.event.run_id,
                occurred_at=envelope.event.occurred_at,
                status="pending",
            )
        )

    async def publish_many(self, events: list[DomainEvent]) -> None:
        for event in events:
            await self.publish(event)

    def subscribe(
        self, event_type: type[E_contra], handler: EventHandler[E_contra]
    ) -> None:
        """内存注册订阅（应用启动装配）；不执行任何外部副作用。"""
        self._handlers.setdefault(event_type.__name__, []).append(
            cast(EventHandler[DomainEvent], handler)
        )
