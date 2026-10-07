"""S2-7 机会域模型行为单测（can_transition_to / mark_lost / wait_seconds）。

行为断言，不依赖实现细节：
- ``can_transition_to`` 全状态矩阵；CONTACTED→QUOTED 合法（现有产品完全匹配）；
  WON/LOST 无出边；非法跳转与自环均拒绝。
- ``mark_lost`` 记录转入 lost 前的 ``died_at_state`` 与 ``reason``；终态/重复终结
  抛 ``InvalidStateTransition``，历史不得改写。
- ``wait_seconds(now)`` 对 requested/accepted 两态用显式注入 now；边界为零；
  负等待与 naive/aware 时间混用按 ``shared.errors`` 抛 ``ValidationError``。

RED 前置：模型方法当前为 ``NotImplementedError``；经 importlib 路由域私有模型。
"""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

import pytest

from shared.errors import InvalidStateTransition, ValidationError
from shared.schemas.identifiers import (
    HandoffId,
    OpportunityId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
)

_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=UTC)

_MODULE_BY_SYMBOL = {
    "Opportunity": "domains.opportunities.models",
    "OpportunityState": "domains.opportunities.models",
    "LossReason": "domains.opportunities.models",
    "HandoffPacket": "domains.opportunities.models",
    "HandoffState": "domains.opportunities.models",
    "HandoffTrigger": "domains.opportunities.models",
}


