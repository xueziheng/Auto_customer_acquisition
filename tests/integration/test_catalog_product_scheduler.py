"""目录产品调度的真实 PostgreSQL 游标契约。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from domains.approvals.service_impl import ApprovalServiceImpl
from domains.demand.service import (
    CatalogClusterCursor,
    CatalogClusterIdPage,
    CatalogClusterNotFoundError,
    CatalogClusterReconciliationItem,
)
from domains.demand.service_impl import DemandServiceImpl
from domains.products.catalog_rules import catalog_policy_content_hash
from domains.products.catalog_service_impl import CatalogProposalServiceImpl
from domains.products.permissions import Phase2ProductAuthorizer
from domains.products.service import (
    CatalogPolicyNotFoundError,
    CatalogPolicyReconciliationItem,
    CatalogPolicyReconciliationPage,
    CatalogProposalNotFoundError,
    CatalogProposalPolicyContent,
    CatalogProposalPolicyView,
    CatalogProposalReconciliationItem,
    CatalogProposalReconciliationPage,
    CatalogReconciliationCursor,
    ProductActor,
    ProductRole,
)
from infra.db.approval_uow import SqlAlchemyApprovalUnitOfWork
from infra.db.catalog_products_uow import SqlAlchemyCatalogProductsUnitOfWork
from infra.db.catalog_reconciliation_checkpoints import (
    PostgresCatalogReconciliationCheckpointStore,
)
from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
from infra.db.outbox import PostgresEventBus
from infra.db.outbox_delivery import OutboxDeliverer
from infra.db.repositories.need_clusters import NeedClusterRepositoryImpl
from infra.db.workflow_engine import PostgresWorkflowEngine
from shared.errors import TradeOSError, ValidationError
from shared.events.catalog import NeedClusterMembershipChanged
from shared.schemas.identifiers import NeedClusterId, TenantId, ValidatedNeedId, new_id
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414 -- 每例独立真实 PostgreSQL
)
from workflows.engine.runner import WorkflowDefinition

NOW = datetime(2026, 9, 5, 10, tzinfo=UTC)


async def _seed_catalog_rows(engine: AsyncEngine) -> dict[str, object]:
    tenant = new_id("tn")
    other_tenant = new_id("tn")
    employee = new_id("emp")
    created_at = NOW - timedelta(days=1)
    policy_ids = tuple(f"cpv_cursor_{index}" for index in (3, 1, 2))
    cluster_ids = tuple(f"ncl_cursor_{index}" for index in (3, 1, 2))
    proposal_ids = tuple(f"cpr_cursor_{index}" for index in (3, 1, 2))
    active_policy_id = "cpv_cursor_active"
    content = CatalogProposalPolicyContent(
        minimum_distinct_accounts=3,
        minimum_recurring_accounts=None,
        minimum_distinct_countries=None,
        minimum_quantity_unit_accounts=None,
        require_unified_unit=False,
    )
    content_json = json.dumps(content.model_dump(mode="json"))
    content_hash = catalog_policy_content_hash(content)

    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO employees (tenant_id,employee_id,name,role) "
                "VALUES (:tenant,:employee,'Cursor owner','product')"
            ),
            {"tenant": tenant, "employee": employee},
        )
        active_request_hash = "9" * 64
        active_payload = json.dumps(
            {
                "schema_version": "catalog-policy-v1",
                "tenant_id": tenant,
                "approval_type": "catalog_proposal_policy_change",
                "policy_version_id": active_policy_id,
                "content_hash": content_hash,
                "request_hash": active_request_hash,
            }
        )
        await connection.execute(
            text(
                "INSERT INTO approval_packages "
                "(tenant_id,approval_id,approval_type,title,proposed_change,reason,"
                "blast_radius,created_at,expires_at,state,proposed_by_employee,"
                "evidence_refs,change_set_ref,owner_employee,contract_namespace,"
                "request_hash,expires_at_limit,decided_at,decided_by) VALUES "
                "(:tenant,'apr_cursor_active','catalog_proposal_policy_change','Policy',"
                "CAST(:payload AS jsonb),'internal','{}'::jsonb,:created_at,:expires,"
                "'approved',:employee,'[]'::jsonb,:change_set,:employee,"
                "'catalog-policy-v1',:request_hash,:expires,:activated_at,:employee)"
            ),
            {
                "tenant": tenant,
                "payload": active_payload,
                "created_at": created_at,
                "expires": NOW + timedelta(days=7),
                "employee": employee,
                "change_set": f"catalog-policy:{active_policy_id}:{content_hash}",
                "request_hash": active_request_hash,
                "activated_at": created_at + timedelta(minutes=1),
            },
        )
        await connection.execute(
            text(
                "INSERT INTO catalog_proposal_policy_versions "
                "(tenant_id,policy_version_id,content,content_hash,base_active_version_id,"
                "proposed_by,creation_key,creation_request_hash,approval_id,state,"
                "created_at,activated_at,terminal_at) VALUES "
                "(:tenant,:policy,CAST(:content AS jsonb),:content_hash,NULL,:employee,"
                "'cursor-policy-active',:request_hash,NULL,'pending_approval',"
                ":created_at,NULL,NULL)"
            ),
            {
                "tenant": tenant,
                "policy": active_policy_id,
                "content": content_json,
                "content_hash": content_hash,
                "employee": employee,
                "request_hash": active_request_hash,
                "created_at": created_at,
            },
        )
        await connection.execute(
            text(
                "UPDATE catalog_proposal_policy_versions SET "
                "approval_id='apr_cursor_active' "
                "WHERE tenant_id=:tenant AND policy_version_id=:policy"
            ),
            {"tenant": tenant, "policy": active_policy_id},
        )
        await connection.execute(
            text(
                "UPDATE catalog_proposal_policy_versions SET state='active',"
                "activated_at=:activated_at "
                "WHERE tenant_id=:tenant AND policy_version_id=:policy"
            ),
            {
                "tenant": tenant,
                "policy": active_policy_id,
                "activated_at": created_at + timedelta(minutes=1),
            },
        )
        for index, policy_id in enumerate(policy_ids):
            await connection.execute(
                text(
                    "INSERT INTO catalog_proposal_policy_versions "
                    "(tenant_id,policy_version_id,content,content_hash,base_active_version_id,"
                    "proposed_by,creation_key,creation_request_hash,approval_id,state,"
                    "created_at,activated_at,terminal_at) VALUES "
                    "(:tenant,:policy,CAST(:content AS jsonb),:content_hash,NULL,:employee,"
                    ":creation_key,:request_hash,NULL,'pending_approval',:created_at,NULL,NULL)"
                ),
                {
                    "tenant": tenant,
                    "policy": policy_id,
                    "content": content_json,
                    "content_hash": content_hash,
                    "employee": employee,
                    "creation_key": f"cursor-policy-{index}",
                    "request_hash": f"{index + 4}" * 64,
                    "created_at": created_at,
                },
            )
        for index, cluster_id in enumerate(cluster_ids):
            run_id = f"run_cursor_{index}"
            evaluation_id = f"cpe_cursor_{index}"
            proposal_id = proposal_ids[index]
            await connection.execute(
                text(
                    "INSERT INTO need_clusters "
                    "(tenant_id,cluster_id,category,keywords,countries,created_at,updated_at) "
                    "VALUES (:tenant,:cluster,'catalog','[]'::jsonb,'[]'::jsonb,"
                    ":created_at,:updated_at)"
                ),
                {
                    "tenant": tenant,
                    "cluster": cluster_id,
                    "created_at": created_at,
                    "updated_at": NOW + timedelta(minutes=index),
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO workflow_runs "
                    "(run_id,tenant_id,workflow_type,workflow_version,subject_ref,"
                    "current_step,status,context,idempotency_key) VALUES "
                    "(:run,:tenant,'catalog_cluster_evaluation',1,:cluster,'evaluate',"
                    "'running','{}'::jsonb,:key)"
                ),
                {
                    "run": run_id,
                    "tenant": tenant,
                    "cluster": cluster_id,
                    "key": f"cursor-run-{index}",
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO catalog_proposal_evaluations "
                    "(tenant_id,evaluation_id,cluster_id,policy_version_id,facts_hash,"
                    "facts,rule_results,overall_passed,blocked_reason,proposed_by_run,created_at) "
                    "VALUES (:tenant,:evaluation,:cluster,:policy,:facts_hash,"
                    "'{}'::jsonb,'[]'::jsonb,true,NULL,:run,:created_at)"
                ),
                {
                    "tenant": tenant,
                    "evaluation": evaluation_id,
                    "cluster": cluster_id,
                    "policy": active_policy_id,
                    "facts_hash": f"{index + 1}" * 64,
                    "run": run_id,
                    "created_at": created_at,
                },
            )
            await connection.execute(
                text(
                    "INSERT INTO catalog_product_proposals "
                    "(tenant_id,proposal_id,evaluation_id,cluster_id,policy_version_id,"
                    "facts_hash,owner_employee,proposed_by_run,approval_id,"
                    "approval_request_hash,state,created_at,updated_at) VALUES "
                    "(:tenant,:proposal,:evaluation,:cluster,:policy,:facts_hash,"
                    ":employee,:run,NULL,NULL,'awaiting_approval_submission',"
                    ":created_at,:created_at)"
                ),
                {
                    "tenant": tenant,
                    "proposal": proposal_id,
                    "evaluation": evaluation_id,
                    "cluster": cluster_id,
                    "policy": active_policy_id,
                    "facts_hash": f"{index + 1}" * 64,
                    "employee": employee,
                    "run": run_id,
                    "created_at": created_at,
                },
            )
        await connection.execute(
            text(
                "INSERT INTO need_clusters "
                "(tenant_id,cluster_id,category,keywords,countries,created_at,updated_at) "
                "VALUES (:tenant,'ncl_other_tenant','catalog','[]'::jsonb,'[]'::jsonb,"
                ":created_at,:created_at)"
            ),
            {"tenant": other_tenant, "created_at": created_at},
        )
    return {
        "tenant": TenantId(tenant),
        "other_tenant": TenantId(other_tenant),
        "employee": employee,
        "created_at": created_at,
        "policy_ids": tuple(sorted(policy_ids)),
        "cluster_ids": tuple(sorted(cluster_ids)),
        "proposal_ids": tuple(sorted(proposal_ids)),
    }


@pytest.mark.asyncio
async def test_public_product_reconciliation_pages_are_stable_and_fail_closed(
    unit_engine: AsyncEngine,
) -> None:
    seeded = await _seed_catalog_rows(unit_engine)
    tenant = cast(TenantId, seeded["tenant"])
    factory = async_sessionmaker(unit_engine, expire_on_commit=False)
    service = CatalogProposalServiceImpl(
        lambda scoped: SqlAlchemyCatalogProductsUnitOfWork(factory, scoped),
        Phase2ProductAuthorizer(tenant),
        now=lambda: NOW,
    )
    actor = ProductActor("system:catalog-scheduler", ProductRole.SYSTEM, tenant)

    policies_one = await service.list_pending_policy_reconciliation(
        tenant, actor=actor, limit=2
    )
    policies_two = await service.list_pending_policy_reconciliation(
        tenant, actor=actor, limit=2, cursor=policies_one.next_cursor
    )
    assert (
        tuple(str(item.policy_version_id) for item in policies_one.items)
        == seeded["policy_ids"][:2]
    )
    assert (
        tuple(str(item.policy_version_id) for item in policies_two.items)
        == seeded["policy_ids"][2:]
    )
    assert policies_one.next_cursor is not None
    assert policies_two.next_cursor is None

    proposals_one = await service.list_awaiting_proposal_reconciliation(
        tenant, actor=actor, limit=2
    )
    proposals_two = await service.list_awaiting_proposal_reconciliation(
        tenant, actor=actor, limit=2, cursor=proposals_one.next_cursor
    )
    assert (
        tuple(str(item.proposal_id) for item in proposals_one.items)
        == seeded["proposal_ids"][:2]
    )
    assert (
        tuple(str(item.proposal_id) for item in proposals_two.items)
        == seeded["proposal_ids"][2:]
    )
    assert proposals_one.next_cursor is not None
    assert proposals_two.next_cursor is None

    wrong_tenant_cursor = CatalogReconciliationCursor(
        tenant_id=seeded["other_tenant"],
        stream="pending_policies",
        position_at=seeded["created_at"],
        entity_id=seeded["policy_ids"][0],
    )
    wrong_stream_cursor = CatalogReconciliationCursor(
        tenant_id=tenant,
        stream="awaiting_proposals",
        position_at=seeded["created_at"],
        entity_id=seeded["proposal_ids"][0],
    )
    with pytest.raises(TradeOSError):
        await service.list_pending_policy_reconciliation(
            tenant, actor=actor, limit=2, cursor=wrong_tenant_cursor
        )
    with pytest.raises(TradeOSError):
        await service.list_pending_policy_reconciliation(
            tenant, actor=actor, limit=2, cursor=wrong_stream_cursor
        )


@pytest.mark.asyncio
async def test_demand_cluster_page_uses_created_at_id_keyset_and_wraps(
    unit_engine: AsyncEngine,
) -> None:
    seeded = await _seed_catalog_rows(unit_engine)
    tenant = cast(TenantId, seeded["tenant"])
    factory = async_sessionmaker(unit_engine, expire_on_commit=False)
    service = DemandServiceImpl(
        lambda scoped: SqlAlchemyDemandUnitOfWork(factory, scoped, now=lambda: NOW),
        now=lambda: NOW,
    )

    first = await service.list_catalog_cluster_id_page(tenant, limit=2)
    second = await service.list_catalog_cluster_id_page(
        tenant, limit=2, cursor=first.next_cursor
    )
    wrapped = await service.list_catalog_cluster_id_page(tenant, limit=2, cursor=None)
    legacy = await service.list_catalog_cluster_ids(tenant, limit=3)
    assert tuple(str(item.cluster_id) for item in first.items) == seeded[
        "cluster_ids"
    ][:2]
    assert tuple(str(item.cluster_id) for item in second.items) == seeded[
        "cluster_ids"
    ][2:]
    assert first.next_cursor is not None
    assert second.next_cursor is None
    assert wrapped.items == first.items
    assert tuple(map(str, legacy)) == (
        "ncl_cursor_2",
        "ncl_cursor_1",
        "ncl_cursor_3",
    )

    assert first.next_cursor is not None
    wrong_tenant_cursor = first.next_cursor.model_copy(
        update={"tenant_id": seeded["other_tenant"]}
    )
    with pytest.raises(TradeOSError):
        await service.list_catalog_cluster_id_page(
            tenant, limit=2, cursor=wrong_tenant_cursor
        )
    wrong_stream_cursor = first.next_cursor.model_copy(
        update={"stream": "pending_policies"}
    )
    with pytest.raises(TradeOSError):
        await service.list_catalog_cluster_id_page(
            tenant, limit=2, cursor=wrong_stream_cursor
        )
    with pytest.raises(CatalogClusterNotFoundError):
        await service.get_cluster_catalog_facts(
            tenant,
            NeedClusterId(new_id("ncl")),
        )


@pytest.mark.asyncio
async def test_demand_repository_rejects_wrong_stream_before_sql() -> None:
    tenant = TenantId(new_id("tn"))

    class NoSqlSession:
        async def execute(self, *args, **kwargs):
            del args, kwargs
            raise AssertionError("wrong-stream cursor 不得到达 SQL")

    cursor = CatalogClusterCursor(
        tenant_id=tenant,
        stream="catalog_clusters",
        created_at=NOW,
        cluster_id=NeedClusterId(new_id("ncl")),
    ).model_copy(update={"stream": "pending_policies"})
    repository = NeedClusterRepositoryImpl(cast(object, NoSqlSession()), tenant)

    with pytest.raises(ValidationError, match="stream"):
        await repository.list_catalog_cluster_id_page(
            tenant,
            limit=2,
            cursor=cursor,
        )


@pytest.mark.asyncio
async def test_real_pg_checkpoints_traverse_all_streams_with_fresh_driver_each_cycle(
    unit_engine: AsyncEngine,
) -> None:
    from apps.scheduler_worker.catalog_products import CatalogProductDriver

    tenant = TenantId(new_id("tn"))
    owner = new_id("emp")
    policy_ids = tuple(sorted(new_id("cpv") for _ in range(3)))
    proposal_ids = tuple(sorted(new_id("cpr") for _ in range(3)))
    cluster_ids = tuple(sorted(new_id("ncl") for _ in range(3)))
    content = CatalogProposalPolicyContent(
        minimum_distinct_accounts=3,
        minimum_recurring_accounts=None,
        minimum_distinct_countries=None,
        minimum_quantity_unit_accounts=None,
        require_unified_unit=False,
    )
    active = CatalogProposalPolicyView(
        policy_version_id=policy_ids[0],
        content=content,
        content_hash=catalog_policy_content_hash(content),
        base_active_version_id=None,
        proposed_by=owner,
        approval_id=None,
        state="active",
        created_at=NOW,
        activated_at=NOW,
        terminal_at=None,
    )

    class Products:
        def __init__(self) -> None:
            self.policy_cursors: list[CatalogReconciliationCursor | None] = []
            self.proposal_cursors: list[CatalogReconciliationCursor | None] = []

        @staticmethod
        def offset(
            values: tuple[str, ...],
            cursor: CatalogReconciliationCursor | None,
        ) -> int:
            if cursor is None:
                return 0
            entity_id = str(cursor.entity_id)
            return values.index(entity_id) + 1

        async def list_pending_policy_reconciliation(
            self, tenant_id, *, actor, limit, cursor=None
        ):
            assert tenant_id == actor.tenant_id == tenant
            self.policy_cursors.append(cursor)
            offset = self.offset(policy_ids, cursor)
            selected = policy_ids[offset : offset + limit]
            next_cursor = (
                CatalogReconciliationCursor(
                    tenant_id=tenant,
                    stream="pending_policies",
                    position_at=NOW,
                    entity_id=selected[-1],
                )
                if offset + limit < len(policy_ids)
                else None
            )
            return CatalogPolicyReconciliationPage(
                tenant_id=tenant,
                items=tuple(
                    CatalogPolicyReconciliationItem(
                        policy_version_id=value, created_at=NOW
                    )
                    for value in selected
                ),
                next_cursor=next_cursor,
            )

        async def list_awaiting_proposal_reconciliation(
            self, tenant_id, *, actor, limit, cursor=None
        ):
            assert tenant_id == actor.tenant_id == tenant
            self.proposal_cursors.append(cursor)
            offset = self.offset(proposal_ids, cursor)
            selected = proposal_ids[offset : offset + limit]
            next_cursor = (
                CatalogReconciliationCursor(
                    tenant_id=tenant,
                    stream="awaiting_proposals",
                    position_at=NOW,
                    entity_id=selected[-1],
                )
                if offset + limit < len(proposal_ids)
                else None
            )
            return CatalogProposalReconciliationPage(
                tenant_id=tenant,
                items=tuple(
                    CatalogProposalReconciliationItem(
                        proposal_id=value, created_at=NOW
                    )
                    for value in selected
                ),
                next_cursor=next_cursor,
            )

        async def get_policy_change_snapshot(self, *args, **kwargs):
            del args, kwargs
            raise CatalogPolicyNotFoundError("stale")

        async def get_proposal(self, *args, **kwargs):
            del args, kwargs
            raise CatalogProposalNotFoundError("stale")

        async def get_evaluation(self, *args, **kwargs):
            del args, kwargs
            raise AssertionError("stale proposal 不应读取 evaluation")

        async def get_active_policy(self, tenant_id, *, actor):
            assert tenant_id == actor.tenant_id == tenant
            return active

    class Demand:
        def __init__(self) -> None:
            self.cursors: list[CatalogClusterCursor | None] = []

        async def list_catalog_cluster_id_page(
            self, tenant_id, *, limit, cursor=None
        ):
            assert tenant_id == tenant
            self.cursors.append(cursor)
            offset = 0 if cursor is None else cluster_ids.index(str(cursor.cluster_id)) + 1
            selected = cluster_ids[offset : offset + limit]
            next_cursor = (
                CatalogClusterCursor(
                    tenant_id=tenant,
                    stream="catalog_clusters",
                    created_at=NOW,
                    cluster_id=selected[-1],
                )
                if offset + limit < len(cluster_ids)
                else None
            )
            return CatalogClusterIdPage(
                tenant_id=tenant,
                items=tuple(
                    CatalogClusterReconciliationItem(
                        cluster_id=value, created_at=NOW
                    )
                    for value in selected
                ),
                next_cursor=next_cursor,
            )

        async def get_cluster_catalog_facts(self, *args, **kwargs):
            del args, kwargs
            raise CatalogClusterNotFoundError("stale")

    class Engine:
        async def start(self, *args, **kwargs):
            del args, kwargs
            raise AssertionError("已知 stale item 不应启动 workflow")

    products = Products()
    demand = Demand()
    factory = async_sessionmaker(unit_engine, expire_on_commit=False)
    checkpoints = PostgresCatalogReconciliationCheckpointStore(factory, tenant)
    actor = ProductActor("system:catalog-scheduler", ProductRole.SYSTEM, tenant)
    for _ in range(4):
        await CatalogProductDriver(
            demand=cast(object, demand),
            products=cast(object, products),
            engine=cast(object, Engine()),
            checkpoints=checkpoints,
            system_actor=actor,
            tenant_id=tenant,
            batch_limit=2,
        ).scan_once()

    assert [None if value is None else value.entity_id for value in products.policy_cursors] == [
        None,
        policy_ids[1],
        None,
        policy_ids[1],
    ]
    assert [None if value is None else value.entity_id for value in products.proposal_cursors] == [
        None,
        proposal_ids[1],
        None,
        proposal_ids[1],
    ]
    assert [None if value is None else str(value.cluster_id) for value in demand.cursors] == [
        None,
        cluster_ids[1],
        None,
        cluster_ids[1],
    ]
    for stream in ("pending_policies", "awaiting_proposals", "catalog_clusters"):
        checkpoint = await checkpoints.load(tenant, stream)
        assert (checkpoint.version, checkpoint.position_at, checkpoint.entity_id) == (
            4,
            None,
            None,
        )


class _CatalogRegistryEngine:
    def __init__(self) -> None:
        self.definitions: list[WorkflowDefinition] = []

    def register(self, definition: WorkflowDefinition) -> None:
        self.definitions.append(definition)

    async def start(self, *args: object) -> str:
        del args
        return new_id("run")


class _CatalogOutboxRegistry:
    def __init__(self) -> None:
        self.handlers: list[tuple[type[object], str, object]] = []

    def register_handler(
        self, event_type: type[object], handler_name: str, handler: object
    ) -> None:
        self.handlers.append((event_type, handler_name, handler))


class _CatalogProspecting:
    async def get_account(self, tenant_id: object, account_id: object) -> object:
        del tenant_id, account_id
        raise AssertionError("composition 不得在注册时读取企业事实")


class _CatalogApprovals:
    async def read_fact(self, *args: object) -> object:
        del args
        raise AssertionError("composition 不得在注册时读取审批事实")


@pytest.mark.asyncio
async def test_catalog_composition_registers_exact_handlers_and_coexists_with_sourcing(
    unit_engine: AsyncEngine,
) -> None:
    from apps.scheduler_worker.catalog_product_runtime import (
        build_catalog_product_composition,
    )
    from shared.events.catalog import NeedClusterMembershipChanged

    tenant = TenantId(new_id("tn"))
    factory = async_sessionmaker(unit_engine, expire_on_commit=False)
    composition = build_catalog_product_composition(
        factory=factory,
        prospecting=cast(object, _CatalogProspecting()),
        approvals=cast(object, _CatalogApprovals()),
        tenant_id=tenant,
        now=lambda: NOW,
    )
    assert set(composition.handlers) == {
        "catalog_product_policy.assemble",
        "catalog_product_policy.submit",
        "catalog_product_policy.wait",
        "catalog_product_policy.apply",
        "catalog_product_policy.expire",
        "catalog_product_policy.mark_applied",
        "catalog_product_evaluation.evaluate",
        "catalog_product_cultivation.assemble",
        "catalog_product_cultivation.submit",
        "catalog_product_cultivation.wait",
        "catalog_product_cultivation.apply",
        "catalog_product_cultivation.expire",
        "catalog_product_cultivation.mark_applied",
    }
    engine = _CatalogRegistryEngine()
    outbox = _CatalogOutboxRegistry()
    outbox.register_handler(
        NeedClusterMembershipChanged,
        "sourcing_case.cluster_membership",
        object(),
    )

    runtime = composition.bind(engine=engine, outbox=outbox, batch_limit=2)

    assert callable(runtime.driver.scan_once)
    assert {definition.workflow_type for definition in engine.definitions} == {
        "catalog_proposal_policy_change",
        "catalog_cluster_evaluation",
        "catalog_product_cultivation",
    }
    handler_names = [name for _event, name, _handler in outbox.handlers]
    assert set(handler_names) == {
        "sourcing_case.cluster_membership",
        "catalog_products.need_cluster_membership_changed",
        "catalog_products.need_catalog_facts_changed",
        "catalog_products.account_country_facts_changed",
        "catalog_products.policy_activated",
        "catalog_products.proposal_created",
        "catalog_product_policy.approval_decided",
        "catalog_product_cultivation.approval_decided",
    }
    assert (
        sum(
            event is NeedClusterMembershipChanged
            for event, _name, _handler in outbox.handlers
        )
        == 2
    )


async def _seed_pending_policy_recovery_rows(
    engine: AsyncEngine,
) -> tuple[TenantId, TenantId, tuple[str, ...]]:
    tenant = TenantId(new_id("tn"))
    other_tenant = TenantId(new_id("tn"))
    owner = new_id("emp")
    other_owner = new_id("emp")
    content = CatalogProposalPolicyContent(
        minimum_distinct_accounts=3,
        minimum_recurring_accounts=None,
        minimum_distinct_countries=None,
        minimum_quantity_unit_accounts=None,
        require_unified_unit=False,
    )
    policy_ids = tuple(f"cpv_recovery_{index}" for index in (3, 1, 2))
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO employees (tenant_id,employee_id,name,role) VALUES "
                "(:tenant,:owner,'Recovery owner','product'),"
                "(:other_tenant,:other_owner,'Other owner','product')"
            ),
            {
                "tenant": str(tenant),
                "owner": owner,
                "other_tenant": str(other_tenant),
                "other_owner": other_owner,
            },
        )
        for index, policy_id in enumerate(policy_ids):
            await connection.execute(
                text(
                    "INSERT INTO catalog_proposal_policy_versions "
                    "(tenant_id,policy_version_id,content,content_hash,base_active_version_id,"
                    "proposed_by,creation_key,creation_request_hash,approval_id,state,"
                    "created_at,activated_at,terminal_at) VALUES "
                    "(:tenant,:policy,CAST(:content AS jsonb),:content_hash,NULL,:owner,"
                    ":creation_key,:request_hash,NULL,'pending_approval',:created_at,NULL,NULL)"
                ),
                {
                    "tenant": str(tenant),
                    "policy": policy_id,
                    "content": json.dumps(content.model_dump(mode="json")),
                    "content_hash": catalog_policy_content_hash(content),
                    "owner": owner,
                    "creation_key": f"recover-{index}",
                    "request_hash": f"{index + 1}" * 64,
                    "created_at": NOW,
                },
            )
        await connection.execute(
            text(
                "INSERT INTO catalog_proposal_policy_versions "
                "(tenant_id,policy_version_id,content,content_hash,base_active_version_id,"
                "proposed_by,creation_key,creation_request_hash,approval_id,state,"
                "created_at,activated_at,terminal_at) VALUES "
                "(:tenant,'cpv_recovery_other',CAST(:content AS jsonb),:content_hash,NULL,"
                ":owner,'recover-other',:request_hash,NULL,'pending_approval',:created_at,NULL,NULL)"
            ),
            {
                "tenant": str(other_tenant),
                "content": json.dumps(content.model_dump(mode="json")),
                "content_hash": catalog_policy_content_hash(content),
                "owner": other_owner,
                "request_hash": "8" * 64,
                "created_at": NOW,
            },
        )
    return tenant, other_tenant, tuple(sorted(policy_ids))


@pytest.mark.asyncio
async def test_real_scheduler_recovers_committed_policy_pages_across_reconstructed_drivers(
    unit_engine: AsyncEngine,
) -> None:
    from apps.scheduler_worker.catalog_product_runtime import (
        build_catalog_product_composition,
    )
    from apps.scheduler_worker.catalog_products import CatalogProductDriver

    tenant, other_tenant, policy_ids = await _seed_pending_policy_recovery_rows(
        unit_engine
    )
    factory = async_sessionmaker(unit_engine, expire_on_commit=False)
    approvals = ApprovalServiceImpl(
        lambda scoped: SqlAlchemyApprovalUnitOfWork(factory, scoped, now=lambda: NOW),
        now=lambda: NOW,
    )
    composition = build_catalog_product_composition(
        factory=factory,
        prospecting=cast(object, _CatalogProspecting()),
        approvals=approvals,
        tenant_id=tenant,
        now=lambda: NOW,
    )
    workflow = PostgresWorkflowEngine(factory, composition.handlers, now=lambda: NOW)
    outbox = OutboxDeliverer(factory, tenant, now=lambda: NOW)
    composition.bind(engine=workflow, outbox=outbox, batch_limit=2)

    async def scan_with_fresh_driver():
        return await CatalogProductDriver(
            demand=composition.demand,
            products=composition.products,
            engine=workflow,
            checkpoints=composition.checkpoints,
            system_actor=composition.system_actor,
            tenant_id=tenant,
            batch_limit=2,
        ).scan_once()

    first = await scan_with_fresh_driver()
    second = await scan_with_fresh_driver()
    replay_first = await scan_with_fresh_driver()
    replay_second = await scan_with_fresh_driver()
    assert (
        first.started_policy_runs,
        second.started_policy_runs,
        replay_first.started_policy_runs,
        replay_second.started_policy_runs,
    ) == (2, 1, 2, 1)

    async with unit_engine.connect() as connection:
        runs = (
            await connection.execute(
                text(
                    "SELECT subject_ref,idempotency_key,context FROM workflow_runs "
                    "WHERE tenant_id=:tenant AND workflow_type='catalog_proposal_policy_change' "
                    "ORDER BY subject_ref"
                ),
                {"tenant": str(tenant)},
            )
        ).all()
        other_runs = (
            await connection.execute(
                text("SELECT count(*) FROM workflow_runs WHERE tenant_id=:tenant"),
                {"tenant": str(other_tenant)},
            )
        ).scalar_one()
        product_count = (
            await connection.execute(
                text("SELECT count(*) FROM products WHERE tenant_id=:tenant"),
                {"tenant": str(tenant)},
            )
        ).scalar_one()
        sourcing_count = (
            await connection.execute(
                text("SELECT count(*) FROM sourcing_cases WHERE tenant_id=:tenant"),
                {"tenant": str(tenant)},
            )
        ).scalar_one()
        forbidden_counts = tuple(
            int(value)
            for value in (
                await connection.execute(
                    text(
                        "SELECT (SELECT count(*) FROM prospect_contacts WHERE tenant_id=:tenant),"
                        "(SELECT count(*) FROM suppliers WHERE tenant_id=:tenant),"
                        "(SELECT count(*) FROM outreach_message_attempts WHERE tenant_id=:tenant),"
                        "(SELECT count(*) FROM costing_quote_bases WHERE tenant_id=:tenant)"
                    ),
                    {"tenant": str(tenant)},
                )
            ).one()
        )
    assert tuple(row.subject_ref for row in runs) == policy_ids
    assert tuple(row.idempotency_key for row in runs) == tuple(
        f"catalog-policy-change:{tenant}:{policy_id}" for policy_id in policy_ids
    )
    assert all(row.context["policy_version_id"] == row.subject_ref for row in runs)
    assert other_runs == product_count == sourcing_count == 0
    assert forbidden_counts == (0, 0, 0, 0)

    duplicate = NeedClusterMembershipChanged(
        tenant_id=tenant,
        occurred_at=NOW,
        cluster_id=NeedClusterId("ncl_recovery_missing"),
        changed_need_id=ValidatedNeedId("vnd_recovery_missing"),
        member_count=1,
    )
    session = factory()
    try:
        bus = PostgresEventBus(session, tenant, now=lambda: NOW)
        await bus.publish(duplicate)
        await bus.publish(duplicate)
        await session.commit()
    finally:
        await session.close()
    assert await outbox.drain() == 2
    async with unit_engine.connect() as connection:
        statuses = (
            (
                await connection.execute(
                    text(
                        "SELECT status FROM outbox_events WHERE tenant_id=:tenant "
                        "AND event_type='NeedClusterMembershipChanged' ORDER BY event_id"
                    ),
                    {"tenant": str(tenant)},
                )
            )
            .scalars()
            .all()
        )
    assert statuses == ["delivered", "delivered"]
