"""有限流程与受信 Run 绑定。"""

from apps.scheduler_worker.assistant import AssistantDispatcher
from workflows.assistant.flow import build_assistant_definition


def test_assistant_has_bounded_generation_without_automatic_retry():
    definition = build_assistant_definition()
    assert {s.step_name for s in definition.steps if "generate" in s.step_name} == {
        "generate",
        "generate_explanation",
    }
    assert all(s.max_retries == 0 for s in definition.steps)
    assert "generate" not in definition.transitions.get("generate_explanation", ())


async def test_dispatch_preserves_preallocated_run_and_recovers_before_bind():
    from domains.assistant.schemas import TurnExecution
    from tests.integration.test_assistant import actor
    from tests.unit.test_assistant_context import turn

    execution = TurnExecution(
        actor=actor(),
        turn=turn("turn_a").model_copy(update={"state": "queued"}),
        dispatch_state="pending",
    )

    class Service:
        def __init__(self):
            self.bound = []

        async def pending(self, tenant, limit):
            return [execution]

        async def bind(self, tenant, turn, run):
            self.bound.append(run)

    class Engine:
        def __init__(self):
            self.calls = []

        async def start_once(self, tenant, run, type, subject, context):
            self.calls.append(run)
            return run

        async def get_run(self, tenant, run):
            return None

    service, engine = Service(), Engine()
    dispatcher = AssistantDispatcher(service, engine)
    await dispatcher.dispatch(execution.actor.tenant_id, 10)
    await dispatcher.dispatch(execution.actor.tenant_id, 10)
    assert service.bound == [execution.turn.run_id] * 2
    assert engine.calls == service.bound
