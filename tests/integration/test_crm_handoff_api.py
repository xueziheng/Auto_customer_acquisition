"""S3-15 handoff API proof through one real-Postgres end-to-end scenario."""

from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.api.dependencies import ConfiguredApiDependencies
from apps.api.main import ApiSettings, create_app
from domains.employees import models as employee_models
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.service_impl import EmployeeServiceImpl
from domains.opportunities import models as opportunity_models
from domains.opportunities.permissions import (
    Actor as OpportunityActor,
)
from domains.opportunities.permissions import (
    OpportunityAction,
    OpportunityScope,
    ScopeLevel,
)
from domains.opportunities.scorer import OpportunityScorerImpl
from domains.opportunities.scoring import ScoringPolicy
from domains.opportunities.service_impl import HandoffPolicy, OpportunityServiceImpl
from infra.db.repositories.employees import (
    EmployeeRepositoryImpl,
    OwnershipRepositoryImpl,
    TerritoryRepositoryImpl,
)
from infra.db.repositories.opportunities import (
    HandoffRepositoryImpl,
    LossRecordRepositoryImpl,
    OpportunityRepositoryImpl,
)
from infra.db.session import create_engine_from
from infra.db.tables import OutboxEventRow
from infra.db.unit_of_work import SqlAlchemyOpportunityUnitOfWork
from shared.schemas.identifiers import (
    EmployeeId,
    HandoffId,
    LossRecordId,
    OpportunityId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.money import CurrencyCode, Money

_NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)


class _AllowAuthorizer:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object, object, TenantId]] = []

    def require(self, actor, action, scope, tenant_id) -> str:
        self.calls.append((actor, action, scope, tenant_id))
        return "test:allow"


class _Audit:
    def __init__(self) -> None:
        self.entries: list[dict[str, str]] = []

    def log(self, *, actor, action, tenant_id, scope, rule) -> None:
        self.entries.append(
            {
                "actor": actor,
                "action": action,
                "tenant_id": str(tenant_id),
                "scope": scope,
                "rule": rule,
            }
        )


def _opportunity(
    tenant: TenantId, opportunity_id: str, *, owner: str | None
) -> opportunity_models.Opportunity:
    return opportunity_models.Opportunity(
        opportunity_id=OpportunityId(opportunity_id),
        tenant_id=tenant,
        account_id=ProspectAccountId(f"account-{opportunity_id}"),
        need_id=ValidatedNeedId(f"need-{opportunity_id}"),
        product_category="hinges",
        created_at=_NOW,
        account_name=f"Account {opportunity_id}",
        country="US",
        owner=EmployeeId(owner) if owner is not None else None,
    )


def _handoff(
    tenant: TenantId,
    handoff_id: str,
    opportunity_id: str,
    *,
    age_seconds: int,
    assigned_to: str,
) -> opportunity_models.HandoffPacket:
    return opportunity_models.HandoffPacket(
        handoff_id=HandoffId(handoff_id),
        tenant_id=tenant,
        opportunity_id=OpportunityId(opportunity_id),
        trigger=opportunity_models.HandoffTrigger.QUOTE_REQUESTED,
        requested_at=_NOW - timedelta(seconds=age_seconds),
        account_name=f"Account {opportunity_id}",
        country="US",
        why_valuable=f"Why {handoff_id}",
        customer_verbatim=f"Customer said {handoff_id}",
        assigned_to=EmployeeId(assigned_to),
        how_we_found_them="Inbound reply",
        validated_need_summary="Confirmed need",
        missing_information=["finish"],
        conversation_summary="Customer asked for a quote.",
        already_sent=["catalogue"],
        commitments_made=["none"],
        suggested_next_step="Confirm finish",
        evidence_links=[f"evidence://{handoff_id}"],
    )


def _headers(tenant: TenantId, employee_id: str) -> dict[str, str]:
    return {"X-Tenant-Id": str(tenant), "X-Employee-Id": employee_id}


