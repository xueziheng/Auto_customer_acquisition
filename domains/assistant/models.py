"""会话状态约束；交付澄清和提案后释放当前执行槽。"""

from domains.assistant.schemas import TurnState

ACTIVE_TURN_STATES = frozenset({"queued", "running"})
TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled", "blocked", "failed"}),
    "running": frozenset(
        {
            "awaiting_input",
            "proposal_ready",
            "completed",
            "blocked",
            "failed",
            "unknown",
            "cancelled",
        }
    ),
}


def is_active_turn(state: str) -> bool:
    return state in ACTIVE_TURN_STATES


def can_transition(current: TurnState, target: TurnState) -> bool:
    return current == target or target in TRANSITIONS.get(current, frozenset())
