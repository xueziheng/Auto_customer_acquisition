"""私有会话接纳、原子意图、幂等和并发均以真实 PG 验证。"""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
from shared.schemas.identifiers import EmployeeId, TenantId, UserId, new_id
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414
)
from tool_gateway.fingerprint import HmacFingerprintProvider


class Authority:
    async def check(self, actor):
        return None


class Projector:
    async def project(self, actor, turn):
        return turn


def service(engine):
    from domains.assistant.service_impl import AssistantServiceImpl
    from infra.db.assistant import SqlAssistantRepository

    return AssistantServiceImpl(
        SqlAssistantRepository(async_sessionmaker(engine, expire_on_commit=False)),
        Authority(),
        CredentialMarkerGuard(),
        HmacFingerprintProvider("v1", b"h" * 32),
        Projector(),
    )


def actor(tenant=None, employee=None):
    from domains.assistant.schemas import AssistantActor

    return AssistantActor(
        tenant_id=tenant or TenantId(new_id("tn")),
        user_id=UserId(new_id("usr")),
        employee_id=employee or EmployeeId(new_id("emp")),
    )


def input_text(text="只研究美国铰链", key="first"):
    from domains.assistant.schemas import TurnInput

    return TurnInput(text=text, object_refs=(), idempotency_key=key)


async def test_accept_replays_same_turn_run_and_rejects_different_payload(
    unit_engine,
):
    from domains.assistant.errors import AssistantConflict

    app = service(unit_engine)
    user = actor()
    session = await app.create_session(user)
    first = await app.accept_turn(user, session.session_id, input_text())
    second = await app.accept_turn(user, session.session_id, input_text())
    assert first.turn_id == second.turn_id and first.run_id == second.run_id
    assert first.state == "queued"
    with pytest.raises(AssistantConflict):
        await app.accept_turn(user, session.session_id, input_text(text="不同输入"))
    assert len(await app.list_turns(user, session.session_id)) == 1


async def test_other_employee_even_boss_and_other_tenant_cannot_read(
    unit_engine,
):
    from domains.assistant.errors import AssistantNotFound

    app = service(unit_engine)
    owner = actor()
    session = await app.create_session(owner)
    turn = await app.accept_turn(owner, session.session_id, input_text())
    for outsider in (actor(owner.tenant_id), actor()):
        assert await app.list_sessions(outsider) == []
        with pytest.raises(AssistantNotFound):
            await app.get_turn(outsider, session.session_id, turn.turn_id)


async def test_concurrent_inputs_one_active_and_cancel_preserves_attempt(
    unit_engine,
):
    from domains.assistant.errors import AssistantConflict

    app = service(unit_engine)
    user = actor()
    session = await app.create_session(user)
    results = await asyncio.gather(
        app.accept_turn(user, session.session_id, input_text(key="one")),
        app.accept_turn(user, session.session_id, input_text(key="two")),
        return_exceptions=True,
    )
    assert sum(isinstance(r, AssistantConflict) for r in results) == 1
    first = next(r for r in results if not isinstance(r, Exception))
    cancelled = await app.cancel_turn(user, session.session_id, first.turn_id)
    assert cancelled.state == "cancelled"
    second = await app.accept_turn(user, session.session_id, input_text(key="three"))
    assert second.turn_id != first.turn_id and second.state == "queued"


async def test_credentials_rejected_before_persistence(unit_engine):
    from shared.errors import ValidationError

    app = service(unit_engine)
    user = actor()
    session = await app.create_session(user)
    with pytest.raises(ValidationError):
        await app.accept_turn(
            user, session.session_id, input_text(text="api_key=TEST_SENTINEL")
        )
    assert await app.list_turns(user, session.session_id) == []


async def test_delivered_clarification_releases_slot_and_replay_is_projected(
    unit_engine,
):
    from domains.assistant.schemas import Clarification

    app = service(unit_engine)
    user = actor()
    session = await app.create_session(user)
    first = await app.accept_turn(user, session.session_id, input_text())
    await app.deliver(user, session.session_id, first.turn_id, "running")
    await app.deliver(
        user,
        session.session_id,
        first.turn_id,
        "awaiting_input",
        result=Clarification(
            questions=("研究预算是多少？",), missing_fields=("max_search_queries",)
        ),
    )
    second = await app.accept_turn(user, session.session_id, input_text(key="next"))
    assert second.state == "queued"

    class HideHistory:
        async def project(self, actor, turn):
            return turn.model_copy(update={"result": None, "content_hidden": True})

    app._projector = HideHistory()
    replay = await app.accept_turn(user, session.session_id, input_text())
    assert replay.result is None and replay.content_hidden


async def test_regenerate_preserves_unknown_and_completed_turn_cannot_be_overwritten(
    unit_engine,
):
    from domains.assistant.errors import AssistantConflict

    app = service(unit_engine)
    user = actor()
    session = await app.create_session(user)
    first = await app.accept_turn(user, session.session_id, input_text())
    await app.deliver(user, session.session_id, first.turn_id, "running")
    await app.deliver(
        user, session.session_id, first.turn_id, "unknown", error_code="unknown"
    )
    regenerated = await app.regenerate(
        user, session.session_id, first.turn_id, "explicit-new-attempt"
    )
    assert regenerated.attempt_of == first.turn_id
    assert regenerated.run_id != first.run_id
    assert (
        await app.get_turn(user, session.session_id, first.turn_id)
    ).state == "unknown"
    with pytest.raises(AssistantConflict):
        await app.deliver(user, session.session_id, first.turn_id, "completed")
