"""真实员工/配置/业务绑定/Gateway；仅模型 Provider 为受控外部端口。"""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from agent_runtime.assistant.reads import CurrentEmployeeIdentity
from apps.composition_support.employee_readers import employee_service_scope
from connectors.deepseek.client import DeepSeekFailure
from domains.assistant.schemas import AssistantActor
from domains.assistant.service_impl import ModelConfigurationServiceImpl
from domains.employees.permissions import (
    Actor,
    EmployeeScope,
    Phase1EmployeeAuthorizer,
    StandardAuditLogger,
)
from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.db.model_usage import SqlModelUsageRepository
from infra.db.reply_model_binding import SqlReplyRunBindingReader
from infra.db.tables import (
    AgentTurnRow,
    EmployeeRow,
    ModelInvocationRow,
    ModelRuntimeProcessRow,
    ToolCallRow,
    WorkflowRunRow,
)
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.schemas.identifiers import new_id
from shared.schemas.model_invocation import (
    InvocationIdentity,
    ModelGenerationError,
    ModelResponse,
    ModelUsage,
)
from tests.integration.test_model_usage import limits
from tests.integration.test_reply_model_binding import NOW, seed_binding
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.model_generate import GatewayModelGenerator

BODY = "We need 5000 hinges."


class Provider:
    def __init__(self, *, unknown=False):
        self.calls = []
        self.unknown = unknown
        self.after = None

    async def generate(self, request):
        self.calls.append(request)
        if self.after is not None:
            await self.after()
        if self.unknown:
            raise DeepSeekFailure("unknown")
        return ModelResponse(
            text=json.dumps(
                {
                    "category": "provides_specification",
                    "candidate_fields": [
                        {
                            "field": "product_category",
                            "value": "hinges",
                            "quote": "hinges",
                        },
                        {"field": "quantity", "value": "5000", "quote": "5000"},
                    ],
                }
            ),
            model=request.model,
            usage=ModelUsage(input_tokens=20, cached_input_tokens=0, output_tokens=30),
        )

    async def aclose(self):
        pass


async def configured_reply(engine, *, gate="ready", provider=None):
    from apps.scheduler_worker.reply_model_binding import (
        BoundReplyClassifier,
        ReplyModelAuthority,
    )

    sessions, tenant, run, message = await seed_binding(engine)
    employee = new_id("emp")
    actor = AssistantActor(
        tenant_id=tenant, employee_id=employee, user_id=new_id("usr")
    )
    async with sessions.begin() as db:
        db.add(
            EmployeeRow(
                tenant_id=tenant,
                employee_id=employee,
                user_id=actor.user_id,
                name="回复测试员工",
                role="boss",
                is_active=True,
            )
        )

    def scope(requested):
        return employee_service_scope(
            sessions,
            requested,
            now=lambda: NOW,
            authorizer=Phase1EmployeeAuthorizer(tenant),
            audit=StandardAuditLogger(),
        )

    lookup = Actor("system:reply-model", EmployeeScope.SYSTEM, "system")
    fingerprints = HmacFingerprintProvider("test-v1", b"k" * 32)
    repo = SqlModelConfigurationRepository(sessions, fingerprints, now=lambda: NOW)
    configuration = ModelConfigurationServiceImpl(
        repo, CurrentEmployeeIdentity(scope, lookup), now=lambda: NOW
    )
    policy = limits(
        tenant_calls=1, employee_calls=1, max_input_bytes=65536, max_output_tokens=512
    )
    if gate != "missing_config":
        await repo.initialize(
            tenant, "reply-v1", "test-model", policy, gate != "export"
        )
        for process in ("api", "scheduler"):
            await repo.register_process(tenant, process, "reply-v1", f"test-{process}")
        if gate not in {"probe", "export"}:
            probe = await configuration.request_probe(actor, "controlled-probe")
            async with sessions.begin() as db:
                await db.execute(
                    update(AgentTurnRow)
                    .where(
                        AgentTurnRow.tenant_id == tenant,
                        AgentTurnRow.turn_id == probe.turn_id,
                    )
                    .values(state="running")
                )
            await configuration.complete_probe(actor, probe.turn_id, "reply-v1")
    if gate in {"role", "inactive", "missing_user"}:
        values = (
            {"role": "sales"}
            if gate == "role"
            else {"is_active": False}
            if gate == "inactive"
            else {"user_id": None}
        )
        async with sessions.begin() as db:
            await db.execute(
                update(EmployeeRow)
                .where(
                    EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == employee
                )
                .values(**values)
            )
    if gate == "heartbeat":
        async with sessions.begin() as db:
            await db.execute(
                update(ModelRuntimeProcessRow)
                .where(
                    ModelRuntimeProcessRow.tenant_id == tenant,
                    ModelRuntimeProcessRow.process == "scheduler",
                )
                .values(heartbeat_at=NOW - timedelta(hours=1))
            )
    authority = ReplyModelAuthority(
        SqlReplyRunBindingReader(sessions),
        scope,
        lookup,
        configuration,
        tenant_id=tenant,
        employee_id=employee,
        model="test-model",
    )
    provider = provider or Provider()
    usage = SqlModelUsageRepository(sessions)

    def build():
        generator = GatewayModelGenerator(
            authority=authority,
            usage=usage,
            provider_factory=lambda: provider,
            limits=policy,
            model="test-model",
            configuration_version="reply-v1",
            ledger_factory=lambda requested: SqlAlchemyToolGatewayUnitOfWork(
                sessions, requested
            ),
            fingerprints=fingerprints,
            lease_owner="reply-tests",
            lease_duration=timedelta(seconds=30),
        )
        return BoundReplyClassifier(
            authority,
            generator,
            model="test-model",
            configuration_version="reply-v1",
            max_output_tokens=policy.max_output_tokens,
        )

    return SimpleNamespace(
        sessions=sessions,
        tenant=tenant,
        run=run,
        message_id=message,
        actor=actor,
        authority=authority,
        provider=provider,
        usage=usage,
        policy=policy,
        build=build,
        message={"message_id": message, "subject": "(current reply)", "body": BODY},
    )


