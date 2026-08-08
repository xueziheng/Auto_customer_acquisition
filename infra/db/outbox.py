"""outbox 事件写入（P1：发布写入与业务同一事务）。

- ``EVENT_REGISTRY``：显式白名单（本切片 opportunities 域实际发布的事件类），
  禁止反射扫描自动放行。
- 事件序列化用 JSON：NewType→str、datetime→ISO、Enum→value、Money→{amount,currency}
  （Decimal 用 str 保精度）、嵌套 dataclass 递归。**禁止 pickle / 动态导入 /
  不可信类型构造**——不可序列化类型直接抛 ``ValidationError``。
- ``PostgresEventBus``：白名单/租户校验后 INSERT outbox_events；``subscribe`` 仅内存
  注册；只写不投递（投递轮询延后）。
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from types import UnionType
from typing import Union, cast, get_args, get_origin, get_type_hints

from sqlalchemy.ext.asyncio import AsyncSession

from infra.db.tables import OutboxEventRow
from shared.errors import ValidationError
from shared.events.bus import E_contra, EventEnvelope, EventHandler
from shared.events.catalog import (
    DomainEvent,
    HandoffAccepted,
    HandoffQueueBacklogged,
    HandoffRequested,
    OpportunityLost,
    OpportunityQualified,
    OpportunityWon,
)
from shared.schemas.identifiers import TenantId, new_id
from shared.schemas.money import CurrencyCode, Money

EVENT_REGISTRY: dict[str, type[DomainEvent]] = {
    "OpportunityQualified": OpportunityQualified,
    "OpportunityLost": OpportunityLost,
    "OpportunityWon": OpportunityWon,
    "HandoffRequested": HandoffRequested,
    "HandoffAccepted": HandoffAccepted,
    "HandoffQueueBacklogged": HandoffQueueBacklogged,
}
"""显式白名单：与 ``domains/opportunities/events.py`` 的 PUBLISHES 一致。
不允许用反射扫描 catalog 自动放行——新事件必须先经契约评审再加白名单。"""


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


def serialize(event: DomainEvent) -> dict[str, object]:
    """事件 → JSON 可序列化 dict（Round-trip 的序列化半边）。"""
    return {f.name: _to_jsonable(getattr(event, f.name)) for f in dataclasses.fields(event)}


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


def deserialize(event_cls: type[DomainEvent], payload: dict[str, object]) -> DomainEvent:
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
        """白名单校验 → 租户一致校验 → 构造 EventEnvelope → INSERT outbox_events。

        事件类型必须在 ``EVENT_REGISTRY``（类身份一致，防伪造）；事件租户必须与
        总线绑定租户一致（硬边界 8，避免业务 repo 与事件 tenant 不一致写入）。
        """
        name = type(event).__name__
        if EVENT_REGISTRY.get(name) is not type(event):
            raise ValidationError(f"事件类型 {name} 不在 EVENT_REGISTRY 白名单")
        if event.tenant_id != self._tenant_id:
            raise ValueError(
                f"事件租户 {event.tenant_id} 与总线绑定租户 {self._tenant_id} "
                "不一致：拒绝发布（硬边界 8）"
            )
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
