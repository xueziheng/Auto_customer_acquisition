"""真实员工/配置/绑定/Gateway/分类事务；只替换原件内容和最外层模型 SDK。"""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from agent_runtime.assistant.reads import CurrentEmployeeIdentity
from agent_runtime.qualification_agent.agent import ReplyClassificationResult
from apps.composition_support.employee_readers import employee_service_scope
from apps.scheduler_worker.adapters.reply_current_access import CurrentReplyAccess
from apps.scheduler_worker.reply_model_binding import (
    BoundReplyClassifier,
    ReplyModelAuthority,
)
from domains.assistant.service_impl import ModelConfigurationServiceImpl
from domains.conversations.service_impl import ConversationServiceImpl
from domains.employees.permissions import (
    Actor,
    EmployeeScope,
    Phase1EmployeeAuthorizer,
    StandardAuditLogger,
)
from infra.db.conversations_uow import SqlAlchemyConversationsUnitOfWork
from infra.db.model_configuration import SqlModelConfigurationRepository
from infra.db.model_usage import SqlModelUsageRepository
from infra.db.reply_model_binding import SqlReplyRunBindingReader
from infra.db.reply_retry import SqlReplyRetryRepository
from infra.db.reply_retry_budget import SqlReplyRetryBudget
from infra.db.reply_retry_tables import ReplyRetryRow
from infra.db.tables import ModelInvocationRow, OutboxEventRow, WorkflowRunRow
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from infra.db.workflow_engine import PostgresWorkflowEngine
from tests.integration.test_model_usage import limits
from tests.integration.test_qq_trial_model import FakeSDK, FixedBrokerProvider
from tests.integration.test_reply_retry import NOW, seed
from tests.integration.test_reply_retry_resume import legacy_request
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.qq_trial_model import TrialModelPorts
from tool_gateway.reply_retry_model import ReplyRetryModelGenerator
from workflows.engine.runner import (
    StepDefinition,
    StepStatus,
    WorkflowDefinition,
    WorkflowRun,
)
from workflows.reply_qualification.ports import ReplyMessageContent
from workflows.reply_qualification.steps import ClassifyStep