async def invocation_rows(runtime):
    async with runtime.sessions() as db:
        return list(
            (
                await db.scalars(
                    select(ModelInvocationRow).where(
                        ModelInvocationRow.tenant_id == runtime.tenant
                    )
                )
            ).all()
        )


async def test_reply_identity_comes_from_current_employee_and_run(integration_engine):
    r = await configured_reply(integration_engine)
    identity = await r.authority.identity_for_message(r.message_id, "reply-v1")
    assert identity.capability == "reply_qualification"
    assert (
        identity.run_id == r.run and identity.turn_id is None and identity.sequence == 0
    )
    assert (
        identity.employee_id == r.actor.employee_id
        and identity.user_id == r.actor.user_id
    )
    result = await r.build().classify(message=r.message)
    assert result.category.value == "provides_specification"
    assert {(f.field, f.value, f.quote) for f in result.candidate_fields} == {
        ("product_category", "hinges", "hinges"),
        ("quantity", "5000", "5000"),
    }
    assert len(r.provider.calls) == 1
    assert r.provider.calls[0].payload == {"subject": "(current reply)", "body": BODY}
    assert r.provider.calls[0].max_output_tokens == 512
    rows = await invocation_rows(r)
    assert len(rows) == 1 and rows[0].state == "succeeded" and rows[0].run_id == r.run
    async with r.sessions() as db:
        tools = (
            await db.scalars(
                select(ToolCallRow.tool_id).where(ToolCallRow.tenant_id == r.tenant)
            )
        ).all()
        assert tools == ["model.generate"]


@pytest.mark.parametrize(
    "gate",
    [
        "missing_config",
        "export",
        "probe",
        "heartbeat",
        "role",
        "inactive",
        "missing_user",
    ],
)
async def test_unavailable_authority_or_configuration_prevents_paid_call(
    integration_engine, gate
):
    r = await configured_reply(integration_engine, gate=gate)
    with pytest.raises(ModelGenerationError):
        await r.build().classify(message=r.message)
    assert r.provider.calls == []
    assert await invocation_rows(r) == []