async def test_real_handoff_api_queue_packet_accept_and_loss_aggregate(
    db_url: str,
) -> None:
    """Real services must keep tenant/order/ABAC/atomicity/audit invariants together."""
    tenant = TenantId("t-s315-api")
    other = TenantId("t-s315-api-other")
    boss, sales_a, sales_b = "emp-s315-boss", "emp-s315-a", "emp-s315-b"
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    seed = factory()
    try:
        employee_repo = EmployeeRepositoryImpl(seed, tenant)
        for employee_id, role in (
            (boss, employee_models.Role.BOSS),
            (sales_a, employee_models.Role.SALES),
            (sales_b, employee_models.Role.SALES),
        ):
            await employee_repo.add(
                employee_models.Employee(
                    employee_id=EmployeeId(employee_id),
                    tenant_id=tenant,
                    name=employee_id,
                    role=role,
                    created_at=_NOW,
                )
            )

        opportunity_repo = OpportunityRepositoryImpl(seed, tenant)
        handoff_repo = HandoffRepositoryImpl(seed, tenant)
        for suffix, owner, age in (
            ("new", boss, 30),
            ("old", boss, 300),
            ("own", sales_a, 90),
            ("foreign", sales_b, 80),
            ("accept", sales_a, 120),
        ):
            opportunity_id = f"opp-s315-{suffix}"
            await opportunity_repo.add(
                _opportunity(tenant, opportunity_id, owner=owner)
            )
            await seed.flush()
            await handoff_repo.add(
                _handoff(
                    tenant,
                    f"hand-s315-{suffix}",
                    opportunity_id,
                    age_seconds=age,
                    assigned_to=owner,
                )
            )

        await OpportunityRepositoryImpl(seed, other).add(
            _opportunity(other, "opp-s315-cross-queue", owner="emp-cross")
        )
        await seed.flush()
        await HandoffRepositoryImpl(seed, other).add(
            _handoff(
                other,
                "hand-s315-cross",
                "opp-s315-cross-queue",
                age_seconds=86_400,
                assigned_to="emp-cross",
            )
        )

        for scope_tenant, suffix, reason, count in (
            (tenant, "own-price", opportunity_models.LossReason.PRICE_TOO_HIGH, 2),
            (tenant, "own-need", opportunity_models.LossReason.NEED_NOT_REAL, 1),
            (other, "cross-price", opportunity_models.LossReason.PRICE_TOO_HIGH, 3),
        ):
            for index in range(count):
                opportunity_id = f"opp-s315-{suffix}-{index}"
                await OpportunityRepositoryImpl(seed, scope_tenant).add(
                    _opportunity(scope_tenant, opportunity_id, owner=None)
                )
                await seed.flush()
                await LossRecordRepositoryImpl(seed, scope_tenant).add(
                    scope_tenant,
                    opportunity_models.LossRecord(
                        loss_record_id=LossRecordId(f"loss-s315-{suffix}-{index}"),
                        tenant_id=scope_tenant,
                        opportunity_id=OpportunityId(opportunity_id),
                        loss_reason=reason,
                        died_at_state=opportunity_models.OpportunityState.QUOTED,
                        confirmed_by=EmployeeId(boss),
                        confirmed_at=_NOW,
                        recorded_at=_NOW,
                    ),
                )
        await seed.commit()

        employee_authorizer = _AllowAuthorizer()
        employee_audit = _Audit()

        @asynccontextmanager
        async def employee_scope(scope_tenant: TenantId):
            session = factory()
            try:
                employees = EmployeeRepositoryImpl(session, scope_tenant)
                yield EmployeeServiceImpl(
                    employees=employees,
                    territories=TerritoryRepositoryImpl(session, scope_tenant),
                    ownership=OwnershipRepositoryImpl(session, scope_tenant),
                    now=lambda: _NOW,
                    manager_pool=lambda _: (),
                    count_active_accounts=employees.count_active_accounts,
                    authorizer=employee_authorizer,
                    audit=employee_audit,
                )
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
            finally:
                await session.close()

        domain_authorizer = _AllowAuthorizer()
        domain_audit = _Audit()
        service = OpportunityServiceImpl(
            lambda: SqlAlchemyOpportunityUnitOfWork(factory, tenant),
            OpportunityScorerImpl(
                ScoringPolicy(
                    version="s3-15-test",
                    value_band_boundaries=(
                        Money(Decimal(100), CurrencyCode("USD")),
                    ),
                    bucket_map={rank: "high" for rank in range(1, 8)},
                )
            ),
            HandoffPolicy(sla_seconds=60, backlog_threshold=5),
            authorizer=domain_authorizer,
            audit=domain_audit,
            now=lambda: _NOW,
        )
        api_authorizer = _AllowAuthorizer()
        app = create_app(
            settings=ApiSettings(
                tenant_id=str(tenant), dev_mode=True, retry_after_seconds=5
            ),
            dependencies=ConfiguredApiDependencies(
                opportunities=service,
                employees=employee_scope,
                opportunity_authorizer=api_authorizer,
                employee_authorizer=employee_authorizer,
                workflow_engine=object(),
                outbox_deliverer=object(),
                notification_router=object(),
                notification_dedup_store=object(),
                employee_lookup_actor=EmployeeActor(
                    actor_id="system:api-identity",
                    scope=EmployeeScope.SYSTEM,
                    role="system",
                ),
            ),
        )
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            queue = await client.get(
                "/crm/handoffs?limit=2", headers=_headers(tenant, boss)
            )
            own_packet = await client.get(
                "/crm/handoffs/hand-s315-own", headers=_headers(tenant, sales_a)
            )
            denied_packet = await client.get(
                "/crm/handoffs/hand-s315-foreign",
                headers=_headers(tenant, sales_a),
            )
            employee_ids = (sales_a, sales_a)
            accepts = await asyncio.gather(
                *(
                    client.post(
                        "/crm/handoffs/hand-s315-accept/accept",
                        headers=_headers(tenant, employee_id),
                    )
                    for employee_id in employee_ids
                )
            )
            analytics = await client.get(
                "/crm/analytics/loss-reasons?since_days=30",
                headers=_headers(tenant, boss),
            )

        assert queue.status_code == 200
        assert [row["handoff_id"] for row in queue.json()] == [
            "hand-s315-old",
            "hand-s315-accept",
        ]
        assert "hand-s315-cross" not in queue.text
        assert own_packet.status_code == 200
        assert own_packet.json()["missing_information"] == ["finish"]
        assert own_packet.json()["wait_seconds"] == 90
        assert denied_packet.status_code == 403
        assert denied_packet.json() == {"code": "forbidden", "message": "没有权限"}
        assert "hand-s315-foreign" not in denied_packet.text

        assert sorted(response.status_code for response in accepts) == [204, 409]
        conflict = next(response for response in accepts if response.status_code == 409)
        assert conflict.json() == {
            "code": "handoff_already_accepted",
            "message": "接管已被接受",
        }
        winner = employee_ids[
            next(index for index, response in enumerate(accepts) if response.status_code == 204)
        ]
        check = factory()
        try:
            packet = await HandoffRepositoryImpl(check, tenant).get(
                tenant, HandoffId("hand-s315-accept")
            )
            events = (
                await check.scalars(
                    select(OutboxEventRow).where(
                        OutboxEventRow.tenant_id == tenant,
                        OutboxEventRow.event_type == "HandoffAccepted",
                    )
                )
            ).all()
        finally:
            await check.close()
        assert packet is not None
        assert packet.state is opportunity_models.HandoffState.ACCEPTED
        assert packet.accepted_by == EmployeeId(winner)
        assert len(events) == 1
        assert events[0].event_payload["accepted_by"] == winner

        assert analytics.status_code == 200
        assert analytics.json() == {
            "price_too_high": {"quoted": 2},
            "need_not_real": {"quoted": 1},
        }
        boss_scope = OpportunityScope(level=ScopeLevel.TENANT)
        sales_a_scope = OpportunityScope(
            level=ScopeLevel.SELF,
            allowed_owners=frozenset({EmployeeId(sales_a)}),
        )
        boss_actor = OpportunityActor(
            actor_id=boss,
            scope=boss_scope,
            role="boss",
        )
        sales_a_actor = OpportunityActor(
            actor_id=sales_a,
            scope=sales_a_scope,
            role="sales",
        )
        expected_authorizer_calls = Counter(
            [
                (
                    boss_actor,
                    OpportunityAction.HANDOFF_QUEUE_READ,
                    boss_scope,
                    tenant,
                ),
                (
                    sales_a_actor,
                    OpportunityAction.HANDOFF_READ,
                    sales_a_scope,
                    tenant,
                ),
                (
                    sales_a_actor,
                    OpportunityAction.HANDOFF_READ,
                    sales_a_scope,
                    tenant,
                ),
                (
                    sales_a_actor,
                    OpportunityAction.HANDOFF_ACCEPT,
                    sales_a_scope,
                    tenant,
                ),
                (
                    sales_a_actor,
                    OpportunityAction.HANDOFF_ACCEPT,
                    sales_a_scope,
                    tenant,
                ),
                (
                    boss_actor,
                    OpportunityAction.LOSS_REASON_READ,
                    boss_scope,
                    tenant,
                ),
            ]
        )
        assert Counter(api_authorizer.calls) == expected_authorizer_calls
        assert Counter(domain_authorizer.calls) == expected_authorizer_calls
        assert any(
            entry == {
                "actor": boss,
                "action": OpportunityAction.LOSS_REASON_READ.value,
                "tenant_id": str(tenant),
                "scope": "tenant",
                "rule": "test:allow",
            }
            for entry in domain_audit.entries
        )
        assert any(entry["rule"] == "deny:abac:owner" for entry in domain_audit.entries)
        assert [
            entry
            for entry in domain_audit.entries
            if entry["action"] == OpportunityAction.HANDOFF_ACCEPT.value
        ] == [
            {
                "actor": sales_a,
                "action": OpportunityAction.HANDOFF_ACCEPT.value,
                "tenant_id": str(tenant),
                "scope": "self",
                "rule": "test:allow",
            }
        ]
    finally:
        await seed.close()
        await engine.dispose()
