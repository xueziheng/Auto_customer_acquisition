"""真实 PG/engine/Gateway 的会话崩溃窗口；仅 Provider 使用受控输出。"""

from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from connectors.deepseek.client import DeepSeekFailure
from shared.schemas.model_invocation import ModelResponse, ModelUsage
from tests.integration.test_assistant import actor, input_text
from tests.integration.test_model_gateway import Authority as ModelAuthority
from tests.integration.test_model_usage import limits
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414
)
from tool_gateway.fingerprint import HmacFingerprintProvider


class Identity:
    allowed = True

    async def check(self, actor):
        from shared.errors import PermissionDenied

        if not self.allowed:
            raise PermissionDenied("已停用")

    async def resolve(self, actor):
        await self.check(actor)
        return "boss", frozenset({"product_help", "research_proposal", "business_read"})


class Reads:
    async def read(self, actor, ref):
        from agent_runtime.assistant.reads import PRODUCT_HELP
        from domains.assistant.schemas import AuthorizedFragment

        return AuthorizedFragment(text=PRODUCT_HELP, dependencies=(ref,))

    async def list(self, actor, query):
        return ()


class Provider:
    def __init__(self, mode="success", text=None):
        self.calls = 0
        self.mode = mode
        self.after = None
        self.text = (
            text
            or '{"kind":"clarify","questions":["请明确研究国家及预算"],"missing_fields":["target_countries"]}'
        )

    async def generate(self, request):
        self.calls += 1
        if self.after:
            await self.after()
        if self.mode == "timeout":
            raise DeepSeekFailure("unknown")
        return ModelResponse(
            model=request.model,
            text=self.text,
            usage=ModelUsage(input_tokens=20, cached_input_tokens=0, output_tokens=15),
        )

    async def aclose(self):
        pass


def assemble(engine, provider):
    from agent_runtime.assistant.context import (
        AssistantContextBuilder,
        HistoryProjector,
    )
    from agent_runtime.assistant.proposal import ResearchProposalBuilder
    from agent_runtime.guardrails.input_guard import CredentialMarkerGuard
    from apps.scheduler_worker.assistant import AssistantDispatcher
    from domains.assistant.service_impl import AssistantServiceImpl
    from infra.db.assistant import SqlAssistantRepository
    from infra.db.model_usage import SqlModelUsageRepository
    from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
    from infra.db.workflow_engine import PostgresWorkflowEngine
    from tests.integration.test_assistant_proposal import service as directives
    from tool_gateway.handlers.model_generate import GatewayModelGenerator
    from workflows.assistant.flow import build_assistant_definition
    from workflows.assistant.ports import AssistantRuntimePorts
    from workflows.assistant.steps import build_assistant_handlers

    sessions = async_sessionmaker(engine, expire_on_commit=False)
    repo = SqlAssistantRepository(sessions)
    identity = Identity()
    reads = Reads()
    projector = HistoryProjector(identity, reads, repo)
    fingerprints = HmacFingerprintProvider("test-v1", b"x" * 32)
    service = AssistantServiceImpl(
        repo, identity, CredentialMarkerGuard(), fingerprints, projector
    )
    generator = GatewayModelGenerator(
        authority=ModelAuthority(),
        usage=SqlModelUsageRepository(sessions),
        provider_factory=lambda: provider,
        limits=limits().model_copy(
            update={
                "max_input_bytes": 100000,
                "max_output_tokens": 2000,
                "tenant_calls": 10,
                "employee_calls": 10,
            }
        ),
        model="test-model",
        configuration_version="test-v1",
        ledger_factory=lambda tenant: SqlAlchemyToolGatewayUnitOfWork(sessions, tenant),
        fingerprints=fingerprints,
        lease_owner="assistant-test",
        lease_duration=timedelta(seconds=30),
    )
    ports = AssistantRuntimePorts(
        service,
        AssistantContextBuilder(
            identity,
            reads,
            repo,
            projector,
            configuration_version="test-v1",
            max_bytes=100000,
        ),
        reads,
        generator,
        ResearchProposalBuilder(),
        directives(engine),
        identity,
        fingerprints,
        "test-model",
        "test-v1",
        2000,
    )
    handlers = build_assistant_handlers(ports)
    workflow = PostgresWorkflowEngine(sessions, handlers)
    workflow.register(build_assistant_definition())
    return (
        service,
        workflow,
        AssistantDispatcher(service, workflow),
        sessions,
        handlers,
        identity,
    )


