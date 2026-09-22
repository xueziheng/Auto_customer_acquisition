"""真实 PostgreSQL 验证目录产品培养的持久编排与中央审批路径。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from apps.scheduler_worker.notification_projection import (
    NotificationAudienceMember,
    NotificationProjectionHandler,
)
from domains.approvals.catalog_contract import CatalogApprovalActorFact
from domains.approvals.schemas import ApprovalReaderIdentity
from domains.approvals.service import ApprovalState
from domains.approvals.service_impl import ApprovalServiceImpl
from domains.demand.service import CatalogEvidenceSummary, NeedClusterCatalogFacts
from domains.products.catalog_service_impl import CatalogProposalServiceImpl
from domains.products.permissions import (
    Phase2ProductAuthorizer,
    ProductActor,
    ProductRole,
)
from domains.products.service import (
    CatalogClusterFactsInput,
    CatalogEvidenceSummaryInput,
)
from infra.db.approval_uow import SqlAlchemyApprovalUnitOfWork
from infra.db.catalog_products_uow import SqlAlchemyCatalogProductsUnitOfWork
from infra.db.outbox import deserialize
from infra.db.tables import (
    ApprovalPackageRow,
    CatalogCultivationCaseRow,
    CatalogProductProposalRow,
    CatalogProposalEvaluationRow,
    OutboxEventRow,
    ProductRow,
    SourcingCaseRow,
    WorkflowRunRow,
)
from infra.db.workflow_engine import PostgresWorkflowEngine
from notification_gateway.jobs import NotificationJob
from shared.errors import TransientError, ValidationError
from shared.events.catalog import (
    ApprovalDecided,
    CatalogProductProposalCreated,
    NeedClusterMembershipChanged,
)
from shared.schemas.identifiers import (
    ApprovalId,
    EmployeeId,
    RunId,
    TenantId,
    ValidatedNeedId,
    new_id,
)
from shared.schemas.provenance import SourceType
from tests.integration.test_catalog_proposal_service import (
    NOW,
    _active_policy,
    _facts,
)
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414 -- 每例隔离真实 PostgreSQL
)
from workflows.catalog_product_proposal import (
    CatalogCultivationApprovalDecidedHandler,
    CatalogProductApplication,
    build_catalog_evaluation_workflow_definition,
    build_catalog_evaluation_workflow_handlers,
    build_catalog_product_workflow_definition,
    build_catalog_product_workflow_handlers,
    catalog_cultivation_idempotency_key,
    cultivation_workflow_context,
)
from workflows.engine.runner import StepHandler, WorkflowRun


class _Clock:
    def __init__(self) -> None:
        self.value = datetime.now(UTC) + timedelta(seconds=1)

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


class _Demand:
    def __init__(self, facts: NeedClusterCatalogFacts) -> None:
        self.facts = facts

    async def get_cluster_catalog_facts(self, tenant_id, cluster_id):
        return self.facts


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
    """真实 delegate 已提交后只丢一次响应。"""

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
    def __init__(self, delegate: CatalogProposalServiceImpl) -> None:
        self._delegate = delegate
        self.apply_calls = 0

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def apply_cultivation_decision(self, *args, **kwargs):
        self.apply_calls += 1
        return await self._delegate.apply_cultivation_decision(*args, **kwargs)


def _routable_facts(values: dict[str, str]) -> CatalogClusterFactsInput:
    base = _facts(values)
    return base.model_copy(
        update={
            "evidence_summaries": (
                CatalogEvidenceSummaryInput(
                    source_type="conversation",
                    source_id=new_id("msg"),
                    extracted_by="employee",
                    confirmed_by=EmployeeId(values["owner"]),
                    confirmed_at=NOW - timedelta(hours=2),
                    observed_at=NOW - timedelta(hours=2),
                    content_hash="b" * 64,
                ),
            )
        }
    )


def _demand_facts(facts: CatalogClusterFactsInput) -> NeedClusterCatalogFacts:
    evidence = tuple(
        CatalogEvidenceSummary(
            source_type=SourceType(item.source_type),
            source_id=item.source_id,
            extracted_by=item.extracted_by,
            confirmed_by=item.confirmed_by,
            confirmed_at=item.confirmed_at,
            observed_at=item.observed_at,
            content_hash=item.content_hash,
        )
        for item in facts.evidence_summaries
    )
    return NeedClusterCatalogFacts(
        tenant_id=facts.tenant_id,
        cluster_id=facts.cluster_id,
        cluster_category=facts.cluster_category,
        member_need_ids=facts.member_need_ids,
        distinct_account_ids=facts.distinct_account_ids,
        member_count=facts.member_count,
        distinct_account_count=facts.distinct_account_count,
        known_country_codes=facts.known_country_codes,
        unknown_country_account_count=facts.unknown_country_account_count,
        recurring_true_account_count=facts.recurring_true_account_count,
        recurring_false_account_count=facts.recurring_false_account_count,
        recurring_unknown_account_count=facts.recurring_unknown_account_count,
        quantity_unit_covered_account_count=facts.quantity_unit_covered_account_count,
        unified_unit=facts.unified_unit,
        safe_total_quantity=facts.safe_total_quantity,
        evidence_summaries=evidence,
        display_codes=facts.display_codes,
        facts_observed_at=facts.facts_observed_at,
        facts_hash=facts.facts_hash,
    )


async def _seed_parents(
    engine: AsyncEngine, *, seed_evaluation_run: bool = True
) -> dict[str, str]:
    values = {
        "tenant": new_id("tn"),
        "owner": new_id("emp"),
        "reviewer": new_id("emp"),
        "cluster": new_id("ncl"),
        "run": new_id("run"),
        "other_run": new_id("run"),
        "message": new_id("msg"),
    }
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO employees (employee_id,tenant_id,name,role) VALUES "
                "(:owner,:tenant,'Catalog owner','product'),"
                "(:reviewer,:tenant,'Independent boss','boss')"
            ),
            values,
        )
        await connection.execute(
            text(
                "INSERT INTO need_clusters "
                "(tenant_id,cluster_id,category,keywords,countries,created_at,updated_at) "
                "VALUES (:tenant,:cluster,'three_wheelers','[]'::jsonb,"
                "'[]'::jsonb,:now,:now)"
            ),
            values | {"now": NOW},
        )
        if seed_evaluation_run:
            await connection.execute(
                text(
                    "INSERT INTO workflow_runs "
                    "(run_id,tenant_id,workflow_type,workflow_version,subject_ref,"
                    "current_step,status,context,idempotency_key) VALUES "
                    "(:run,:tenant,'catalog_cluster_evaluation',1,:cluster,'evaluate',"
                    "'running','{}'::jsonb,:key)"
                ),
                values | {"key": f"catalog-evaluation:seed:{values['run']}"},
            )
    return values


async def _start(
    engine: AsyncEngine,
    *,
    clock: _Clock,
    lose_after: str | None = None,
):
    values = await _seed_parents(engine)
    tenant = TenantId(values["tenant"])
    owner = EmployeeId(values["owner"])
    boss = EmployeeId(values["reviewer"])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    await _active_policy(engine, factory, values)
    products = CatalogProposalServiceImpl(
        lambda scoped: SqlAlchemyCatalogProductsUnitOfWork(factory, scoped),
        Phase2ProductAuthorizer(tenant),
        now=clock,
    )
    system = ProductActor("system:catalog-cultivation", ProductRole.SYSTEM, tenant)
    product_facts = _routable_facts(values)
    evaluation = await products.evaluate_cluster(
        tenant,
        product_facts,
        proposed_by_run=values["run"],
        actor=system,
    )
    proposals = await products.list_proposals(
        tenant,
        actor=ProductActor(str(owner), ProductRole.PRODUCT, tenant),
        limit=10,
    )
    proposal = proposals[0]
    policy = await products.get_active_policy(tenant, actor=system)
    assert policy is not None
    approvals = ApprovalServiceImpl(
        lambda scoped: SqlAlchemyApprovalUnitOfWork(factory, scoped, now=clock),
        catalog_actor_reader=_BossReader(tenant, boss),
        now=clock,
    )
    counted_products = _CountingProducts(products)
    handlers = dict(
        build_catalog_product_workflow_handlers(
            _Demand(_demand_facts(product_facts)),
            counted_products,
            approvals,
            system,
            now=clock,
        )
    )
    if lose_after is not None:
        handlers[lose_after] = _LoseResponseOnce(handlers[lose_after])
    workflow = PostgresWorkflowEngine(factory, handlers, now=clock)
    workflow.register(build_catalog_product_workflow_definition())
    context = cultivation_workflow_context(tenant, proposal, evaluation, policy)
    key = catalog_cultivation_idempotency_key(tenant, proposal.proposal_id)
    run_id = await workflow.start(
        tenant,
        "catalog_product_cultivation",
        str(proposal.proposal_id),
        context,
        key,
    )
    replay_id = await workflow.start(
        tenant,
        "catalog_product_cultivation",
        str(proposal.proposal_id),
        context,
        key,
    )
    assert replay_id == run_id
    return tenant, boss, approvals, counted_products, workflow, proposal, run_id


async def _pending(approvals: ApprovalServiceImpl, tenant: TenantId, boss: EmployeeId):
    views = await approvals.list_for_reader(
        tenant,
        reader=ApprovalReaderIdentity(employee_id=boss, role="boss"),
        limit=20,
    )
    matching = [
        item for item in views if item.approval_type == "catalog_product_cultivation"
    ]
    assert len(matching) == 1
    return matching[0]


async def _approve(
    engine: AsyncEngine,
    clock: _Clock,
    tenant: TenantId,
    boss: EmployeeId,
    approvals: ApprovalServiceImpl,
    workflow: PostgresWorkflowEngine,
):
    pending = await _pending(approvals, tenant, boss)
    clock.advance(timedelta(minutes=1))
    await approvals.decide(tenant, ApprovalId(pending.approval_id), True, boss)
    async with engine.connect() as connection:
        event_payload = (
            await connection.execute(
                select(OutboxEventRow.event_payload).where(
                    OutboxEventRow.tenant_id == str(tenant),
                    OutboxEventRow.event_type == "ApprovalDecided",
                )
            )
        ).scalar_one()
    event = deserialize(ApprovalDecided, event_payload)
    await CatalogCultivationApprovalDecidedHandler(workflow, approvals).handle(event)
    return pending, event, event_payload


@pytest.mark.asyncio
async def test_real_facts_event_drives_evaluation_outbox_and_cultivation_once(
    unit_engine: AsyncEngine,
) -> None:
    """真实事件链须保留 run 绑定、outbox 元数据与重复投递幂等。"""
    clock = _Clock()
    values = await _seed_parents(unit_engine, seed_evaluation_run=False)
    tenant = TenantId(values["tenant"])
    boss = EmployeeId(values["reviewer"])
    factory = async_sessionmaker(unit_engine, expire_on_commit=False)
    await _active_policy(unit_engine, factory, values)
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
    system = ProductActor("system:catalog-cultivation", ProductRole.SYSTEM, tenant)
    facts = _routable_facts(values)
    demand = _Demand(_demand_facts(facts))
    handlers = {
        **build_catalog_evaluation_workflow_handlers(demand, products, system),
        **build_catalog_product_workflow_handlers(
            demand, products, approvals, system, now=clock
        ),
    }
    workflow = PostgresWorkflowEngine(factory, handlers, now=clock)
    workflow.register(build_catalog_evaluation_workflow_definition())
    workflow.register(build_catalog_product_workflow_definition())
    application = CatalogProductApplication(demand, products, workflow, system)
    facts_event = NeedClusterMembershipChanged(
        tenant_id=tenant,
        occurred_at=clock(),
        cluster_id=facts.cluster_id,
        changed_need_id=ValidatedNeedId(new_id("need")),
        member_count=999,
    )

    evaluation_run = await application.handle_need_cluster_membership_changed(
        facts_event
    )
    duplicate_evaluation_run = (
        await application.handle_need_cluster_membership_changed(facts_event)
    )
    assert isinstance(evaluation_run, str)
    assert duplicate_evaluation_run == evaluation_run
    assert await workflow.poll_due(tenant, 10) == 1

    async with unit_engine.connect() as connection:
        evaluation_run_state = (
            await connection.execute(
                text(
                    "SELECT current_step,status,last_error FROM workflow_runs "
                    "WHERE tenant_id=:tenant AND run_id=:run"
                ),
                {"tenant": str(tenant), "run": str(evaluation_run)},
            )
        ).one()
        assert tuple(evaluation_run_state) == (
            "evaluate",
            "completed",
            None,
        )
        evaluation_row = (
            await connection.execute(
                select(
                    CatalogProposalEvaluationRow.proposed_by_run,
                    CatalogProposalEvaluationRow.evaluation_id,
                ).where(
                    CatalogProposalEvaluationRow.tenant_id == str(tenant)
                )
            )
        ).one()
        proposal_row = (
            await connection.execute(
                select(
                    CatalogProductProposalRow.proposed_by_run,
                    CatalogProductProposalRow.evaluation_id,
                ).where(
                    CatalogProductProposalRow.tenant_id == str(tenant)
                )
            )
        ).one()
        proposal_payload = (
            await connection.execute(
                select(OutboxEventRow.event_payload).where(
                    OutboxEventRow.tenant_id == str(tenant),
                    OutboxEventRow.event_type == "CatalogProductProposalCreated",
                )
            )
        ).scalar_one()
        evaluation_context = await connection.scalar(
            select(WorkflowRunRow.context).where(
                WorkflowRunRow.tenant_id == str(tenant),
                WorkflowRunRow.run_id == str(evaluation_run),
            )
        )
    assert evaluation_row.proposed_by_run == str(evaluation_run)
    assert proposal_row.proposed_by_run == str(evaluation_run)
    assert evaluation_row.evaluation_id == proposal_row.evaluation_id
    assert set(evaluation_context) == {
        "cluster_id",
        "policy_version_id",
        "policy_content_hash",
        "facts_hash",
        "evaluation_id",
        "evaluation_state",
    }
    assert set(proposal_payload) == {
        "tenant_id",
        "occurred_at",
        "run_id",
        "proposal_id",
        "evaluation_id",
        "cluster_id",
        "policy_version_id",
        "facts_hash",
    }
    proposal_event = deserialize(CatalogProductProposalCreated, proposal_payload)
    assert proposal_event.run_id == evaluation_run

    cultivation_run = await application.handle_catalog_product_proposal_created(
        proposal_event
    )
    duplicate_cultivation_run = (
        await application.handle_catalog_product_proposal_created(proposal_event)
    )
    assert isinstance(cultivation_run, str)
    assert duplicate_cultivation_run == cultivation_run
    assert await workflow.poll_due(tenant, 10) == 3
    pending = await _pending(approvals, tenant, boss)
    assert pending.state == "pending"

    async with unit_engine.connect() as connection:
        workflow_counts = dict(
            (
                await connection.execute(
                    select(WorkflowRunRow.workflow_type, func.count())
                    .where(WorkflowRunRow.tenant_id == str(tenant))
                    .group_by(WorkflowRunRow.workflow_type)
                )
            ).all()
        )
        cultivation_context = await connection.scalar(
            select(WorkflowRunRow.context).where(
                WorkflowRunRow.tenant_id == str(tenant),
                WorkflowRunRow.run_id == str(cultivation_run),
            )
        )
    assert workflow_counts == {
        "catalog_cluster_evaluation": 1,
        "catalog_product_cultivation": 1,
    }
    assert set(cultivation_context) == {
        "proposal_id",
        "evaluation_id",
        "cluster_id",
        "policy_version_id",
        "policy_content_hash",
        "facts_hash",
        "owner_employee",
        "proposed_by_run",
        "change_set_ref",
        "approval_id",
        "approval_timeout_seconds",
        "approval_state",
    }

    clock.value = pending.expires_at - timedelta(seconds=1)
    await approvals.decide(tenant, ApprovalId(pending.approval_id), True, boss)
    clock.value = pending.expires_at + timedelta(seconds=1)
    assert await workflow.poll_due(tenant, 1) == 1
    assert await workflow.poll_due(tenant, 10) == 1
    fact = await approvals.read_catalog_fact(tenant, ApprovalId(pending.approval_id))
    completed = await workflow.get_run(tenant, RunId(str(cultivation_run)))
    assert fact.state is ApprovalState.APPLIED
    assert completed.status.value == "completed"
    assert completed.context["application_state"] == "applied"


@pytest.mark.asyncio
async def test_real_cultivation_is_one_persisted_run_and_central_approval_notification_path(
    unit_engine: AsyncEngine,
) -> None:
    clock = _Clock()
    tenant, boss, approvals, products, workflow, proposal, run_id = await _start(
        unit_engine, clock=clock
    )

    assert await workflow.poll_due(tenant, 10) == 3
    pending = await _pending(approvals, tenant, boss)
    assert pending.state == "pending"
    async with unit_engine.connect() as connection:
        run_context = await connection.scalar(
            select(WorkflowRunRow.context).where(
                WorkflowRunRow.tenant_id == str(tenant),
                WorkflowRunRow.run_id == str(run_id),
            )
        )
        run_count = await connection.scalar(
            select(func.count())
            .select_from(WorkflowRunRow)
            .where(
                WorkflowRunRow.tenant_id == str(tenant),
                WorkflowRunRow.workflow_type == "catalog_product_cultivation",
            )
        )
    assert run_count == 1
    forbidden = {
        "rule_results",
        "evidence_refs",
        "policy_content",
        "facts",
        "price",
        "probability",
        "credential",
        "exception",
    }
    assert not forbidden.intersection(str(run_context))

    pending, event, event_payload = await _approve(
        unit_engine, clock, tenant, boss, approvals, workflow
    )
    assert set(event_payload) == {
        "tenant_id",
        "occurred_at",
        "run_id",
        "approval_id",
        "decision",
        "decided_by",
    }
    assert not forbidden.intersection(str(event_payload))
    jobs = _Jobs()
    await NotificationProjectionHandler(
        tenant_id=tenant,
        audience=_Audience(tenant, boss),
        jobs=jobs,
        now=clock,
    ).handle(event)
    assert len(jobs.items) == 1
    assert jobs.items[0].context.primary_id == pending.approval_id
    assert jobs.items[0].context.reason_code == "approved"

    assert await workflow.poll_due(tenant, 10) == 2
    fact = await approvals.read_catalog_fact(tenant, ApprovalId(pending.approval_id))
    assert fact.state is ApprovalState.APPLIED
    assert products.apply_calls == 2
    await CatalogCultivationApprovalDecidedHandler(workflow, approvals).handle(event)
    async with unit_engine.connect() as connection:
        counts = [
            await connection.scalar(
                select(func.count()).select_from(row).where(row.tenant_id == str(tenant))
            )
            for row in (
                ApprovalPackageRow,
                CatalogCultivationCaseRow,
                CatalogProductProposalRow,
            )
        ]
        external_counts = [
            await connection.scalar(
                select(func.count()).select_from(row).where(row.tenant_id == str(tenant))
            )
            for row in (ProductRow, SourcingCaseRow)
        ]
    assert counts == [2, 1, 1]
    assert external_counts == [0, 0]
    assert proposal.state == "awaiting_approval_submission"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "lost_handler",
    [
        "catalog_product_cultivation.submit",
        "catalog_product_cultivation.apply",
        "catalog_product_cultivation.mark_applied",
    ],
)
async def test_committed_response_loss_replays_products_before_repairing_receipt(
    unit_engine: AsyncEngine,
    lost_handler: str,
) -> None:
    clock = _Clock()
    tenant, boss, approvals, products, workflow, _, _ = await _start(
        unit_engine, clock=clock, lose_after=lost_handler
    )
    await workflow.poll_due(tenant, 10)
    if lost_handler == "catalog_product_cultivation.submit":
        clock.advance(timedelta(seconds=30))
        await workflow.poll_due(tenant, 10)
    pending, _, _ = await _approve(
        unit_engine, clock, tenant, boss, approvals, workflow
    )
    await workflow.poll_due(tenant, 10)
    clock.advance(timedelta(seconds=30))
    await workflow.poll_due(tenant, 10)

    fact = await approvals.read_catalog_fact(tenant, ApprovalId(pending.approval_id))
    assert fact.state is ApprovalState.APPLIED
    assert products.apply_calls >= 2
    async with unit_engine.connect() as connection:
        assert (
            await connection.scalar(
                select(func.count())
                .select_from(CatalogCultivationCaseRow)
                .where(CatalogCultivationCaseRow.tenant_id == str(tenant))
            )
            == 1
        )


@pytest.mark.asyncio
async def test_cross_tenant_demand_snapshot_fails_closed_without_products_write(
    unit_engine: AsyncEngine,
) -> None:
    clock = _Clock()
    tenant, _, _, _, workflow, _, run_id = await _start(unit_engine, clock=clock)
    handler = workflow._handlers["catalog_product_cultivation.assemble"]
    other = TenantId(new_id("tn"))
    handler._demand.facts = NeedClusterCatalogFacts(  # type: ignore[attr-defined]
        **{**handler._demand.facts.__dict__, "tenant_id": other}  # type: ignore[attr-defined]
    )

    with pytest.raises(ValidationError):
        await handler.execute(await workflow.get_run(tenant, run_id))
    async with unit_engine.connect() as connection:
        assert (
            await connection.scalar(
                select(func.count())
                .select_from(ApprovalPackageRow)
                .where(
                    ApprovalPackageRow.tenant_id == str(tenant),
                    ApprovalPackageRow.approval_type == "catalog_product_cultivation",
                )
            )
            == 0
        )