@pytest.mark.parametrize(
    "patch",
    [
        {"configuration_version": "old-v0"},
        {"employee_id": "emp_other"},
        {"user_id": "usr_other"},
        {"model": "other-model"},
        {"multiple": True},
    ],
)
@pytest.mark.parametrize(
    "state", ["reserved", "rejected", "invalid", "unknown", "succeeded"]
)
async def test_prior_invocation_pins_actor_model_and_version(
    integration_engine, patch, state
):
    r = await configured_reply(integration_engine)
    values = {
        "tenant_id": r.tenant,
        "user_id": r.actor.user_id,
        "employee_id": r.actor.employee_id,
        "run_id": r.run,
        "capability": "reply_qualification",
        "configuration_version": "reply-v1",
        "sequence": 0,
    }
    old = InvocationIdentity.model_validate(
        values | {k: v for k, v in patch.items() if k in values}
    )
    policy = limits(
        tenant_calls=10,
        employee_calls=10,
        tenant_concurrency=10,
        employee_concurrency=10,
    )
    claim = await r.usage.reserve(
        old, "a" * 64, policy, NOW, model=patch.get("model", "test-model")
    )
    if state != "reserved":
        if state != "rejected":
            await r.usage.mark_dispatched(r.tenant, claim.invocation_id)
        await r.usage.finish(
            r.tenant,
            claim.invocation_id,
            ModelUsage(input_tokens=None, cached_input_tokens=None, output_tokens=None),
            state,
        )
    if patch.get("multiple"):
        await r.usage.reserve(
            InvocationIdentity.model_validate(
                values | {"configuration_version": "old-v0"}
            ),
            "b" * 64,
            policy,
            NOW,
            model="test-model",
        )
    with pytest.raises(ModelGenerationError) as caught:
        await r.build().classify(message=r.message)
    assert caught.value.code == "permission"
    assert r.provider.calls == []
    assert len(await invocation_rows(r)) == (2 if patch.get("multiple") else 1)


@pytest.mark.parametrize("change", ["employee", "association"])
async def test_revocation_during_generation_discards_result(integration_engine, change):
    r = await configured_reply(integration_engine)

    async def revoke():
        async with r.sessions.begin() as db:
            if change == "employee":
                await db.execute(
                    update(EmployeeRow)
                    .where(
                        EmployeeRow.tenant_id == r.tenant,
                        EmployeeRow.employee_id == r.actor.employee_id,
                    )
                    .values(is_active=False)
                )
            else:
                await db.execute(
                    update(WorkflowRunRow)
                    .where(
                        WorkflowRunRow.tenant_id == r.tenant,
                        WorkflowRunRow.run_id == r.run,
                    )
                    .values(context={"message_id": "forged"})
                )

    r.provider.after = revoke
    with pytest.raises(ModelGenerationError) as caught:
        await r.build().classify(message=r.message)
    assert caught.value.code == "permission"
    rows = await invocation_rows(r)
    assert len(r.provider.calls) == 1 and rows[0].state == "succeeded"
    assert rows[0].input_tokens == 20


@pytest.mark.parametrize("unknown", [False, True])
async def test_unknown_or_lost_result_is_not_resent(integration_engine, unknown):
    r = await configured_reply(integration_engine, provider=Provider(unknown=unknown))
    if unknown:
        with pytest.raises(ModelGenerationError) as caught:
            await r.build().classify(message=r.message)
        assert caught.value.code == "unknown"
    else:
        await r.build().classify(message=r.message)
    with pytest.raises(ModelGenerationError) as caught:
        await r.build().classify(message=r.message)
    assert caught.value.code == "unknown" and len(r.provider.calls) == 1
    rows = await invocation_rows(r)
    assert len(rows) == 1 and rows[0].state == ("unknown" if unknown else "succeeded")
    if unknown:
        assert rows[0].input_tokens is None and not rows[0].slot_released


async def test_paid_quota_is_checked_before_reply_provider(integration_engine):
    r = await configured_reply(integration_engine)
    identity = InvocationIdentity(
        tenant_id=r.tenant,
        user_id=r.actor.user_id,
        employee_id=r.actor.employee_id,
        run_id="run_other",
        capability="reply_qualification",
        configuration_version="reply-v1",
        sequence=0,
    )
    from datetime import UTC, datetime

    claim = await r.usage.reserve(
        identity, "a" * 64, r.policy, datetime.now(UTC), model="test-model"
    )
    await r.usage.mark_dispatched(r.tenant, claim.invocation_id)
    with pytest.raises(ModelGenerationError) as caught:
        await r.build().classify(message=r.message)
    assert caught.value.code == "quota" and r.provider.calls == []
