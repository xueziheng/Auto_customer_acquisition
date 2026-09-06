"""真实 PostgreSQL 验证 Catalog Policy workflow 的提交顺序、恢复与通知。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from apps.scheduler_worker.notification_projection import (
    NotificationAudienceMember,
    NotificationProjectionHandler,
)
from domains.approvals.catalog_contract import CatalogApprovalActorFact
from domains.approvals.schemas import ApprovalReaderIdentity
from domains.approvals.service import ApprovalState, ApprovalType, BlastRadius
from domains.approvals.service_impl import ApprovalServiceImpl
from domains.products.catalog_service_impl import CatalogProposalServiceImpl
from domains.products.permissions import (
    Phase2ProductAuthorizer,
    ProductActor,
    ProductRole,
)
from domains.products.schemas import CatalogProposalPolicyContent
from infra.db.approval_uow import SqlAlchemyApprovalUnitOfWork
from infra.db.catalog_products_uow import SqlAlchemyCatalogProductsUnitOfWork
from infra.db.outbox import deserialize
from infra.db.tables import (
    ApprovalPackageRow,
    CatalogProposalPolicyVersionRow,
    OutboxEventRow,
)
from infra.db.workflow_engine import PostgresWorkflowEngine
from notification_gateway.jobs import NotificationJob
from shared.errors import TransientError
from shared.events.catalog import ApprovalDecided
from shared.schemas.identifiers import ApprovalId, EmployeeId, TenantId, new_id
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414 -- 每例隔离真实 PostgreSQL
)
from workflows.engine.runner import StepHandler, WorkflowRun

try:
    from workflows.catalog_product_proposal import (
        CatalogPolicyApprovalDecidedHandler,
        build_catalog_policy_workflow_definition,
        build_catalog_policy_workflow_handlers,
        catalog_policy_change_idempotency_key,
        policy_workflow_context,
    )
except (ImportError, ModuleNotFoundError):

    def _missing(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        pytest.fail("RED：Catalog Policy durable workflow 尚未实现")

    CatalogPolicyApprovalDecidedHandler = _missing
    build_catalog_policy_workflow_definition = _missing
    build_catalog_policy_workflow_handlers = _missing
    catalog_policy_change_idempotency_key = _missing
    policy_workflow_context = _missing


NOW = datetime(2026, 9, 5, 10, tzinfo=UTC)


class _Clock:
    def __init__(self) -> None:
        self.value = NOW

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


class _BossReader:
    def __init__(self, tenant: TenantId, boss: EmployeeId) -> None:
        self.tenant = tenant
        self.boss = boss

    async def read_actor(self, tenant_id, employee_id):
        return CatalogApprovalActorFact(
            tenant_id=tenant_id,
            employee_id=employee_id,
            current_role="boss",
            active=True,
            eligible=tenant_id == self.tenant and employee_id == self.boss,
        )


class _Audience:
    def __init__(self, tenant: TenantId, recipient: EmployeeId) -> None:
        self.member = NotificationAudienceMember(tenant, recipient)

    async def recipients_for(self, tenant_id, event):
        return (self.member,)


class _Jobs:
    def __init__(self) -> None:
        self.items: list[NotificationJob] = []

    async def enqueue(self, job: NotificationJob) -> bool:
        self.items.append(job)
        return True


class _LoseResponseOnce:
    """真实 delegate 已提交后只丢一次响应，测试重放修复而非 mock 行为。"""

    def __init__(self, delegate: StepHandler) -> None:
        self.delegate = delegate
        self.lost = False

    async def execute(self, run: WorkflowRun):
        result = await self.delegate.execute(run)
        if not self.lost:
            self.lost = True
            raise TransientError("sensitive database response was lost")
        return result


class _CountingProducts:
    """保留真实 Products 服务，只记录 durable recovery 穿过 apply 边界。"""

    def __init__(self, delegate: CatalogProposalServiceImpl) -> None:
        self._delegate = delegate
        self.apply_calls = 0

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def apply_policy_decision(self, *args, **kwargs):
        self.apply_calls += 1
        return await self._delegate.apply_policy_decision(*args, **kwargs)


class _RejectEngineUse:
    def __init__(self) -> None:
        self.deliveries: list[object] = []

    async def find_active_run(self, *args, **kwargs):
        raise AssertionError("非 Catalog 事件不得查询 Catalog workflow")

    async def deliver_event(self, *args, **kwargs):
        self.deliveries.append((args, kwargs))
        raise AssertionError("非 Catalog 事件不得投递 Catalog workflow")


def _content(accounts: int = 3) -> CatalogProposalPolicyContent:
    return CatalogProposalPolicyContent(
        minimum_distinct_accounts=accounts,
        minimum_recurring_accounts=None,
        minimum_distinct_countries=None,
        minimum_quantity_unit_accounts=None,
        require_unified_unit=False,
    )


async def _services(
    engine: AsyncEngine,
    *,
    clock: _Clock,
) -> tuple[
    TenantId,
    EmployeeId,
    EmployeeId,
    async_sessionmaker[AsyncSession],
    CatalogProposalServiceImpl,
    ApprovalServiceImpl,
    ProductActor,
]:
    tenant = TenantId(new_id("tn"))
    proposer = EmployeeId(new_id("emp"))
    boss = EmployeeId(new_id("emp"))
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO employees (employee_id,tenant_id,name,role) VALUES "
                "(:proposer,:tenant,'Catalog owner','product'),"
                "(:boss,:tenant,'Independent boss','boss')"
            ),
            {
                "tenant": str(tenant),
                "proposer": str(proposer),
                "boss": str(boss),
            },
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    products = CatalogProposalServiceImpl(
        lambda scoped: SqlAlchemyCatalogProductsUnitOfWork(factory, scoped),
        Phase2ProductAuthorizer(tenant),
        now=clock,
    )
    approvals = ApprovalServiceImpl(
        lambda scoped: SqlAlchemyApprovalUnitOfWork(factory, scoped, now=clock),
        catalog_actor_reader=_BossReader(tenant, boss),
        now=clock,
    )
    system = ProductActor("system:catalog-policy-workflow", ProductRole.SYSTEM, tenant)
    return tenant, proposer, boss, factory, products, approvals, system


async def _start(
    engine: AsyncEngine,
    *,
    clock: _Clock,
    lose_after: str | None = None,
):
    tenant, proposer, boss, factory, products, approvals, system = await _services(
        engine, clock=clock
    )
    policy_id = await products.create_policy_candidate(
        tenant,
        _content(),
        idempotency_key="catalog-policy-workflow-controlled",
        actor=ProductActor(str(proposer), ProductRole.PRODUCT, tenant),
    )
    snapshot = await products.get_policy_change_snapshot(
        tenant, policy_id, actor=system
    )
    counted_products = _CountingProducts(products)
    handlers = dict(
        build_catalog_policy_workflow_handlers(
            counted_products, approvals, system, now=clock
        )
    )
    if lose_after is not None:
        handlers[lose_after] = _LoseResponseOnce(handlers[lose_after])
    workflow = PostgresWorkflowEngine(factory, handlers, now=clock)
    workflow.register(build_catalog_policy_workflow_definition())
    run_id = await workflow.start(
        tenant,
        "catalog_proposal_policy_change",
        str(policy_id),
        policy_workflow_context(tenant, snapshot),
        catalog_policy_change_idempotency_key(tenant, policy_id),
    )
    created = await workflow.get_run(tenant, run_id)
    assert created is not None
    # Run创建时间沿PG时钟；后续固定时钟必须从该持久事实起算。
    clock.value = created.created_at
    return (
        tenant,
        proposer,
        boss,
        counted_products,
        approvals,
        workflow,
        run_id,
        policy_id,
    )


async def _pending(approvals: ApprovalServiceImpl, tenant: TenantId, boss: EmployeeId):
    views = await approvals.list_for_reader(
        tenant,
        reader=ApprovalReaderIdentity(employee_id=boss, role="boss"),
        limit=20,
    )
    matching = [
        item for item in views if item.approval_type == "catalog_proposal_policy_change"
    ]
    assert len(matching) == 1
    return matching[0]


@pytest.mark.asyncio
async def test_real_workflow_is_visible_in_central_queue_then_applies_and_projects_redacted_notification(
    unit_engine: AsyncEngine,
) -> None:
    """中央队列、事件 outbox 与既有通知投影必须形成同一真实路径。"""
    clock = _Clock()
    tenant, _, boss, products, approvals, workflow, run_id, policy_id = await _start(
        unit_engine, clock=clock
    )

    processed = await workflow.poll_due(tenant, 10)
    state = await workflow.get_run(tenant, run_id)
    assert processed == 3, (state.status, state.current_step, state.last_error)
    pending = await _pending(approvals, tenant, boss)
    assert pending.state == "pending"
    assert (
        pending.proposed_change_display["configuration_when_missing"] == "未配置即关闭"
    )
    assert pending.proposed_change_display["external_action"] == "无外部动作"

    await approvals.decide(tenant, ApprovalId(pending.approval_id), True, boss)
    async with unit_engine.connect() as connection:
        event_payload = (
            await connection.execute(
                select(OutboxEventRow.event_payload).where(
                    OutboxEventRow.tenant_id == str(tenant),
                    OutboxEventRow.event_type == "ApprovalDecided",
                )
            )
        ).scalar_one()
    event = deserialize(ApprovalDecided, event_payload)
    assert isinstance(event, ApprovalDecided)
    assert (event.approval_id, event.decision, event.decided_by) == (
        pending.approval_id,
        "approve",
        boss,
    )
    assert set(event_payload) == {
        "tenant_id",
        "occurred_at",
        "run_id",
        "approval_id",
        "decision",
        "decided_by",
    }
    assert not {
        "content",
        "minimum_distinct_accounts",
        "request_hash",
        "change_set_ref",
        "evidence_refs",
        "price",
        "probability",
        "exception",
    }.intersection(event_payload)
    jobs = _Jobs()
    await NotificationProjectionHandler(
        tenant_id=tenant,
        audience=_Audience(tenant, boss),
        jobs=jobs,
        now=clock,
    ).handle(event)
    assert len(jobs.items) == 1
    assert jobs.items[0].context.reason_code == "approved"
    assert jobs.items[0].context.primary_id == pending.approval_id

    await CatalogPolicyApprovalDecidedHandler(workflow, approvals).handle(event)
    assert await workflow.poll_due(tenant, 10) == 2
    active = await products.get_active_policy(
        tenant, actor=ProductActor(str(boss), ProductRole.BOSS, tenant)
    )
    assert active is not None and active.policy_version_id == policy_id
    fact = await approvals.read_catalog_fact(tenant, ApprovalId(pending.approval_id))
    assert fact.state is ApprovalState.APPLIED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lost_handler",
    [
        "catalog_product_policy.submit",
        "catalog_product_policy.apply",
        "catalog_product_policy.mark_applied",
    ],
)
async def test_response_loss_after_each_committed_boundary_converges_without_duplicates(
    unit_engine: AsyncEngine,
    lost_handler: str,
) -> None:
    """提交成功但响应丢失时必须读 canonical 状态修复，不能重复业务效果。"""
    clock = _Clock()
    tenant, _, boss, products, approvals, workflow, _, policy_id = await _start(
        unit_engine, clock=clock, lose_after=lost_handler
    )
    await workflow.poll_due(tenant, 10)
    if lost_handler == "catalog_product_policy.submit":
        clock.advance(timedelta(seconds=30))
        await workflow.poll_due(tenant, 10)
    pending = await _pending(approvals, tenant, boss)
    await approvals.decide(tenant, ApprovalId(pending.approval_id), True, boss)
    event = ApprovalDecided(tenant, clock(), None, pending.approval_id, "approve", boss)
    await CatalogPolicyApprovalDecidedHandler(workflow, approvals).handle(event)
    await workflow.poll_due(tenant, 10)
    clock.advance(timedelta(seconds=30))
    await workflow.poll_due(tenant, 10)

    async with unit_engine.connect() as connection:
        assert (
            await connection.scalar(
                select(func.count())
                .select_from(ApprovalPackageRow)
                .where(
                    ApprovalPackageRow.tenant_id == str(tenant),
                    ApprovalPackageRow.approval_type
                    == "catalog_proposal_policy_change",
                )
            )
            == 1
        )
        assert (
            await connection.scalar(
                select(func.count())
                .select_from(CatalogProposalPolicyVersionRow)
                .where(
                    CatalogProposalPolicyVersionRow.tenant_id == str(tenant),
                    CatalogProposalPolicyVersionRow.policy_version_id == str(policy_id),
                    CatalogProposalPolicyVersionRow.state == "active",
                )
            )
            == 1
        )
    fact = await approvals.read_catalog_fact(tenant, ApprovalId(pending.approval_id))
    assert fact.state is ApprovalState.APPLIED
    assert products.apply_calls == {
        "catalog_product_policy.submit": 2,
        "catalog_product_policy.apply": 3,
        "catalog_product_policy.mark_applied": 3,
    }[lost_handler]


@pytest.mark.asyncio
async def test_real_legacy_approval_event_is_ignored_before_strict_catalog_read(
    unit_engine: AsyncEngine,
) -> None:
    """真实中央 legacy 包不能被 Catalog strict reader 误判为坏事件。"""
    clock = _Clock()
    tenant, _, boss, _, _, approvals, _ = await _services(unit_engine, clock=clock)
    approval_id = await approvals.submit(
        tenant,
        ApprovalType.PLAYBOOK_CHANGE,
        "Legacy playbook approval",
        {"version": "legacy-v1"},
        "Controlled non-Catalog package",
        BlastRadius(
            affected_entities=["legacy playbook"],
            if_approved="Apply through its own workflow.",
            if_rejected="Keep the current playbook.",
            reversible=True,
        ),
        proposed_by_employee=None,
        owner_employee=None,
    )
    await approvals.decide(tenant, approval_id, True, boss)
    fact = await approvals.read_fact(tenant, approval_id)
    assert fact.approval_type == "playbook_change"
    assert fact.contract_namespace is None
    engine = _RejectEngineUse()

    await CatalogPolicyApprovalDecidedHandler(engine, approvals).handle(
        ApprovalDecided(tenant, clock(), None, str(approval_id), "approve", boss)
    )

    assert engine.deliveries == []


@pytest.mark.asyncio
async def test_rejection_and_timeout_are_durable_terminal_without_activation(
    unit_engine: AsyncEngine,
) -> None:
    """拒绝与七日超时都必须关闭候选，绝不能启用策略。"""
    for rejected in (True, False):
        clock = _Clock()
        tenant, _, boss, products, approvals, workflow, _, policy_id = await _start(
            unit_engine, clock=clock
        )
        assert await workflow.poll_due(tenant, 10) == 3
        pending = await _pending(approvals, tenant, boss)
        if rejected:
            await approvals.decide(
                tenant,
                ApprovalId(pending.approval_id),
                False,
                boss,
                note="controlled rejection",
            )
            await CatalogPolicyApprovalDecidedHandler(workflow, approvals).handle(
                ApprovalDecided(
                    tenant, clock(), None, pending.approval_id, "reject", boss
                )
            )
        else:
            clock.advance(timedelta(days=7, seconds=1))
        await workflow.poll_due(tenant, 10)

        policy = await products.get_policy_change_snapshot(
            tenant,
            policy_id,
            actor=ProductActor(
                "system:catalog-policy-workflow", ProductRole.SYSTEM, tenant
            ),
        )
        assert policy.candidate.state == ("rejected" if rejected else "expired")
        assert (
            await products.get_active_policy(
                tenant, actor=ProductActor(str(boss), ProductRole.BOSS, tenant)
            )
            is None
        )