@pytest.mark.parametrize("legacy", [False, True, "stopped", "invalid-result"])
async def test_actual_classification_commit_and_crash_replay_keep_one_event(
    integration_engine,
    legacy,
):
    if legacy == "stopped":
        from tests.integration.test_reply_retry_failed import failed_retry

        sessions, tenant, run, message, actor, _, _ = await failed_retry(
            integration_engine
        )
    else:
        sessions, tenant, run, message, actor, _, _ = await seed(integration_engine)

    def scope(t):
        return employee_service_scope(
            sessions,
            t,
            now=lambda: NOW,
            authorizer=Phase1EmployeeAuthorizer(tenant),
            audit=StandardAuditLogger(),
        )

    lookup = Actor("system:reply-model", EmployeeScope.SYSTEM, "system")
    fingerprints = HmacFingerprintProvider("v1", b"k" * 32)
    cfg = SqlModelConfigurationRepository(
        sessions,
        fingerprints,
        now=lambda: NOW,
        deployment_verification=(tenant, "retry-v1", NOW),
    )
    for process in ("api", "scheduler"):
        await cfg.register_process(tenant, process, "retry-v1", "fixture-" + process)
    configuration = ModelConfigurationServiceImpl(
        cfg, CurrentEmployeeIdentity(scope, lookup), now=lambda: NOW
    )
    repo = SqlReplyRetryRepository(
        sessions, configuration, now=lambda: NOW + timedelta(seconds=5)
    )
    if legacy == "stopped":
        from workflows.reply_qualification.retry import ReplyFailedRetryCommand

        repo.now = lambda: NOW + timedelta(days=1)
        await repo.retry_failed(
            tenant,
            message,
            actor,
            "classification-stopped",
            ReplyFailedRetryCommand(expected_sequence=1, acknowledge_cost=True),
        )
    elif legacy is True:
        await legacy_request(repo, tenant, message, actor, "classification-recovery")
        repo.now = lambda: NOW + timedelta(minutes=6)
        await repo.resume(tenant, message, actor, "classification-resume")
    else:
        await repo.request(tenant, message, actor, "classification-recovery")
    authority = ReplyModelAuthority(
        SqlReplyRunBindingReader(sessions),
        scope,
        lookup,
        configuration,
        tenant_id=tenant,
        employee_id=actor.employee_id,
        model="test-model",
        retries=repo,
    )

    async def commit(db, msg):
        await repo.commit_message(db, tenant, msg)

    def uow(t):
        return SqlAlchemyConversationsUnitOfWork(
            sessions, t, now=lambda: NOW, trial_commit=commit
        )

    conversations = ConversationServiceImpl(uow, now=lambda: NOW)

    class Forbidden:
        def __getattr__(self, name):
            raise AssertionError("stopped trial or default model must not run")

    access = CurrentReplyAccess(
        tenant, actor.employee_id, scope, uow, Forbidden(), repo
    )

    class InvalidSDK(FakeSDK):
        async def create(self, **body):
            response = (await super().create(**body)).model_dump()
            if len(self.requests) == 2:
                response["output"][0]["content"][0]["text"] = (
                    '{"category":"not_a_category"}'
                )
            return SimpleNamespace(model_dump=lambda: response)

    sdk = InvalidSDK() if legacy == "invalid-result" else FakeSDK()

    class FixedTimeUsage(SqlModelUsageRepository):
        async def reserve(self, identity, request_hmac, limits, now, **kwargs):
            return await super().reserve(
                identity, request_hmac, limits, repo.now(), **kwargs
            )

    ports = TrialModelPorts(
        authority,
        FixedTimeUsage(sessions),
        lambda t: SqlAlchemyToolGatewayUnitOfWork(sessions, t),
        fingerprints,
        limits(max_input_bytes=32768, max_output_tokens=512, timeout_seconds=60),
        "retry-classification",
        timedelta(seconds=90),
        "deepseek",
    )

    def generator(identity, msg):
        return ReplyRetryModelGenerator(
            identity,
            "test-model",
            SqlReplyRetryBudget(repo, tenant, msg, sequence=identity.sequence),
            ports,
            lambda b, i: FixedBrokerProvider(b, sdk),
        )

    async def replay(msg):
        if not await repo.committed(tenant, msg):
            return None
        stored = await conversations.get_classification(tenant, msg)
        return ReplyClassificationResult(category=stored.category)

    classifier = BoundReplyClassifier(
        authority,
        Forbidden(),
        model="test-model",
        configuration_version="retry-v1",
        max_output_tokens=512,
        retry_generator=generator,
        replay=replay,
    )

    class Content:
        async def load(self, *args):
            return ReplyMessageContent(subject="Private test", body="I want to buy.")

    class Guard:
        def check(self, **kwargs):
            pass

    handler = ClassifyStep(classifier, Content(), Guard(), conversations, access)
    async with sessions() as db:
        row = await db.scalar(
            select(WorkflowRunRow).where(
                WorkflowRunRow.tenant_id == tenant, WorkflowRunRow.run_id == run
            )
        )
        workflow = WorkflowRun(
            run_id=run,
            tenant_id=tenant,
            workflow_type=row.workflow_type,
            workflow_version=1,
            subject_ref=message,
            current_step="classify",
            status=StepStatus.PENDING,
            created_at=NOW,
            context=row.context,
        )
    results = []

    class ObservedClassify:
        async def execute(self, claimed):
            result = await handler.execute(claimed)
            results.append(result)
            return result

    class NotRun:
        async def execute(self, claimed):
            raise AssertionError("only classification is claimed by this test")

    engine = PostgresWorkflowEngine(
        sessions,
        {"classify": ObservedClassify(), "apply_actions": NotRun()},
        now=lambda: repo.now(),
    )
    engine.register(
        WorkflowDefinition(
            workflow_type="reply_qualification",
            version=1,
            steps=(
                StepDefinition(step_name="classify", handler_ref="classify"),
                StepDefinition(step_name="apply_actions", handler_ref="apply_actions"),
            ),
            transitions={"classify": ["apply_actions"]},
        )
    )
    assert await engine.poll_due(tenant, 1) == 1
    if legacy == "invalid-result":
        assert not results and not await repo.committed(tenant, message)
        async with sessions() as db:
            previous = await db.scalar(
                select(ReplyRetryRow).where(
                    ReplyRetryRow.tenant_id == tenant,
                    ReplyRetryRow.message_id == message,
                )
            )
            call = await db.scalar(
                select(ModelInvocationRow).where(
                    ModelInvocationRow.tenant_id == tenant,
                    ModelInvocationRow.run_id == run,
                    ModelInvocationRow.sequence == 1,
                )
            )
            assert (
                previous.state == "running"
                and call.state == "succeeded"
                and call.slot_released
            )
        from workflows.reply_qualification.retry import ReplyFailedRetryCommand

        repo.now = lambda: NOW + timedelta(days=1)
        assert (await repo.read(tenant, message, actor)).can_retry
        await repo.retry_failed(
            tenant,
            message,
            actor,
            "invalid-result-retry",
            ReplyFailedRetryCommand(expected_sequence=1, acknowledge_cost=True),
        )
        assert len(sdk.requests) == 2
        sdk = FakeSDK()
        assert await engine.poll_due(tenant, 1) == 1
    assert len(results) == 1
    first = results[0]
    assert (
        first[0:2] == ("advance", "apply_actions")
        and first[2]["category"] == "clear_interest"
    )
    assert await repo.committed(tenant, message)
    # 模拟分类事务已提交但原引擎事务崩溃；第二次复用真实持久分类。
    assert await handler.execute(workflow) == first
    assert len(sdk.requests) == 2
    async with sessions() as db:
        calls = (
            await db.scalars(
                select(ModelInvocationRow).where(
                    ModelInvocationRow.tenant_id == tenant,
                    ModelInvocationRow.run_id == run,
                )
            )
        ).all()
        assert len(calls) == (3 if legacy in {"stopped", "invalid-result"} else 2)
        events = (
            await db.scalars(
                select(OutboxEventRow).where(
                    OutboxEventRow.tenant_id == tenant,
                    OutboxEventRow.event_type == "ReplyReceived",
                )
            )
        ).all()
        assert len(events) == 1