def _load(symbol: str):
    """按模块字符串导入符号；缺失转行为失败。"""
    try:
        return getattr(importlib.import_module(_MODULE_BY_SYMBOL[symbol]), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{symbol} 尚未定义（{exc}）")


def _opp(state):
    """构造指定状态的机会（models 行为测试用）。"""
    Opportunity = _load("Opportunity")
    return Opportunity(
        opportunity_id=OpportunityId("opp1"),
        tenant_id=TenantId("t1"),
        account_id=ProspectAccountId("acc1"),
        need_id=ValidatedNeedId("need1"),
        product_category="hinges",
        created_at=_NOW,
        account_name="Acme",
        country="US",
        state=state,
    )


# --- can_transition_to ----------------------------------------------------------


def test_can_transition_to_full_matrix() -> None:
    """全状态矩阵：合法 True、非法/自环 False。"""
    OpportunityState = _load("OpportunityState")
    states = list(OpportunityState)
    expected = {
        OpportunityState.QUALIFIED: {OpportunityState.ASSIGNED, OpportunityState.LOST},
        OpportunityState.ASSIGNED: {OpportunityState.CONTACTED, OpportunityState.LOST},
        OpportunityState.CONTACTED: {
            OpportunityState.SOURCING,
            OpportunityState.QUOTED,
            OpportunityState.LOST,
        },
        OpportunityState.SOURCING: {OpportunityState.QUOTED, OpportunityState.LOST},
        OpportunityState.QUOTED: {OpportunityState.NEGOTIATING, OpportunityState.LOST},
        OpportunityState.NEGOTIATING: {OpportunityState.WON, OpportunityState.LOST},
        OpportunityState.WON: set(),
        OpportunityState.LOST: set(),
    }
    for src in states:
        opp = _opp(src)
        for dst in states:
            assert opp.can_transition_to(dst) is (dst in expected[src]), (
                f"{src.value} -> {dst.value} 判定错误"
            )


def test_contacted_to_quoted_is_legal() -> None:
    """CONTACTED 可直接跳 QUOTED（现有产品完全匹配，无需寻源）。"""
    OpportunityState = _load("OpportunityState")
    opp = _opp(OpportunityState.CONTACTED)
    assert opp.can_transition_to(OpportunityState.QUOTED) is True


def test_terminal_states_have_no_out_edges() -> None:
    """WON/LOST 终态：任何转换（含自环）均 False。"""
    OpportunityState = _load("OpportunityState")
    for terminal in (OpportunityState.WON, OpportunityState.LOST):
        opp = _opp(terminal)
        for dst in OpportunityState:
            assert opp.can_transition_to(dst) is False


# --- mark_lost -------------------------------------------------------------------


def test_mark_lost_records_died_at_state_and_reason() -> None:
    """mark_lost 记录转入 lost 前的状态与 reason。"""
    OpportunityState = _load("OpportunityState")
    LossReason = _load("LossReason")
    opp = _opp(OpportunityState.QUOTED)
    opp.mark_lost(LossReason.PRICE_TOO_HIGH, _NOW)
    assert opp.state == OpportunityState.LOST
    assert opp.died_at_state == OpportunityState.QUOTED  # 转入前状态，不是 lost
    assert opp.loss_reason == LossReason.PRICE_TOO_HIGH
    assert opp.closed_at == _NOW


def test_mark_lost_on_terminal_does_not_rewrite_history() -> None:
    """终态重复终结：抛 InvalidStateTransition，历史不可改写。"""
    OpportunityState = _load("OpportunityState")
    LossReason = _load("LossReason")
    opp = _opp(OpportunityState.CONTACTED)
    opp.mark_lost(LossReason.PRICE_TOO_HIGH, _NOW)
    with pytest.raises(InvalidStateTransition):
        opp.mark_lost(LossReason.NO_REPLY, _NOW + timedelta(days=1))
    assert opp.loss_reason == LossReason.PRICE_TOO_HIGH
    assert opp.died_at_state == OpportunityState.CONTACTED
    assert opp.closed_at == _NOW

    won = _opp(OpportunityState.NEGOTIATING)
    won.state = OpportunityState.WON
    with pytest.raises(InvalidStateTransition):
        won.mark_lost(LossReason.NO_REPLY, _NOW)


# --- wait_seconds ----------------------------------------------------------------


def _handoff(requested_at=_NOW, *, state="requested", accepted_at=None):
    """构造接管包（models 行为测试用）。"""
    HandoffPacket = _load("HandoffPacket")
    HandoffState = _load("HandoffState")
    HandoffTrigger = _load("HandoffTrigger")
    return HandoffPacket(
        handoff_id=HandoffId("h1"),
        tenant_id=TenantId("t1"),
        opportunity_id=OpportunityId("opp1"),
        trigger=HandoffTrigger.QUOTE_REQUESTED,
        requested_at=requested_at,
        account_name="Acme",
        country="US",
        why_valuable="扩建",
        customer_verbatim="we need hinges",
        state=HandoffState(state),
        accepted_at=accepted_at,
    )


def test_wait_seconds_requested_uses_now() -> None:
    """requested 态：wait = now - requested_at（显式注入 now）。"""
    packet = _handoff(requested_at=_NOW)
    assert packet.wait_seconds(_NOW + timedelta(seconds=90)) == 90


def test_wait_seconds_accepted_uses_accepted_at() -> None:
    """accepted 态：wait = accepted_at - requested_at（不随 now 变化）。"""
    packet = _handoff(
        requested_at=_NOW, state="accepted", accepted_at=_NOW + timedelta(seconds=45)
    )
    assert packet.wait_seconds(_NOW + timedelta(hours=3)) == 45


def test_wait_seconds_zero_boundary() -> None:
    """边界为零：now == requested_at → 0；accepted_at == requested_at → 0。"""
    pending = _handoff(requested_at=_NOW)
    assert pending.wait_seconds(_NOW) == 0
    accepted = _handoff(requested_at=_NOW, state="accepted", accepted_at=_NOW)
    assert accepted.wait_seconds(_NOW + timedelta(hours=1)) == 0


def test_wait_seconds_rejects_negative() -> None:
    """负等待拒绝：注入 now 早于 requested_at。"""
    packet = _handoff(requested_at=_NOW)
    with pytest.raises(ValidationError):
        packet.wait_seconds(_NOW - timedelta(seconds=1))


def test_wait_seconds_rejects_naive_aware_mixing() -> None:
    """naive 与 aware datetime 混用 → ValidationError。"""
    packet = _handoff(requested_at=_NOW)  # aware
    naive_now = datetime.fromisoformat("2026-08-08T12:00:00")  # 故意 naive 验证混用拒绝
    with pytest.raises(ValidationError):
        packet.wait_seconds(naive_now)


# --- 强化验收：wait_seconds 以 accepted_at 是否存在为准（不随 state 名称漂移）-------


@pytest.mark.parametrize("terminal_state", ["completed", "reassigned"])
def test_wait_seconds_accepted_at_present_ignores_now(terminal_state: str) -> None:
    """COMPLETED/REASSIGNED 且 accepted_at 非空：固定 accepted_at-requested_at，不随 now 变。"""
    accepted_at = _NOW + timedelta(seconds=30)
    packet = _handoff(requested_at=_NOW, state=terminal_state, accepted_at=accepted_at)
    assert packet.wait_seconds(_NOW + timedelta(hours=5)) == 30
    assert packet.wait_seconds(_NOW + timedelta(days=30)) == 30  # now 持续增长不污染历史 SLA


def test_wait_seconds_expired_never_accepted_uses_now() -> None:
    """EXPIRED 且从未接受（accepted_at=None）：仍用注入 now（未接受语义）。"""
    packet = _handoff(requested_at=_NOW, state="expired")
    assert packet.wait_seconds(_NOW + timedelta(seconds=60)) == 60
    assert packet.wait_seconds(_NOW + timedelta(seconds=120)) == 120


def test_wait_seconds_accepted_missing_accepted_at_is_none() -> None:
    """ACCEPTED 但缺 accepted_at：数据不一致，返回 None（保持语义）。"""
    packet = _handoff(requested_at=_NOW, state="accepted")
    assert packet.wait_seconds(_NOW + timedelta(hours=1)) is None
