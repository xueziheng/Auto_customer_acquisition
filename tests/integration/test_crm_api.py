"""S3-14 CRM API 的跨层 Pydantic/HTTP 契约测试。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal

from httpx import ASGITransport, AsyncClient

from apps.api.dependencies import ConfiguredApiDependencies
from apps.api.main import ApiSettings, create_app
from domains.employees import models as employee_models
from domains.employees.permissions import Actor as EmployeeActor
from domains.employees.permissions import EmployeeScope
from domains.employees.service_impl import EmployeeServiceImpl
from domains.opportunities.permissions import Actor as OpportunityActor
from domains.opportunities.permissions import (
    OpportunityAction,
    OpportunityScope,
    ScopeLevel,
)
from domains.opportunities.scorer import OpportunityScorerImpl
from domains.opportunities.scoring import ScoringPolicy
from domains.opportunities.service_impl import HandoffPolicy, OpportunityServiceImpl
from infra.db.session import create_engine_from
from infra.db.unit_of_work import SqlAlchemyOpportunityUnitOfWork
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId, TenantId
from shared.schemas.money import CurrencyCode, Money
from tests.provider_readiness_fakes import provider_readiness_dependencies
from tests.unit.test_crm_router import (
    _HEADERS,
    _app,
    _Client,
    _create_body,
    _ManualRuntime,
)


class _AllowAuthorizer:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object, object, TenantId]] = []

    def require(self, actor, action, scope, tenant_id) -> str:
        self.calls.append((actor, action, scope, tenant_id))
        return "test:allow"


class _DenyAuthorizer(_AllowAuthorizer):
    def require(self, actor, action, scope, tenant_id) -> str:
        self.calls.append((actor, action, scope, tenant_id))
        raise PermissionDenied("domain deny")


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


def test_create_accepts_domain_dto_envelope_through_fastapi_validation() -> None:
    """若 API 用裸 dict 或复制字段，日期/Decimal/Provenance 的域 DTO 契约会漂移。"""
    app, opportunities, _, _ = _app(role="boss")
    body = _create_body()
    request = body["request"]
    assert isinstance(request, dict)
    request["required_by"] = "2026-09-01"
    request["target_price"] = {"amount": "1250.50", "currency": "USD"}

    response = _Client(app).request(
        "POST", "/crm/opportunities", headers=_HEADERS, json=body
    )

    assert response.status_code == 201
    create_call = opportunities.calls[0]
    domain_request = create_call[1][1]
    domain_evidence = create_call[1][2]
    assert domain_request.required_by.isoformat() == "2026-09-01"
    assert str(domain_request.target_price.amount) == "1250.50"
    assert domain_evidence.level.value == "customer_interest_reply"


def test_create_malformed_money_string_returns_safe_validation_error() -> None:
    """若共享 TypeAdapter 逸出 Decimal 异常，坏金额会变成未脱敏 500。"""
    app, opportunities, _, _ = _app(role="boss")
    body = _create_body()
    request = body["request"]
    assert isinstance(request, dict)
    request["minimum_order_value"] = {"amount": "abc", "currency": "USD"}

    response = _Client(app).request(
        "POST", "/crm/opportunities", headers=_HEADERS, json=body
    )

    assert response.status_code == 400
    assert response.json() == {"code": "validation_error", "message": "请求参数无效"}
    assert opportunities.calls == []


def test_list_detail_transition_and_mark_lost_are_http_visible_domain_operations() -> None:
    """若任一路由只注册未接域服务，CRM UI 会看到成功响应却没有真实操作。"""
    app, opportunities, _, _ = _app()
    client = _Client(app)

    listed = client.request("GET", "/crm/opportunities?limit=2", headers=_HEADERS)
    detailed = client.request("GET", "/crm/opportunities/opp-1", headers=_HEADERS)
    transitioned = client.request(
        "POST",
        "/crm/opportunities/opp-1/transition",
        headers=_HEADERS,
        json={"target": "assigned"},
    )
    lost = client.request(
        "POST",
        "/crm/opportunities/opp-1/mark-lost",
        headers=_HEADERS,
        json={"reason": "price_too_high"},
    )

    assert [response.status_code for response in (listed, detailed, transitioned, lost)] == [
        200,
        200,
        200,
        200,
    ]
    assert listed.json()[0]["opportunity_id"] == "opp-1"
    assert detailed.json()["provenance"][0]["field_name"] == "quantity"
    assert [name for name, _, _ in opportunities.calls] == [
        "list",
        "get",
        "transition",
        "mark_lost",
    ]


async def test_create_uses_real_postgres_services_and_both_authorization_layers(
    db_url: str,
) -> None:
    """若 ASGI 只接 fake，二次授权/审计或事务副作用的回归不会被发现。"""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from infra.db.repositories.employees import (
        EmployeeRepositoryImpl,
        OwnershipRepositoryImpl,
        TerritoryRepositoryImpl,
    )
    from infra.db.repositories.opportunities import OpportunityRepositoryImpl
    from infra.db.tables import OutboxEventRow, ProvenanceRecordRow, ScoreSnapshotRow

    tenant = TenantId("t-crm-real")
    now = datetime(2026, 8, 9, tzinfo=UTC)
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    employee_authorizer = _AllowAuthorizer()
    employee_audit = _Audit()

    @asynccontextmanager
    async def employee_scope(scope_tenant: TenantId):
        session = factory()
        try:
            employees = EmployeeRepositoryImpl(session, scope_tenant)
            service = EmployeeServiceImpl(
                employees=employees,
                territories=TerritoryRepositoryImpl(session, scope_tenant),
                ownership=OwnershipRepositoryImpl(session, scope_tenant),
                now=lambda: now,
                manager_pool=lambda _: (),
                count_active_accounts=employees.count_active_accounts,
                authorizer=employee_authorizer,
                audit=employee_audit,
            )
            yield service
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    class _TrackingOpportunityUowFactory:
        """仍委托真实 UoW；拒绝路径一旦触及 UoW 即留下不可忽略的痕迹。"""

        def __init__(self) -> None:
            self.constructed = 0
            self.entered = 0
            self.exited = 0

        def __call__(self):
            self.constructed += 1
            inner = SqlAlchemyOpportunityUnitOfWork(factory, tenant)
            tracker = self

            class _TrackedContext:
                async def __aenter__(self):
                    tracker.entered += 1
                    return await inner.__aenter__()

                async def __aexit__(self, exc_type, exc, tb):
                    tracker.exited += 1
                    return await inner.__aexit__(exc_type, exc, tb)

            return _TrackedContext()

    try:
        seed = AsyncSession(bind=engine, expire_on_commit=False)
        try:
            await EmployeeRepositoryImpl(seed, tenant).add(
                employee_models.Employee(
                    employee_id=EmployeeId("emp-sales"),
                    tenant_id=tenant,
                    name="Sales",
                    role=employee_models.Role.SALES,
                    created_at=now,
                )
            )
            await EmployeeRepositoryImpl(seed, tenant).add(
                employee_models.Employee(
                    employee_id=EmployeeId("emp-boss"),
                    tenant_id=tenant,
                    name="Boss",
                    role=employee_models.Role.BOSS,
                    created_at=now,
                )
            )
            await seed.commit()
        finally:
            await seed.close()

        domain_authorizer = _AllowAuthorizer()
        domain_audit = _Audit()
        policy = ScoringPolicy(
            version="test-v1",
            value_band_boundaries=(Money(Decimal(100), CurrencyCode("USD")),),
            bucket_map={rank: "high" for rank in range(1, 8)},
        )
        service = OpportunityServiceImpl(
            lambda: SqlAlchemyOpportunityUnitOfWork(factory, tenant),
            OpportunityScorerImpl(policy),
            HandoffPolicy(sla_seconds=60, backlog_threshold=5),
            authorizer=domain_authorizer,
            audit=domain_audit,
            now=lambda: now,
        )
        api_authorizer = _AllowAuthorizer()
        manual_runtime = _ManualRuntime()
        app = create_app(
            settings=ApiSettings(tenant_id=str(tenant), dev_mode=True, retry_after_seconds=5),
            dependencies=ConfiguredApiDependencies(
                opportunities=service,
                outreach=manual_runtime,
                sending_identities=manual_runtime,
                tool_gateway=manual_runtime,
                delivery_materials=manual_runtime,
                unsubscribe_links=manual_runtime,
                unsubscribe_service=manual_runtime,
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
                outreach_authorizer=object(),
                sending_identity_authorizer=object(),
                campaign_scope_resolver=object(),
                in_app_notifications=object(),
                **provider_readiness_dependencies(tenant),
            ),
        )
        body = _create_body()
        request = body["request"]
        assert isinstance(request, dict)
        request.update(
            {
                "need_id": "need-real-allow",
                "evidence_tier": "mid_high",
                "estimated_order_value": {"amount": "1000", "currency": "USD"},
                "supply_available": True,
            }
        )
        headers = {"X-Tenant-Id": str(tenant), "X-Employee-Id": "emp-boss"}
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            allowed = await client.post("/crm/opportunities", headers=headers, json=body)
        assert allowed.status_code == 201
        assert [entry["action"] for entry in domain_audit.entries] == [
            "opportunity:create",
            "opportunity:assign",
            "opportunity:read",
        ]
        assert [call[1] for call in api_authorizer.calls] == [
            OpportunityAction.OPPORTUNITY_CREATE
        ]

        denied_audit = _Audit()
        denied_authorizer = _DenyAuthorizer()
        denied_uow_factory = _TrackingOpportunityUowFactory()
        denied_service = OpportunityServiceImpl(
            denied_uow_factory,
            OpportunityScorerImpl(policy),
            HandoffPolicy(sla_seconds=60, backlog_threshold=5),
            authorizer=denied_authorizer,
            audit=denied_audit,
            now=lambda: now,
        )
        app.state.dependencies = ConfiguredApiDependencies(
            opportunities=denied_service,
            outreach=manual_runtime,
            sending_identities=manual_runtime,
            tool_gateway=manual_runtime,
            delivery_materials=manual_runtime,
            unsubscribe_links=manual_runtime,
            unsubscribe_service=manual_runtime,
            employees=employee_scope,
            opportunity_authorizer=api_authorizer,
            employee_authorizer=employee_authorizer,
            workflow_engine=object(),
            outbox_deliverer=object(),
            notification_router=object(),
            notification_dedup_store=object(),
            employee_lookup_actor=EmployeeActor(
                actor_id="system:api-identity", scope=EmployeeScope.SYSTEM, role="system"
            ),
            outreach_authorizer=object(),
            sending_identity_authorizer=object(),
            campaign_scope_resolver=object(),
            in_app_notifications=object(),
            **provider_readiness_dependencies(tenant),
        )
        expected_scope = OpportunityScope(level=ScopeLevel.TENANT)
        expected_actor = OpportunityActor(
            actor_id="emp-boss", scope=expected_scope, role="boss"
        )
        before_first_gate_calls = len(api_authorizer.calls)
        baseline = AsyncSession(bind=engine, expire_on_commit=False)
        try:
            snapshot_ids = set(
                (await baseline.scalars(
                    select(ScoreSnapshotRow.snapshot_id).where(
                        ScoreSnapshotRow.tenant_id == tenant
                    )
                )).all()
            )
            outbox_ids = set(
                (await baseline.scalars(
                    select(OutboxEventRow.event_id).where(
                        OutboxEventRow.tenant_id == tenant,
                        OutboxEventRow.event_type == "OpportunityQualified",
                    )
                )).all()
            )
            provenance_entities = set(
                (await baseline.scalars(
                    select(ProvenanceRecordRow.entity_id).where(
                        ProvenanceRecordRow.tenant_id == tenant,
                        ProvenanceRecordRow.entity_type == "opportunity",
                    )
                )).all()
            )
        finally:
            await baseline.close()
        request.update(
            {
                "need_id": "need-real-deny",
                "account_id": "account-real-deny",
            }
        )
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            denied = await client.post("/crm/opportunities", headers=headers, json=body)
        assert denied.status_code == 403
        assert api_authorizer.calls[before_first_gate_calls:] == [
            (expected_actor, OpportunityAction.OPPORTUNITY_CREATE, expected_scope, tenant)
        ]
        assert denied_authorizer.calls == [
            (expected_actor, OpportunityAction.OPPORTUNITY_CREATE, expected_scope, tenant)
        ]
        assert (
            denied_uow_factory.constructed,
            denied_uow_factory.entered,
            denied_uow_factory.exited,
        ) == (0, 0, 0)
        assert denied_audit.entries == [
            {
                "actor": "emp-boss",
                "action": "opportunity:create",
                "tenant_id": str(tenant),
                "scope": "tenant",
                "rule": "deny",
            }
        ]
        check = AsyncSession(bind=engine, expire_on_commit=False)
        try:
            assert await OpportunityRepositoryImpl(check, tenant).find_by_need(
                tenant, "need-real-deny"
            ) is None
            assert await OwnershipRepositoryImpl(check, tenant).get(
                tenant, "account-real-deny"
            ) is None
            assert set(
                (await check.scalars(
                    select(ScoreSnapshotRow.snapshot_id).where(
                        ScoreSnapshotRow.tenant_id == tenant
                    )
                )).all()
            ) == snapshot_ids
            assert set(
                (await check.scalars(
                    select(OutboxEventRow.event_id).where(
                        OutboxEventRow.tenant_id == tenant,
                        OutboxEventRow.event_type == "OpportunityQualified",
                    )
                )).all()
            ) == outbox_ids
            assert set(
                (await check.scalars(
                    select(ProvenanceRecordRow.entity_id).where(
                        ProvenanceRecordRow.tenant_id == tenant,
                        ProvenanceRecordRow.entity_type == "opportunity",
                    )
                )).all()
            ) == provenance_entities
        finally:
            await check.close()
    finally:
        await engine.dispose()
