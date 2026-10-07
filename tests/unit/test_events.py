"""shared.events.bus 的 EventEnvelope / EventHandler 契约单测（行为断言）。

覆盖（拦截的变异）：
1. EventEnvelope 携带事件载荷与四个元数据字段，event 保持同一对象
2. frozen：构造后赋值抛 FrozenInstanceError（用 setattr 避免类型忽略）
3. attempt 必须真 int 且 >=1（拒绝 0/-1/bool/float/str）；event_id/trace_id 空白拒绝，
   非空含边缘空格不改写
4. EventHandler @runtime_checkable：实现 async handle 的类被 isinstance 识别，
   普通 object 不是
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass
from datetime import UTC, datetime
from typing import cast

import pytest

from shared.errors import ValidationError
from shared.events.bus import EventEnvelope, EventHandler
from shared.events.catalog import DomainEvent
from shared.schemas.identifiers import RunId, TenantId

_NOW = datetime(2026, 8, 8, tzinfo=UTC)


@dataclass(frozen=True)
class _TestEvent(DomainEvent):
    """测试内最小领域事件（不改 catalog）。"""

    payload: str = ""


_TEST_EVENT = _TestEvent(
    tenant_id=TenantId("t1"),
    occurred_at=_NOW,
    run_id=RunId("r1"),
    payload="p",
)


def _envelope(
    *,
    event: DomainEvent = _TEST_EVENT,
    event_id: str = "evt1",
    attempt: int = 1,
    published_at: datetime = _NOW,
    trace_id: str = "tr1",
) -> EventEnvelope:
    return EventEnvelope(
        event=event,
        event_id=event_id,
        attempt=attempt,
        published_at=published_at,
        trace_id=trace_id,
    )


# --- EventEnvelope：事件载荷 + 元数据 + frozen ---------------------------------


def test_envelope_carries_event_and_metadata() -> None:
    envelope = _envelope()
    assert envelope.event is _TEST_EVENT
    assert envelope.event_id == "evt1"
    assert envelope.attempt == 1
    assert envelope.published_at == _NOW
    assert envelope.trace_id == "tr1"


def test_envelope_is_frozen() -> None:
    envelope = _envelope()
    field_name = "event_id"  # 变量名避开 B010（常量属性名），同时不触发 mypy frozen 赋值错误
    with pytest.raises(FrozenInstanceError):
        setattr(envelope, field_name, "changed")


# --- attempt：必须真 int 且 >= 1 -------------------------------------------------


@pytest.mark.parametrize(
    "bad_attempt",
    [0, -1, cast(int, True), cast(int, 1.0), cast(int, "1")],
)
def test_envelope_rejects_bad_attempt(bad_attempt: int) -> None:
    with pytest.raises(ValidationError):
        _envelope(attempt=bad_attempt)


# --- event_id / trace_id：空白拒绝，非空含边缘空格不改写 --------------------------


@pytest.mark.parametrize("blank", ["", "   "])
def test_envelope_rejects_blank_event_id(blank: str) -> None:
    with pytest.raises(ValidationError):
        _envelope(event_id=blank)


@pytest.mark.parametrize("blank", ["", "   "])
def test_envelope_rejects_blank_trace_id(blank: str) -> None:
    with pytest.raises(ValidationError):
        _envelope(trace_id=blank)


def test_envelope_accepts_nonblank_with_edge_spaces() -> None:
    envelope = _envelope(event_id="  evt1  ", trace_id="  tr1  ")
    assert envelope.event_id == "  evt1  "  # 含边缘空格不改写
    assert envelope.trace_id == "  tr1  "


# --- EventHandler：@runtime_checkable 运行时检查 ----------------------------------


class _Handler:
    async def handle(self, event: DomainEvent) -> None:
        pass


def test_event_handler_runtime_checkable() -> None:
    assert isinstance(_Handler(), EventHandler)
    assert not isinstance(object(), EventHandler)