async def drain(workflow, tenant, count=8):
    for _ in range(count):
        await workflow.poll_due(tenant, 1)


async def test_selected_sources_complete_multiple_turns_and_survive_restart(unit_engine):
    from agent_runtime.assistant.reads import PRODUCT_HELP, PRODUCT_REF

    provider = Provider(text='{"kind":"explain","source_indexes":[0]}')
    app, wf, dispatcher, _db, _handlers, _identity = assemble(unit_engine, provider)
    owner = actor()
    session = await app.create_session(owner)
    ids = []
    for index in range(3):
        turn = await app.accept_turn(owner, session.session_id,
            input_text(text="聊天中的好是否批准商业承诺？", key=f"explain-{index}"))
        ids.append(turn.turn_id)
        await dispatcher.dispatch(owner.tenant_id, 10)
        await drain(wf, owner.tenant_id)
        result = await app.get_turn(owner, session.session_id, turn.turn_id)
        assert result.state == "completed"
        assert result.result.fragments[0].text == PRODUCT_HELP
        assert result.result.fragments[0].dependencies == (PRODUCT_REF,)
        assert result.result.fragments[0].source_turn_ids == tuple(ids)
        assert provider.calls == index + 1
    restarted = assemble(unit_engine, provider)
    await restarted[2].dispatch(owner.tenant_id, 10)
    await drain(restarted[1], owner.tenant_id)
    assert provider.calls == 3
    assert len(await restarted[0].list_turns(owner, session.session_id)) == 3


async def test_chinese_research_fields_and_two_budget_updates_form_exact_proposals(unit_engine):
    import json

    from infra.db.tables import DirectiveProposalRow, WorkflowRunRow
    from tests.integration.test_directives_persistence import BOSS
    from tests.unit.test_assistant_model_decision import RESEARCH_INPUT

    provider = Provider(text='{"kind":"research"}')
    app, wf, dispatcher, db, _handlers, _identity = assemble(unit_engine, provider)
    owner = actor(employee=BOSS)
    session = await app.create_session(owner)
    proposals = []
    for index, (message, budget) in enumerate(((RESEARCH_INPUT, "9"), ("搜索次数=6", "6"), ("搜索次数=3", "3"))):
        turn = await app.accept_turn(owner, session.session_id,
            input_text(text=message, key=f"research-{index}"))
        await dispatcher.dispatch(owner.tenant_id, 10)
        await drain(wf, owner.tenant_id)
        result = await app.get_turn(owner, session.session_id, turn.turn_id)
        assert result.state == "proposal_ready" and result.proposal_id
        proposals.append(result.proposal_id)
        async with db() as connection:
            proposal = await connection.scalar(select(DirectiveProposalRow).where(
                DirectiveProposalRow.tenant_id == owner.tenant_id,
                DirectiveProposalRow.proposal_id == result.proposal_id))
            fields = json.loads(proposal.raw_text)
            field = next(f for f in fields if f["name"] == "max_search_queries")
            assert field["value"] == budget and field["source_turn_id"] == turn.turn_id
            assert proposal.state == "pending_confirmation"
            plan = proposal.parsed_content["demand_discovery"]
            assert plan["max_search_queries"] == int(budget)
            assert plan["target_countries"] == ["US"]
            assert plan["execution_mode"] == "research_only"
            kinds = list(await connection.scalars(select(WorkflowRunRow.workflow_type).where(
                WorkflowRunRow.tenant_id == owner.tenant_id)))
            assert set(kinds) == {"assistant"}
        assert provider.calls == index + 1
    assert len(set(proposals)) == 3
    restarted = assemble(unit_engine, provider)
    await restarted[2].dispatch(owner.tenant_id, 10)
    await drain(restarted[1], owner.tenant_id)
    assert provider.calls == 3
    assert [t.proposal_id for t in await restarted[0].list_turns(owner, session.session_id)] == proposals


