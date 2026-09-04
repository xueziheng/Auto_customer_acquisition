"""PostgreSQL catalog facts use one tenant-bound bidirectional membership snapshot."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from domains.demand.schemas import DemandCatalogAccountFact, catalog_evidence_summary
from domains.demand.service_impl import DemandServiceImpl
from infra.db.demand_uow import SqlAlchemyDemandUnitOfWork
from shared.schemas.identifiers import (
    EmployeeId,
    MessageId,
    NeedClusterId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
    new_id,
)
from shared.schemas.provenance import FactualField, Provenance, SourceType

_models = importlib.import_module("domains.demand.models")
NeedCluster = _models.NeedCluster
NeedStatus = _models.NeedStatus
ValidatedNeed = _models.ValidatedNeed

NOW = datetime(2026, 9, 4, 11, 0, tzinfo=UTC)
ACTOR = EmployeeId("emp_catalog_integration")


def _fact(value: object, source_id: str) -> FactualField[object]:
    provenance = Provenance(
        source_type=SourceType.CONVERSATION,
        source_id=source_id,
        extracted_by="human",
        extracted_at=NOW,
        confirmed_by=ACTOR,
        confirmed_at=NOW,
    )
    return FactualField(value, provenance)


def _need(
    tenant: TenantId,
    need_id: ValidatedNeedId,
    account_id: ProspectAccountId,
    quantity: int,
) -> ValidatedNeed:
    quantity_fact = _fact(quantity, f"msg_quantity_{need_id}")
    return ValidatedNeed(
        need_id=need_id,
        tenant_id=tenant,
        account_id=account_id,
        product_category=_fact("hinges", f"msg_category_{need_id}"),
        source_message_id=MessageId(f"msg_{need_id}"),
        created_at=NOW - timedelta(days=1),
        status=NeedStatus.VALIDATED,
        quantity=quantity_fact,  # type: ignore[arg-type]
        recurring_requirement=_fact(True, f"msg_recurring_{need_id}"),  # type: ignore[arg-type]
    )


class _Accounts:
    async def get_account_catalog_fact(
        self,
        tenant_id: TenantId,
        account_id: ProspectAccountId,
    ) -> DemandCatalogAccountFact:
        provenance = Provenance(
            source_type=SourceType.CONVERSATION,
            source_id=f"account_country_{account_id}",
            extracted_by="human",
            extracted_at=NOW,
            confirmed_by=ACTOR,
            confirmed_at=NOW,
        )
        return DemandCatalogAccountFact(
            tenant_id=tenant_id,
            account_id=account_id,
            country_code="US",
            country_evidence=catalog_evidence_summary(
                tenant_id=tenant_id,
                subject_id=str(account_id),
                field_name="country",
                value="US",
                provenance=provenance,
            ),
        )


async def test_catalog_facts_and_enumeration_are_tenant_bound_and_query_bounded(
    integration_engine: AsyncEngine,
) -> None:
    """Dropping tenant filters or restoring member N+1 queries leaks or scales poorly."""
    tenant = TenantId(new_id("tn"))
    other_tenant = TenantId(new_id("tn"))
    cluster_id = NeedClusterId(new_id("ncl"))
    first_id = ValidatedNeedId(new_id("vnd"))
    second_id = ValidatedNeedId(new_id("vnd"))
    unclustered_id = ValidatedNeedId(new_id("vnd"))
    account_id = ProspectAccountId(new_id("acc"))
    other_account_id = ProspectAccountId(new_id("acc"))
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    first = _need(tenant, first_id, account_id, 10)
    second = _need(tenant, second_id, other_account_id, 20)
    unclustered = _need(tenant, unclustered_id, account_id, 30)

    async with SqlAlchemyDemandUnitOfWork(factory, tenant, now=lambda: NOW) as uow:
        await uow.needs.add(first)
        await uow.needs.add(second)
        await uow.needs.add(unclustered)
    cluster = NeedCluster(
        cluster_id=cluster_id,
        tenant_id=tenant,
        category="hinges",
        member_need_ids=[second_id, first_id],
        created_at=NOW,
        updated_at=NOW + timedelta(hours=1),
    )
    async with SqlAlchemyDemandUnitOfWork(factory, tenant, now=lambda: NOW) as uow:
        await uow.clusters.add(cluster)
        first.cluster_id = cluster_id
        second.cluster_id = cluster_id
        await uow.needs.update(first)
        await uow.needs.update(second)

    service = DemandServiceImpl(
        lambda requested: SqlAlchemyDemandUnitOfWork(factory, requested, now=lambda: NOW),
        now=lambda: NOW + timedelta(days=10),
        catalog_accounts=_Accounts(),
    )
    statements: list[str] = []

    def _record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: object,
    ) -> None:
        if "need_clusters" in statement and statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(integration_engine.sync_engine, "before_cursor_execute", _record_statement)
    try:
        facts = await service.get_cluster_catalog_facts(tenant, cluster_id)
    finally:
        event.remove(integration_engine.sync_engine, "before_cursor_execute", _record_statement)

    assert facts.member_need_ids == tuple(sorted((first_id, second_id)))
    assert facts.safe_total_quantity is None
    assert len(statements) == 1
    assert await service.list_catalog_cluster_ids(tenant, limit=20) == (cluster_id,)
    assert await service.list_catalog_cluster_ids(other_tenant, limit=20) == ()
    assert await service.list_catalog_cluster_ids_for_account(
        tenant, account_id, limit=20
    ) == (cluster_id,)
