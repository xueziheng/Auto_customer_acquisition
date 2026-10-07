"""负责人提醒以真实 PostgreSQL 接受事务及站内提交为验收边界。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from domains.opportunities.permissions import (
    Actor,
    OpportunityScope,
    Phase1OpportunityAuthorizer,
    ScopeLevel,
    StandardAuditLogger,
)
from domains.opportunities.service_impl import HandoffPolicy, OpportunityServiceImpl
from infra.db.repositories.in_app_notifications import PostgresInAppNotificationStore
from infra.db.repositories.notification_jobs import PostgresNotificationJobStore
from infra.db.tables import (
    EmployeeRow,
    HandoffRow,
    InAppNotificationRow,
    OpportunityRow,
    OwnershipLockRow,
)
from infra.db.unit_of_work import SqlAlchemyOpportunityUnitOfWork
from notification_gateway.inbox import InAppNotification
from notification_gateway.jobs import (
    NotificationContext,
    NotificationJob,
    NotificationKind,
)
from notification_gateway.models import NotificationPriority
from shared.schemas.identifiers import (
    EmployeeId,
    HandoffId,
    NotificationId,
    NotificationJobId,
    OpportunityId,
    TenantId,
    new_id,
)
from tests.runtime_database_fixtures import RuntimeDatabaseFactory
from tests.runtime_database_fixtures import (
    runtime_database_url as runtime_database_url,  # noqa: PLC0414 - 真实受限运行角色夹具
)

NOW = datetime(2026, 9, 8, tzinfo=UTC)
INTERVAL = timedelta(seconds=7200)


class _UnusedScorer:
    async def score(self, *args, **kwargs):
        raise AssertionError("本测试不运行打分")


@pytest.fixture
async def scenario(integration_engine):
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    tenant, owner, opp, hand = (
        TenantId(new_id("tn")),
        EmployeeId(new_id("emp")),
        OpportunityId(new_id("opp")),
        HandoffId(new_id("hand")),
    )
    account = new_id("acc")
    async with factory.begin() as session:
        session.add(
            OwnershipLockRow(
                lock_id=new_id("loc"),
                tenant_id=tenant,
                account_id=account,
                owner=owner,
                locked_at=NOW,
                locked_by_rule="synthetic",
            )
        )
        session.add(
            EmployeeRow(
                tenant_id=tenant,
                employee_id=owner,
                name="合成员工",
                role="sales",
                is_active=True,
            )
        )
        session.add(
            OpportunityRow(
                tenant_id=tenant,
                opportunity_id=opp,
                account_id=account,
                account_name="合成客户",
                country="US",
                need_id=new_id("need"),
                product_category="合成类别",
                owner=owner,
            )
        )
        await session.flush()
        session.add(
            HandoffRow(
                tenant_id=tenant,
                handoff_id=hand,
                opportunity_id=opp,
                assigned_to=owner,
                trigger="quote_requested",
                requested_at=NOW,
                account_name="合成客户",
                country="US",
                why_valuable="合成理由",
                customer_verbatim="synthetic need",
            )
        )
    service = OpportunityServiceImpl(
        lambda: SqlAlchemyOpportunityUnitOfWork(factory, tenant),
        _UnusedScorer(),
        HandoffPolicy(sla_seconds=1, backlog_threshold=1),
        authorizer=Phase1OpportunityAuthorizer(tenant),
        audit=StandardAuditLogger(),
        now=lambda: NOW,
    )
    actor = Actor(
        str(owner),
        OpportunityScope(level=ScopeLevel.SELF, allowed_owners=frozenset({owner})),
        "sales",
    )
    system = Actor(
        "system:owner-reminder",
        OpportunityScope(level=ScopeLevel.SYSTEM, notification_opportunity_id=opp),
        "system",
    )
    return factory, tenant, owner, opp, hand, service, actor, system


async def _notification(factory, tenant, owner, opp, hand):
    job_id = NotificationJobId(new_id("njb"))
    context = NotificationContext(
        NotificationKind.HANDOFF_ESCALATION, hand, opp, "owner_reminder", None
    )
    await PostgresNotificationJobStore(factory, now=lambda: NOW).enqueue(
        NotificationJob(
            job_id,
            tenant,
            owner,
            NotificationPriority.URGENT,
            context,
            "a" * 64,
            "HandoffEscalationNotice",
            str(job_id),
            NOW,
        )
    )
    return InAppNotification(
        NotificationId(new_id("not")),
        tenant,
        owner,
        NotificationPriority.URGENT,
        "待接管提醒",
        context,
        f"/crm/handoffs/{hand}",
        job_id,
        NOW,
    )


async def test_accepted_commit_suppresses_queued_notification_without_outbox(scenario):
    factory, tenant, owner, opp, hand, service, actor, system = scenario

    @asynccontextmanager
    async def guard(notification):
        async with service.handoff_notification_scope(
            tenant, hand, opp, owner, actor=system
        ) as allowed:
            yield allowed

    store = PostgresInAppNotificationStore(factory, handoff_guard=guard)
    queued = await _notification(factory, tenant, owner, opp, hand)
    await service.accept_handoff(tenant, hand, owner, actor=actor)
    assert await store.append(queued) is False
    assert await store.list_for_recipient(tenant, owner, limit=20, before=None) == ()


async def test_delivery_holds_acceptance_until_real_in_app_commit(scenario):
    factory, tenant, owner, opp, hand, service, actor, system = scenario
    entered, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def guard(notification):
        async with service.handoff_notification_scope(
            tenant, hand, opp, owner, actor=system
        ) as allowed:
            entered.set()
            await release.wait()
            yield allowed

    store = PostgresInAppNotificationStore(factory, handoff_guard=guard)
    delivery = asyncio.create_task(
        store.append(await _notification(factory, tenant, owner, opp, hand))
    )
    await asyncio.wait_for(entered.wait(), 3)
    acceptance = asyncio.create_task(
        service.accept_handoff(tenant, hand, owner, actor=actor)
    )
    try:
        await asyncio.wait_for(asyncio.shield(acceptance), 0.15)
    except TimeoutError:
        pass
    else:
        pytest.fail("接受提交越过了在途通知的事务边界")
    release.set()
    assert await asyncio.wait_for(delivery, 3) is True
    await asyncio.wait_for(acceptance, 3)
    assert (
        await store.append(await _notification(factory, tenant, owner, opp, hand))
        is False
    )
    assert (
        len(await store.list_for_recipient(tenant, owner, limit=20, before=None)) == 1
    )


async def test_real_workflow_jobs_worker_two_hour_boundary_restart_and_delayed_acceptance(
    scenario, runtime_database_url: RuntimeDatabaseFactory,
):
    from pydantic import SecretStr

    from apps.notification_worker.config import NotificationWorkerConfig
    from apps.notification_worker.runtime import (
        NotificationRuntimeMode,
        notification_worker_runtime,
        run_notification_worker,
    )
    from apps.scheduler_worker.notification_projection import (
        NotificationJobHandoffNotifier,
    )
    from domains.employees.permissions import Actor as EmployeeActor
    from domains.employees.permissions import EmployeeScope
    from infra.db.workflow_engine import PostgresWorkflowEngine
    from infra.pilot.resources import reserve_port
    from shared.events.catalog import HandoffAccepted, HandoffRequested
    from tests.integration.test_human_handoff_workflow import (
        _Clock,
        _EmployeeService,
        _Registry,
    )
    from workflows.human_handoff.flow import (
        build_human_handoff_step_handlers,
        register_human_handoff,
    )

    factory, tenant, owner, opp, hand, service, actor, _system = scenario
    clock = _Clock(NOW)
    jobs = PostgresNotificationJobStore(factory, now=clock.now)

    def build():
        handlers = build_human_handoff_step_handlers(
            opportunity_service=service,
            employee_service=_EmployeeService(tenant),
            notifier=NotificationJobHandoffNotifier(jobs, now=clock.now),
            opportunity_system_actor=Actor(
                "system:reminder", OpportunityScope(level=ScopeLevel.SYSTEM), "system"
            ),
            employee_system_actor=EmployeeActor(
                "system:reminder", EmployeeScope.SYSTEM, "system"
            ),
            t1=timedelta(seconds=3),
            t2=timedelta(seconds=7),
            now=clock.now,
            owner_reminder_interval=INTERVAL,
        )
        workflow = PostgresWorkflowEngine(factory, handlers, now=clock.now)
        registry = _Registry()
        register_human_handoff(
            workflow,
            registry,
            t1=timedelta(seconds=3),
            t2=timedelta(seconds=7),
            owner_reminder_interval=INTERVAL,
        )
        return workflow, registry

    workflow, registry = build()
    requested = next(row[2] for row in registry.rows if row[0] is HandoffRequested)
    event = HandoffRequested(
        tenant_id=tenant,
        occurred_at=NOW,
        handoff_id=hand,
        opportunity_id=opp,
        assigned_to=owner,
        trigger="quote_requested",
    )
    await requested.handle(event)
    await workflow.poll_due(tenant, 20)
    with reserve_port(0) as listener:
        port = listener.getsockname()[1]
    settings = NotificationWorkerConfig(
        SecretStr(await runtime_database_url(str(tenant))), tenant, 1, 20, port, "owner-reminder-test"
    )
    async with notification_worker_runtime(
        settings, mode=NotificationRuntimeMode.LOCAL_IN_APP, now=clock.now
    ) as runtime:

        async def wait(_seconds, stop):
            stop.set()

        async def deliver():
            await run_notification_worker(runtime, wait=wait)

        store = PostgresInAppNotificationStore(factory)
        await deliver()
        assert (
            len(await store.list_for_recipient(tenant, owner, limit=20, before=None))
            == 1
        )
        clock.value = NOW + timedelta(seconds=7199)
        await workflow.poll_due(tenant, 20)
        await deliver()
        assert (
            len(await store.list_for_recipient(tenant, owner, limit=20, before=None))
            == 1
        )
        clock.value = NOW + INTERVAL
        await workflow.poll_due(tenant, 20)
        await deliver()
        assert (
            len(await store.list_for_recipient(tenant, owner, limit=20, before=None))
            == 2
        )
        workflow, registry = build()
        await next(
            row[2] for row in registry.rows if row[0] is HandoffRequested
        ).handle(event)
        await workflow.poll_due(tenant, 20)
        await deliver()
        assert (
            len(await store.list_for_recipient(tenant, owner, limit=20, before=None))
            == 2
        )
        clock.value = NOW + INTERVAL * 2
        await workflow.poll_due(tenant, 20)
        await deliver()
        assert (
            len(await store.list_for_recipient(tenant, owner, limit=20, before=None))
            == 3
        )
        clock.value = NOW + INTERVAL * 3
        await workflow.poll_due(tenant, 20)  # 接受前已入队
        await service.accept_handoff(tenant, hand, owner, actor=actor)
        await deliver()  # 接受事件尚未消费
        clock.value = NOW + INTERVAL * 4
        await workflow.poll_due(tenant, 20)
        await deliver()
        assert (
            len(await store.list_for_recipient(tenant, owner, limit=20, before=None))
            == 3
        )
        accepted = next(row[2] for row in registry.rows if row[0] is HandoffAccepted)
        await accepted.handle(
            HandoffAccepted(
                tenant_id=tenant,
                occurred_at=clock.now(),
                handoff_id=hand,
                accepted_by=owner,
            )
        )
        await workflow.poll_due(tenant, 20)
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(InAppNotificationRow).where(
                            InAppNotificationRow.tenant_id == tenant
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 3
            assert {row.recipient_employee_id for row in rows} == {owner}
            assert {row.title for row in rows} == {"待接管提醒"}
            assert (
                await session.scalar(
                    select(OpportunityRow.owner).where(
                        OpportunityRow.tenant_id == tenant,
                        OpportunityRow.opportunity_id == opp,
                    )
                )
                == owner
            )


async def test_startup_rejects_live_mode_or_interval_change(scenario):
    from infra.db.repositories.opportunities import (
        assert_handoff_reminder_compatibility,
    )
    from infra.db.workflow_engine import PostgresWorkflowEngine
    from shared.errors import ValidationError

    factory, tenant, _owner, _opp, hand, _service, _actor, _system = scenario
    from workflows.engine.runner import StepDefinition, WorkflowDefinition
    from workflows.sourcing_case.steps import FixedWaitStep

    engine = PostgresWorkflowEngine(
        factory, {"wait": FixedWaitStep("test")}, now=lambda: NOW
    )
    engine.register(
        WorkflowDefinition("human_handoff", 7201, (StepDefinition("wait", "wait"),))
    )
    await engine.start(tenant, "human_handoff", hand, {}, new_id("key"))
    await assert_handoff_reminder_compatibility(factory, tenant, 7200)
    with pytest.raises(ValidationError, match="handoff_reminder_policy_conflict"):
        await assert_handoff_reminder_compatibility(factory, tenant, None)
    with pytest.raises(ValidationError, match="handoff_reminder_policy_conflict"):
        await assert_handoff_reminder_compatibility(factory, tenant, 3600)


def _pilot_settings(tenant, db_url):
    import secrets
    import uuid
    from dataclasses import replace
    from decimal import Decimal

    from pydantic import SecretStr

    from apps.api.pilot import runtime_settings
    from infra.pilot.config import PilotConfig, PilotPolicy, allocated_port
    profile = PilotConfig(owner=uuid.uuid4().hex, tenant_id=tenant, api_port=allocated_port(), scheduler_port=allocated_port(), notification_port=allocated_port(), database_port=0, object_port=allocated_port(), bucket="pilot-"+uuid.uuid4().hex, storage={}, secrets={name:SecretStr(secrets.token_hex(32)) for name in ("PILOT_DATABASE_PASSWORD", "PILOT_OBJECT_ACCESS", "PILOT_OBJECT_SECRET", "PILOT_FINGERPRINT", "PILOT_UNSUBSCRIBE")}, policy=PilotPolicy.model_validate({"handoff_policy":{"sla_seconds":1,"backlog_threshold":1,"t1_seconds":3,"t2_seconds":7,"owner_reminder_interval_seconds":7200},"scoring_policy":{"version":"synthetic-reminder","currency":"USD","value_band_boundaries":[Decimal(250)],"bucket_map":{str(i):"low" for i in range(1,8)}}}))
    settings = replace(runtime_settings(profile), database_url=SecretStr(str(db_url)))
    env = profile.runtime_environment()
    env["DATABASE_URL"] = str(db_url)
    return profile, settings, env


def _scheduler_factory(profile, settings, env, now):
    from apps.scheduler_worker.bootstrap import CanonicalSchedulerBootstrap
    from apps.scheduler_worker.config import SchedulerWorkerConfig
    from apps.scheduler_worker.pilot import UnconfiguredDnsResolver, UnconfiguredDnsStep
    from apps.scheduler_worker.runtime import (
        SchedulerHealthServer,
        SchedulerRuntimeFactory,
    )
    return SchedulerRuntimeFactory(env, pilot_config=SchedulerWorkerConfig.from_pilot_environ(env), secret_resolver=profile, unconfigured_dns_step=UnconfiguredDnsStep(), bootstrap=CanonicalSchedulerBootstrap(settings.scoring_policy, settings.handoff_policy, research_enabled=False, contacts_enabled=False, campaign_enabled=False), resolver_factory=UnconfiguredDnsResolver, health_server_factory=lambda state,port: SchedulerHealthServer(state,port,host="127.0.0.1"), now=now)


@pytest.mark.parametrize("accept_at", ["before_scan", "queued_initial", "repeat"])
async def test_pilot_api_scheduler_outbox_and_real_notification_roots(
    scenario, runtime_database_url: RuntimeDatabaseFactory, accept_at,
):
    from apps.api.pilot import UnconfiguredModelClient
    from apps.api.runtime import create_runtime_app_from_settings
    from apps.notification_worker.config import NotificationWorkerConfig
    from apps.notification_worker.runtime import (
        NotificationRuntimeMode,
        notification_worker_runtime,
        run_notification_worker,
    )
    from connectors.object_store.config import S3ObjectStoreSettings
    from infra.db.outbox import PostgresEventBus
    from infra.db.tables import OutboxEventRow, WorkflowRunRow
    from shared.events.catalog import HandoffRequested
    from tests.integration.test_human_handoff_workflow import _Clock
    factory, tenant, owner, opp, hand, service, actor, _system = scenario
    profile, settings, env = _pilot_settings(tenant, await runtime_database_url(str(tenant)))
    app = create_runtime_app_from_settings(settings, secret_resolver=profile, model_client=UnconfiguredModelClient(), object_store_settings=S3ObjectStoreSettings.from_pilot_environ(env))
    clock = _Clock(NOW)
    async with app.router.lifespan_context(app):
        async with factory.begin() as session:
            await PostgresEventBus(session, tenant).publish(HandoffRequested(tenant_id=tenant, occurred_at=NOW, handoff_id=hand, opportunity_id=opp, assigned_to=owner, trigger="quote_requested"))
        if accept_at == "before_scan":
            await service.accept_handoff(tenant, hand, owner, actor=actor)
        async with _scheduler_factory(profile, settings, env, clock.now)() as scheduler:
            await scheduler.outbox.drain()
            await scheduler.workflow.poll_due(tenant, 20)
            async with factory() as session:
                assert await session.scalar(select(WorkflowRunRow.workflow_version).where(WorkflowRunRow.tenant_id == tenant, WorkflowRunRow.subject_ref == hand)) == 7201
            if accept_at == "queued_initial":
                await service.accept_handoff(tenant, hand, owner, actor=actor)
            expected = 1 if accept_at == "repeat" else 0
            notification_config = NotificationWorkerConfig(settings.database_url, tenant, 1, 20, profile.notification_port, "owner-roots-test")
            async with notification_worker_runtime(notification_config, mode=NotificationRuntimeMode.LOCAL_IN_APP, now=clock.now) as runtime:
                async def stop(_seconds, event):
                    event.set()
                await run_notification_worker(runtime, wait=stop)
                store = PostgresInAppNotificationStore(factory)
                assert len(await store.list_for_recipient(tenant, owner, limit=20, before=None)) == expected
                clock.value = NOW + INTERVAL
                await scheduler.workflow.poll_due(tenant, 20)
                if accept_at == "repeat":
                    await service.accept_handoff(tenant, hand, owner, actor=actor)
                await run_notification_worker(runtime, wait=stop)
                assert len(await store.list_for_recipient(tenant, owner, limit=20, before=None)) == expected
                await scheduler.outbox.drain()
                clock.value = NOW + INTERVAL * 2
                await scheduler.workflow.poll_due(tenant, 20)
                await run_notification_worker(runtime, wait=stop)
                assert len(await store.list_for_recipient(tenant, owner, limit=20, before=None)) == expected
            async with factory() as session:
                assert await session.scalar(select(WorkflowRunRow.status).where(WorkflowRunRow.tenant_id == tenant, WorkflowRunRow.subject_ref == hand)) == "completed"
                events = (await session.execute(select(OutboxEventRow.status).where(OutboxEventRow.tenant_id == tenant))).scalars().all()
                assert events and set(events) == {"delivered"}


@pytest.mark.parametrize("existing,configured", [(1,7200), (7201,None), (3601,7200)])
async def test_real_startups_reject_incompatible_active_run_without_side_effects(
    scenario, runtime_database_url: RuntimeDatabaseFactory, existing, configured,
):
    from dataclasses import replace

    from apps.api.pilot import UnconfiguredModelClient
    from apps.api.runtime import create_runtime_app_from_settings
    from connectors.object_store.config import S3ObjectStoreSettings
    from infra.db.tables import WorkflowRunRow
    from infra.db.workflow_engine import PostgresWorkflowEngine
    from shared.errors import ValidationError
    from workflows.engine.runner import StepDefinition, WorkflowDefinition
    from workflows.sourcing_case.steps import FixedWaitStep
    factory, tenant, _owner, _opp, hand, _service, _actor, _system = scenario
    engine = PostgresWorkflowEngine(factory, {"wait":FixedWaitStep("test")}, now=lambda:NOW)
    engine.register(WorkflowDefinition("human_handoff", existing, (StepDefinition("wait","wait"),)))
    await engine.start(tenant,"human_handoff",hand,{},new_id("key"))
    profile, settings, env = _pilot_settings(tenant, await runtime_database_url(str(tenant)))
    settings = replace(settings, owner_reminder_interval=None if configured is None else timedelta(seconds=configured))
    if configured is None:
        env.pop("TRADEOS_HANDOFF_OWNER_REMINDER_INTERVAL_SECONDS")
    else:
        env["TRADEOS_HANDOFF_OWNER_REMINDER_INTERVAL_SECONDS"] = str(configured)
    app = create_runtime_app_from_settings(settings, secret_resolver=profile, model_client=UnconfiguredModelClient(), object_store_settings=S3ObjectStoreSettings.from_pilot_environ(env))
    with pytest.raises(ValidationError, match="handoff_reminder_policy_conflict"):
        async with app.router.lifespan_context(app):
            pytest.fail("不兼容API不得ready")
    with pytest.raises(ValidationError, match="handoff_reminder_policy_conflict"):
        async with _scheduler_factory(profile,settings,env,lambda:NOW)():
            pytest.fail("不兼容scheduler不得ready")
    async with factory() as session:
        run = (await session.execute(select(WorkflowRunRow).where(WorkflowRunRow.tenant_id == tenant))).scalar_one()
        assert (run.status,run.workflow_version) == ("running",existing)
        assert (await session.execute(select(InAppNotificationRow).where(InAppNotificationRow.tenant_id == tenant))).scalars().all() == []


@pytest.mark.parametrize("change", ["employee_inactive", "opportunity_owner", "account_owner"])
async def test_current_facts_suppress_old_owner(scenario, change):
    factory, tenant, owner, opp, hand, service, _actor, system = scenario
    async with factory.begin() as session:
        if change == "employee_inactive":
            await session.execute(update(EmployeeRow).where(EmployeeRow.tenant_id == tenant, EmployeeRow.employee_id == owner).values(is_active=False))
        elif change == "opportunity_owner":
            await session.execute(update(OpportunityRow).where(OpportunityRow.tenant_id == tenant, OpportunityRow.opportunity_id == opp).values(owner=new_id("emp")))
        else:
            import importlib
            models = importlib.import_module("domains.employees.models")
            OwnershipLock, OwnershipTransfer = models.OwnershipLock, models.OwnershipTransfer
            from infra.db.repositories.employees import OwnershipRepositoryImpl
            from shared.schemas.identifiers import ProspectAccountId
            row = (await session.execute(select(OwnershipLockRow).where(OwnershipLockRow.tenant_id == tenant))).scalar_one()
            replacement = EmployeeId(new_id("emp"))
            session.add(EmployeeRow(tenant_id=tenant,employee_id=replacement,name="合成新负责人",role="sales",is_active=True))
            await session.flush()
            await OwnershipRepositoryImpl(session,tenant).replace(tenant, OwnershipLock(tenant_id=tenant, account_id=ProspectAccountId(row.account_id), owner=replacement, locked_at=NOW, locked_by_rule="transfer"), OwnershipTransfer(transfer_id=new_id("otr"), tenant_id=tenant, account_id=ProspectAccountId(row.account_id), from_owner=owner,to_owner=replacement,transferred_by=owner,transferred_at=NOW,reason="合成转移"))
    async with service.handoff_notification_scope(tenant, hand, opp, owner, actor=system) as allowed:
        assert allowed is False
    async with factory() as session:
        assert await session.scalar(select(HandoffRow.state).where(HandoffRow.tenant_id == tenant,HandoffRow.handoff_id == hand)) == "requested"


async def test_wrong_tenant_old_owner_and_duplicate_acceptance_rejected(scenario):
    from domains.opportunities.errors import HandoffAlreadyAcceptedError
    from shared.errors import PermissionDenied
    _factory, tenant, owner, opp, hand, service, actor, system = scenario
    with pytest.raises(PermissionDenied):
        async with service.handoff_notification_scope(TenantId(new_id("tn")), hand, opp, owner, actor=system):
            pytest.fail("跨租户读取不得进入")
    other = EmployeeId(new_id("emp"))
    wrong_actor = Actor(str(other),OpportunityScope(level=ScopeLevel.SELF,allowed_owners=frozenset({other})),"sales")
    with pytest.raises(PermissionDenied):
        await service.accept_handoff(tenant,hand,other,actor=wrong_actor)
    await service.accept_handoff(tenant,hand,owner,actor=actor)
    with pytest.raises(HandoffAlreadyAcceptedError):
        await service.accept_handoff(tenant,hand,owner,actor=actor)


def _lock_failure_state(error: BaseException) -> str:
    """只从原始异常提取 SQLSTATE；不可把原始数据库材料放进断言或输出。"""
    current: BaseException | None = error
    while current is not None:
        state = getattr(current, "sqlstate", None)
        if isinstance(state, str):
            return state
        current = getattr(current, "orig", None) or current.__cause__
    return "unexpected_failure"


async def test_transfer_history_and_owner_guard_commit_without_deadlock(
    scenario, integration_engine
):
    import importlib

    from sqlalchemy import event
    from sqlalchemy.exc import SQLAlchemyError

    from infra.db.repositories.employees import OwnershipRepositoryImpl
    from infra.db.tables import OwnershipTransferHistoryRow
    from shared.schemas.identifiers import ProspectAccountId

    models = importlib.import_module("domains.employees.models")
    factory, tenant, owner, opp, hand, service, _actor, system = scenario
    replacement = EmployeeId(new_id("emp"))
    async with factory.begin() as session:
        session.add(EmployeeRow(tenant_id=tenant, employee_id=replacement,
            name="合成新负责人", role="sales", is_active=True))
    notification = await _notification(factory, tenant, owner, opp, hand)
    owner_locked, transfer_updated = asyncio.Event(), asyncio.Event()

    def after_execute(_conn, _cursor, statement, _params, _context, _many):
        if "FROM employees" in statement and "FOR " in statement:
            owner_locked.set()

    @asynccontextmanager
    async def guard(notification):
        async with service.handoff_notification_scope(
            tenant, hand, opp, owner, actor=system
        ) as allowed:
            yield allowed

    store = PostgresInAppNotificationStore(factory, handoff_guard=guard)

    async def transfer():
        try:
            async with factory.begin() as session:
                row = (await session.execute(select(OwnershipLockRow).where(
                    OwnershipLockRow.tenant_id == tenant))).scalar_one()
                await OwnershipRepositoryImpl(session, tenant).replace(
                    tenant,
                    models.OwnershipLock(tenant_id=tenant,
                        account_id=ProspectAccountId(row.account_id), owner=replacement,
                        locked_at=NOW, locked_by_rule="transfer"),
                    models.OwnershipTransfer(transfer_id=new_id("otr"), tenant_id=tenant,
                        account_id=ProspectAccountId(row.account_id), from_owner=owner,
                        to_owner=replacement, transferred_by=owner, transferred_at=NOW,
                        reason="合成并发转移"),
                )
                transfer_updated.set()
                await asyncio.wait_for(owner_locked.wait(), 3)
                # 真实 flush/commit 的历史 FK 请求旧员工 KEY SHARE。
            return "committed"
        except SQLAlchemyError as error:
            return _lock_failure_state(error)

    async def reminder():
        await asyncio.wait_for(transfer_updated.wait(), 3)
        try:
            return "created" if await store.append(notification) else "suppressed"
        except SQLAlchemyError as error:
            return _lock_failure_state(error)

    event.listen(integration_engine.sync_engine, "after_cursor_execute", after_execute)
    tasks = [asyncio.create_task(transfer()), asyncio.create_task(reminder())]
    try:
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks), 8)
        assert outcomes.count("40P01") == 0
        assert outcomes == ["committed", "suppressed"]
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        event.remove(integration_engine.sync_engine, "after_cursor_execute", after_execute)

    async with factory() as session:
        history = (await session.execute(select(OwnershipTransferHistoryRow).where(
            OwnershipTransferHistoryRow.tenant_id == tenant))).scalars().all()
        assert len(history) == 1
        assert (history[0].from_owner, history[0].to_owner) == (owner, replacement)
        assert await session.scalar(select(OwnershipLockRow.owner).where(
            OwnershipLockRow.tenant_id == tenant)) == replacement
        assert await session.scalar(select(HandoffRow.state).where(
            HandoffRow.tenant_id == tenant, HandoffRow.handoff_id == hand)) == "requested"
    assert await store.list_for_recipient(tenant, owner, limit=20, before=None) == ()


async def test_matching_active_run_allows_real_api_and_scheduler_startups(
    scenario, runtime_database_url: RuntimeDatabaseFactory,
):
    from apps.api.pilot import UnconfiguredModelClient
    from apps.api.runtime import create_runtime_app_from_settings
    from connectors.object_store.config import S3ObjectStoreSettings
    from infra.db.tables import WorkflowRunRow

    factory, tenant, owner, opp, hand, _service, _actor, _system = scenario
    profile, settings, env = _pilot_settings(tenant, await runtime_database_url(str(tenant)))
    app = create_runtime_app_from_settings(settings, secret_resolver=profile,
        model_client=UnconfiguredModelClient(),
        object_store_settings=S3ObjectStoreSettings.from_pilot_environ(env))
    await app.state.dependencies.workflow_engine.start(tenant, "human_handoff", hand,
        {"handoff_id": hand, "opportunity_id": opp, "assigned_to": owner, "sla_started_at": NOW.isoformat()},
        new_id("key"), scheduled_at=NOW)
    async with (
        app.router.lifespan_context(app),
        _scheduler_factory(profile, settings, env, lambda: NOW)(),
        factory() as session,
    ):
        run = (await session.execute(select(WorkflowRunRow).where(
            WorkflowRunRow.tenant_id == tenant))).scalar_one()
        assert (run.status, run.workflow_version) == ("running", 7201)
        assert (await session.execute(select(InAppNotificationRow).where(
            InAppNotificationRow.tenant_id == tenant))).scalars().all() == []


async def test_employee_deactivation_waits_for_in_app_commit_then_suppresses(scenario):
    from infra.db.repositories.employees import EmployeeRepositoryImpl

    factory, tenant, owner, opp, hand, service, _actor, system = scenario
    locked, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def guard(notification):
        async with service.handoff_notification_scope(tenant, hand, opp, owner, actor=system) as allowed:
            locked.set()
            await release.wait()
            yield allowed

    store = PostgresInAppNotificationStore(factory, handoff_guard=guard)
    notification = await _notification(factory, tenant, owner, opp, hand)
    disable_started = asyncio.Event()

    async def deactivate():
        async with factory.begin() as session:
            repository = EmployeeRepositoryImpl(session, tenant)
            employee = await repository.get(tenant, owner)
            assert employee is not None
            employee.is_active = False
            disable_started.set()
            await repository.update(employee)

    delivery = asyncio.create_task(store.append(notification))
    deactivation = None
    try:
        await asyncio.wait_for(locked.wait(), 3)
        deactivation = asyncio.create_task(deactivate())
        await asyncio.wait_for(disable_started.wait(), 3)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(deactivation), 0.15)
        release.set()
        assert await asyncio.wait_for(delivery, 3) is True
        await asyncio.wait_for(deactivation, 3)
        assert await store.append(await _notification(factory, tenant, owner, opp, hand)) is False
        assert len(await store.list_for_recipient(tenant, owner, limit=20, before=None)) == 1
    finally:
        release.set()
        tasks = [delivery] + ([deactivation] if deactivation is not None else [])
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