async def test_selected_source_revoked_before_delivery_stays_blocked(unit_engine):
    from shared.errors import PermissionDenied

    provider = Provider(text='{"kind":"explain","source_indexes":[0]}')
    app, wf, dispatcher, _db, handlers, _identity = assemble(unit_engine, provider)
    owner = actor()
    session = await app.create_session(owner)
    turn = await app.accept_turn(owner, session.session_id, input_text())

    async def revoke():
        async def denied(actor, ref):
            raise PermissionDenied("来源已撤权")
        handlers["assistant.generate"]._ports.read_port.read = denied

    provider.after = revoke
    await dispatcher.dispatch(owner.tenant_id, 10)
    await drain(wf, owner.tenant_id)
    result = (await app.execution(owner.tenant_id, turn.turn_id)).turn
    assert result.state == "blocked" and result.result is None
    assert provider.calls == 1


async def test_legacy_explanation_checkpoint_applies_without_regeneration(unit_engine):
    from agent_runtime.assistant.reads import PRODUCT_HELP, PRODUCT_REF
    from domains.assistant.schemas import AuthorizedFragment, Explanation

    provider = Provider()
    app, wf, dispatcher, _db, _handlers, _identity = assemble(unit_engine, provider)
    owner = actor()
    session = await app.create_session(owner)
    turn = await app.accept_turn(owner, session.session_id, input_text())
    await dispatcher.dispatch(owner.tenant_id, 10)
    await wf.poll_due(owner.tenant_id, 1)
    legacy = Explanation(fragments=(AuthorizedFragment(
        text=PRODUCT_HELP, dependencies=(PRODUCT_REF,), source_turn_ids=(turn.turn_id,)),))
    await app.checkpoint(owner, session.session_id, turn.turn_id, 0, legacy, (PRODUCT_REF,))
    restarted = assemble(unit_engine, provider)
    await restarted[2].dispatch(owner.tenant_id, 10)
    await drain(restarted[1], owner.tenant_id)
    result = await restarted[0].get_turn(owner, session.session_id, turn.turn_id)
    assert result.state == "completed" and result.result == legacy
    assert provider.calls == 0


@pytest.mark.parametrize("text,reason", [
    ('{"kind":"explain","source_indexes":[999]}', "assistant_output_reference"),
    ('{"kind":"explain","source_indexes":[0],"text":"虚构承诺"}', "assistant_output_schema"),
])
async def test_invalid_model_selection_records_safe_reason_without_retry(unit_engine, text, reason):
    provider = Provider(text=text)
    app, wf, dispatcher, _db, _handlers, _identity = assemble(unit_engine, provider)
    owner = actor()
    session = await app.create_session(owner)
    turn = await app.accept_turn(owner, session.session_id, input_text())
    await dispatcher.dispatch(owner.tenant_id, 10)
    await drain(wf, owner.tenant_id)
    run = await wf.get_run(owner.tenant_id, turn.run_id)
    assert run.status == "failed"
    assert run.context["assistant_output_failure"] == reason.removeprefix("assistant_")
    assert set(run.context) == {"turn_id", "assistant_output_failure"}
    result = await app.get_turn(owner, session.session_id, turn.turn_id)
    assert result.state == "failed" and result.error_code == "invalid_response"
    assert result.result is None
    await drain(wf, owner.tenant_id)
    assert provider.calls == 1


async def test_accept_stop_start_and_lost_bind_use_one_run(unit_engine):
    provider = Provider()
    app, wf, dispatcher, db, _handlers, _identity = assemble(unit_engine, provider)
    owner = actor()
    session = await app.create_session(owner)
    turn = await app.accept_turn(owner, session.session_id, input_text())
    assert provider.calls == 0
    await wf.start_once(
        owner.tenant_id,
        turn.run_id,
        "assistant",
        turn.turn_id,
        {"turn_id": turn.turn_id},
    )
    await dispatcher.dispatch(owner.tenant_id, 10)
    await dispatcher.dispatch(owner.tenant_id, 10)
    await drain(wf, owner.tenant_id)
    result = await app.get_turn(owner, session.session_id, turn.turn_id)
    assert result.state == "awaiting_input" and provider.calls == 1
    from infra.db.tables import WorkflowRunRow

    async with db() as connection:
        assert (
            await connection.scalar(
                select(func.count())
                .select_from(WorkflowRunRow)
                .where(WorkflowRunRow.tenant_id == owner.tenant_id)
            )
            == 1
        )


@pytest.mark.parametrize("mode", ["timeout", "lost_result", "cancel", "revoke"])
async def test_uncertain_and_late_results_never_repeat_provider(unit_engine, mode):
    provider = Provider(mode)
    app, wf, dispatcher, _db, _handlers, identity = assemble(unit_engine, provider)
    owner = actor()
    session = await app.create_session(owner)
    turn = await app.accept_turn(owner, session.session_id, input_text())
    if mode == "lost_result":

        async def broken(*args, **kwargs):
            raise RuntimeError("simulated write loss")

        app.checkpoint = broken
    if mode == "cancel":

        async def cancel():
            await app.cancel_turn(owner, session.session_id, turn.turn_id)

        provider.after = cancel
    if mode == "revoke":

        async def revoke():
            identity.allowed = False

        provider.after = revoke
    await dispatcher.dispatch(owner.tenant_id, 10)
    await drain(wf, owner.tenant_id)
    # Worker restart scans retained state; no implicit model attempt.
    restarted = assemble(unit_engine, provider)
    await restarted[2].dispatch(owner.tenant_id, 10)
    await drain(restarted[1], owner.tenant_id)
    result = (await app.execution(owner.tenant_id, turn.turn_id)).turn
    assert (
        result.state
        == {
            "timeout": "unknown",
            "lost_result": "unknown",
            "cancel": "cancelled",
            "revoke": "blocked",
        }[mode]
    )
    assert result.result is None and provider.calls == 1


async def test_persisted_result_before_engine_advance_is_reused(unit_engine):
    provider = Provider()
    app, wf, dispatcher, _db, handlers, _identity = assemble(unit_engine, provider)
    owner = actor()
    session = await app.create_session(owner)
    turn = await app.accept_turn(owner, session.session_id, input_text())
    await dispatcher.dispatch(owner.tenant_id, 10)
    await wf.poll_due(owner.tenant_id, 1)
    run = await wf.get_run(owner.tenant_id, turn.run_id)
    assert run.current_step == "generate"
    await handlers["assistant.generate"].execute(run)
    assert provider.calls == 1
    await drain(wf, owner.tenant_id)
    assert (
        await app.get_turn(owner, session.session_id, turn.turn_id)
    ).state == "awaiting_input"
    assert provider.calls == 1


async def test_committed_proposal_before_turn_binding_is_reused(unit_engine):
    from infra.db.tables import AssistantProposalSourceRow
    from tests.integration.test_directives_persistence import BOSS

    provider = Provider()
    app, wf, dispatcher, db, handlers, _identity = assemble(unit_engine, provider)
    owner = actor(employee=BOSS)
    session = await app.create_session(owner)
    for index in range(2):
        private = await app.accept_turn(owner, session.session_id,
            input_text(text="PRIVATE_HISTORY " + "内部随记" * 1400, key=f"private-{index}"))
        await dispatcher.dispatch(owner.tenant_id, 10)
        await drain(wf, owner.tenant_id)
        assert (await app.get_turn(owner, session.session_id, private.turn_id)).state == "awaiting_input"
    fields = {
        "objective": "研究铰链需求",
        "target_countries": "US",
        "target_categories": "hinges",
        "excluded_countries": "无",
        "excluded_categories": "无",
        "max_search_queries": "3",
        "max_pages_read": "2",
        "max_signals": "2",
        "max_hypotheses": "1",
        "minimum_confidence_tier": "low_mid",
        "strategy_group": "demand_first",
        "query_limit": "3",
    }
    turn = await app.accept_turn(
        owner,
        session.session_id,
        input_text(text="；".join(f"{k}={v}" for k, v in fields.items())),
    )
    provider.text = '{"kind":"research"}'
    await dispatcher.dispatch(owner.tenant_id, 10)
    await drain(wf, owner.tenant_id, 2)
    run = await wf.get_run(owner.tenant_id, turn.run_id)
    assert run.current_step == "apply_result"

    class Crash(BaseException):
        pass

    original = app.deliver

    async def die(*args, **kwargs):
        raise Crash()

    app.deliver = die
    with pytest.raises(Crash):
        await handlers["assistant.apply_result"].execute(run)
    app.deliver = original
    await drain(wf, owner.tenant_id)
    result = await app.get_turn(owner, session.session_id, turn.turn_id)
    assert result.state == "proposal_ready" and result.proposal_id
    async with db() as connection:
        rows = (
            await connection.scalars(
                select(AssistantProposalSourceRow).where(
                    AssistantProposalSourceRow.tenant_id == owner.tenant_id
                )
            )
        ).all()
        assert len(rows) == 1 and rows[0].proposal_id == result.proposal_id
    from infra.db.tables import DirectiveProposalRow
    async with db() as connection:
        proposal = (await connection.scalars(select(DirectiveProposalRow).where(
            DirectiveProposalRow.tenant_id == owner.tenant_id,
            DirectiveProposalRow.proposal_id == result.proposal_id))).one()
        assert "PRIVATE_HISTORY" not in proposal.raw_text
        assert len(proposal.raw_text) <= 10000 and turn.turn_id in proposal.raw_text
    assert provider.calls == 3


@pytest.mark.parametrize('window', ['checkpoint', 'before_checkpoint', 'export_revoked'])
async def test_changed_configuration_never_applies_or_replays_old_generation(unit_engine, window):
    from dataclasses import replace

    from shared.schemas.model_invocation import ModelGenerationError
    from workflows.assistant.steps import AssistantStepHandler

    provider = Provider()
    app, wf, dispatcher, _db, handlers, _identity = assemble(unit_engine, provider)
    owner = actor()
    session = await app.create_session(owner)
    turn = await app.accept_turn(owner, session.session_id, input_text())
    await dispatcher.dispatch(owner.tenant_id, 10)
    await wf.poll_due(owner.tenant_id, 1)
    run = await wf.get_run(owner.tenant_id, turn.run_id)
    if window == 'before_checkpoint':
        original = app.checkpoint
        async def crash(*args, **kwargs): raise KeyboardInterrupt()
        app.checkpoint = crash
        with pytest.raises(KeyboardInterrupt): await handlers['assistant.generate'].execute(run)
        app.checkpoint = original
    else:
        await handlers['assistant.generate'].execute(run)
    assert provider.calls == 1
    class Revoked:
        async def authorize(self, *args, **kwargs): raise ModelGenerationError('configuration')
    ports = handlers['assistant.generate']._ports
    if window == 'export_revoked': ports = replace(ports, configuration_service=Revoked())
    else: ports = replace(ports, configuration_version='test-v2')
    handler = AssistantStepHandler(ports, 'generate' if window == 'before_checkpoint' else 'apply_result')
    outcome = await handler.execute(run)
    result = (await app.execution(owner.tenant_id, turn.turn_id)).turn
    assert outcome[0] == 'fail' and result.state == 'blocked'
    assert result.result is None and provider.calls == 1


@pytest.mark.parametrize('count,expected_calls,expected_state', [(50,1,'awaiting_input'),(256,0,'failed')])
async def test_source_budget_is_checked_before_paid_generation(unit_engine, count, expected_calls, expected_state):
    from dataclasses import replace

    from domains.assistant.schemas import (
        AssistantReadQuery,
        AuthorizedFragment,
        ObjectRef,
        ReadRequest,
    )
    from workflows.assistant.steps import AssistantStepHandler

    provider = Provider()
    app, wf, dispatcher, _db, handlers, _identity = assemble(unit_engine, provider)
    owner=actor(); session=await app.create_session(owner)
    turn=await app.accept_turn(owner,session.session_id,input_text())
    await dispatcher.dispatch(owner.tenant_id,10); await wf.poll_due(owner.tenant_id,1)
    await app.checkpoint(owner,session.session_id,turn.turn_id,0,
        ReadRequest(query=AssistantReadQuery(kind='need',limit=50)),())
    class ManyReads(Reads):
        async def list(self, actor, query):
            return tuple(AuthorizedFragment(text='已授权来源',dependencies=(ObjectRef(kind='need',object_id=f'need_{i}'),)) for i in range(count))
    handler=AssistantStepHandler(replace(handlers['assistant.generate']._ports,read_port=ManyReads()),'generate_explanation')
    run=await wf.get_run(owner.tenant_id,turn.run_id)
    result=await handler.execute(run)
    if result[0]=='advance': await AssistantStepHandler(handler._ports,'apply_result').execute(run)
    assert provider.calls==expected_calls
    assert (await app.execution(owner.tenant_id,turn.turn_id)).turn.state==expected_state
