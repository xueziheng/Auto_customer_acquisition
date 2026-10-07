"""真实 PostgreSQL 写侧资源 ABAC 与审计提交顺序。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.opportunities.permissions import (
    Actor as OpportunityActor,
)
from domains.opportunities.permissions import (
    OpportunityAction,
    OpportunityScope,
    Phase1OpportunityAuthorizer,
    ScopeLevel,
)
from domains.opportunities.schemas import HandoffCreateRequest
from domains.opportunities.service_impl import (
    HandoffPolicy,
    LossReason,
    OpportunityServiceImpl,
    OpportunityState,
)
from infra.db.session import create_engine_from
from infra.db.tables import (
    HandoffRow,
    LossRecordRow,
    OpportunityRow,
    OutboxEventRow,
    ProvenanceRecordRow,
)
from infra.db.unit_of_work import SqlAlchemyOpportunityUnitOfWork
from shared.errors import PermissionDenied
from shared.schemas.identifiers import EmployeeId, HandoffId, OpportunityId, TenantId
from shared.schemas.provenance import Provenance, SourceType

_NOW = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _ExactActionAuthorizer:
    actor: OpportunityActor
    action: OpportunityAction
    tenant_id: TenantId

    def require(self, actor, action, scope, tenant_id) -> str:
        if (
            actor != self.actor
            or action is not self.action
            or scope != self.actor.scope
            or tenant_id != self.tenant_id
        ):
            raise PermissionDenied("测试精确授权拒绝")
        return f"test:exact:{action.value}"


class _Audit:
    def __init__(self, trace: list[str] | None = None) -> None:
        self.entries: list[dict[str, str]] = []
        self.trace = trace

    def log(self, *, actor, action, tenant_id, scope, rule) -> None:
        if self.trace is not None:
            self.trace.append("audit")
        self.entries.append(
            {
                "actor": actor,
                "action": action,
                "tenant_id": str(tenant_id),
                "scope": scope,
                "rule": rule,
            }
        )


class _UnusedScorer:
    async def score(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("写侧 ABAC 测试不应进入打分")


class _TracingUoW(SqlAlchemyOpportunityUnitOfWork):
    def __init__(self, *args: object, trace: list[str], **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self._trace = trace

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await super().__aexit__(exc_type, exc, tb)
        if exc_type is None:
            self._trace.append("commit")


@dataclass(frozen=True)
class _Fixture:
    engine: AsyncEngine
    factory: async_sessionmaker[AsyncSession]
    tenant: TenantId
    other_tenant: TenantId
    sales_a: EmployeeId
    sales_b: EmployeeId
    sales_a_actor: OpportunityActor
    opportunity_a: OpportunityId
    opportunity_b: OpportunityId
    handoff_b: HandoffId
    before: tuple[object, ...]


async def _seed_write_abac_fixture(db_url: str, operation: str) -> _Fixture:
    engine = create_engine_from(str(db_url))
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    suffix = operation.replace("_", "-")
    tenant = TenantId(f"tenant-abac-{suffix}")
    other_tenant = TenantId(f"t-other-{suffix}")
    sales_a = EmployeeId(f"emp-a-{suffix}")
    sales_b = EmployeeId(f"emp-b-{suffix}")
    opportunity_a = OpportunityId(f"opp-a-{suffix}")
    opportunity_b = OpportunityId(f"opp-b-{suffix}")
    handoff_b = HandoffId(f"hand-b-{suffix}")
    state_b = (
        OpportunityState.NEGOTIATING.value
        if operation == "mark_won"
        else OpportunityState.QUALIFIED.value
    )
    try:
        async with factory.begin() as session:
            session.add_all(
                [
                    OpportunityRow(
                        opportunity_id=opportunity_a,
                        tenant_id=tenant,
                        account_id=f"acct-a-{suffix}",
                        account_name="Account A",
                        country="US",
                        need_id=f"need-a-{suffix}",
                        product_category="hinges",
                        state=OpportunityState.QUALIFIED.value,
                        created_at=_NOW,
                        owner=sales_a,
                    ),
                    OpportunityRow(
                        opportunity_id=opportunity_b,
                        tenant_id=tenant,
                        account_id=f"acct-b-{suffix}",
                        account_name="Account B",
                        country="DE",
                        need_id=f"need-b-{suffix}",
                        product_category="fasteners",
                        state=state_b,
                        created_at=_NOW,
                        owner=sales_b,
                    ),
                    OpportunityRow(
                        opportunity_id=f"opp-other-{suffix}",
                        tenant_id=other_tenant,
                        account_id=f"acct-other-{suffix}",
                        account_name="Other Tenant",
                        country="FR",
                        need_id=f"need-other-{suffix}",
                        product_category="locks",
                        state=OpportunityState.QUALIFIED.value,
                        created_at=_NOW,
                        owner=f"emp-other-{suffix}",
                    ),
                ]
            )
            await session.flush()
            session.add(
                HandoffRow(
                    handoff_id=handoff_b,
                    tenant_id=tenant,
                    opportunity_id=opportunity_b,
                    trigger="quote_requested",
                    state="requested",
                    requested_at=_NOW,
                    account_name="Account B",
                    country="DE",
                    why_valuable="Customer requested a quote",
                    customer_verbatim="Please quote the requested fasteners",
                    assigned_to=sales_b,
                )
            )
    except BaseException:
        await engine.dispose()
        raise
    actor = OpportunityActor(
        str(sales_a),
        OpportunityScope(
            level=ScopeLevel.SELF,
            allowed_owners=frozenset({sales_a}),
        ),
        "sales",
    )
    fixture = _Fixture(
        engine=engine,
        factory=factory,
        tenant=tenant,
        other_tenant=other_tenant,
        sales_a=sales_a,
        sales_b=sales_b,
        sales_a_actor=actor,
        opportunity_a=opportunity_a,
        opportunity_b=opportunity_b,
        handoff_b=handoff_b,
        before=(),
    )
    return _Fixture(**{**fixture.__dict__, "before": await _durable_snapshot(fixture)})


async def _durable_snapshot(fixture: _Fixture) -> tuple[object, ...]:
    async with fixture.factory() as session:
        opportunities = (
            await session.execute(
                select(
                    OpportunityRow.tenant_id,
                    OpportunityRow.opportunity_id,
                    OpportunityRow.state,
                    OpportunityRow.owner,
                )
                .where(
                    OpportunityRow.tenant_id.in_([fixture.tenant, fixture.other_tenant])
                )
                .order_by(OpportunityRow.tenant_id, OpportunityRow.opportunity_id)
            )
        ).all()
        handoff = (
            await session.execute(
                select(HandoffRow.state, HandoffRow.accepted_by).where(
                    HandoffRow.tenant_id == fixture.tenant,
                    HandoffRow.handoff_id == fixture.handoff_b,
                )
            )
        ).one()
        counts = []
        for row_type in (LossRecordRow, ProvenanceRecordRow, OutboxEventRow):
            counts.append(
                await session.scalar(
                    select(func.count())
                    .select_from(row_type)
                    .where(row_type.tenant_id == fixture.tenant)
                )
            )
    return tuple(opportunities), handoff, tuple(counts)


def _real_service(
    fixture: _Fixture,
    *,
    audit: _Audit,
    authorizer: object | None = None,
    trace: list[str] | None = None,
) -> OpportunityServiceImpl:
    if trace is None:
        uow_factory = lambda: SqlAlchemyOpportunityUnitOfWork(
            fixture.factory, fixture.tenant
        )
    else:
        uow_factory = lambda: _TracingUoW(
            fixture.factory, fixture.tenant, trace=trace
        )
    return OpportunityServiceImpl(
        uow_factory,
        _UnusedScorer(),  # type: ignore[arg-type]
        HandoffPolicy(sla_seconds=300, backlog_threshold=20),
        authorizer=authorizer or Phase1OpportunityAuthorizer(fixture.tenant),
        audit=audit,
        now=lambda: _NOW,
    )


_ACTION_BY_OPERATION = {
    "assign": OpportunityAction.OPPORTUNITY_ASSIGN,
    "transition": OpportunityAction.OPPORTUNITY_TRANSITION,
    "mark_lost": OpportunityAction.OPPORTUNITY_MARK_LOST,
    "mark_won": OpportunityAction.OPPORTUNITY_MARK_WON,
    "request_handoff": OpportunityAction.HANDOFF_REQUEST,
    "accept_handoff": OpportunityAction.HANDOFF_ACCEPT,
}


async def _invoke_cross_owner_operation(
    service: OpportunityServiceImpl, fixture: _Fixture, operation: str
) -> Any:
    if operation == "assign":
        return await service.assign(
            fixture.tenant,
            fixture.opportunity_a,
            fixture.sales_b,
            fixture.sales_a,
            actor=fixture.sales_a_actor,
        )
    if operation == "transition":
        return await service.transition(
            fixture.tenant,
            fixture.opportunity_b,
            OpportunityState.ASSIGNED,
            actor=fixture.sales_a_actor,
        )
    if operation == "mark_lost":
        return await service.mark_lost(
            fixture.tenant,
            fixture.opportunity_b,
            LossReason.PRICE_TOO_HIGH,
            actor=fixture.sales_a_actor,
            confirmed_by=fixture.sales_a,
            confirmed_at=_NOW,
        )
    if operation == "mark_won":
        return await service.mark_won(
            fixture.tenant,
            fixture.opportunity_b,
            actor=fixture.sales_a_actor,
            confirmed_by=fixture.sales_a,
            confirmed_at=_NOW,
        )
    if operation == "request_handoff":
        return await service.request_handoff(
            fixture.tenant,
            HandoffCreateRequest(
                opportunity_id=fixture.opportunity_b,
                trigger="quote_requested",
                account_name="Account B",
                country="DE",
                why_valuable="Customer requested a quote",
                customer_verbatim="Please quote the requested fasteners",
                customer_verbatim_provenance=Provenance(
                    source_type=SourceType.CONVERSATION,
                    source_id="message-abac",
                    extracted_by="human",
                    extracted_at=_NOW,
                ),
            ),
            actor=fixture.sales_a_actor,
        )
    return await service.accept_handoff(
        fixture.tenant,
        fixture.handoff_b,
        fixture.sales_a,
        actor=fixture.sales_a_actor,
    )


@pytest.mark.parametrize(
    "operation",
    [
        "assign",
        "transition",
        "mark_lost",
        "mark_won",
        "request_handoff",
        "accept_handoff",
    ],
)
async def test_write_resource_abac_denies_before_any_durable_effect(
    db_url: str, operation: str
) -> None:
    fixture = await _seed_write_abac_fixture(db_url, operation)
    try:
        audit = _Audit()
        actor = fixture.sales_a_actor
        action = _ACTION_BY_OPERATION[operation]
        service = _real_service(
            fixture,
            audit=audit,
            authorizer=_ExactActionAuthorizer(actor, action, fixture.tenant),
        )

        with pytest.raises(PermissionDenied):
            await _invoke_cross_owner_operation(service, fixture, operation)

        after = await _durable_snapshot(fixture)
        assert after == fixture.before
        assert audit.entries == [
            {
                "actor": str(fixture.sales_a),
                "action": action.value,
                "tenant_id": str(fixture.tenant),
                "scope": "self",
                "rule": "deny:abac:owner",
            }
        ]
    finally:
        await fixture.engine.dispose()


async def test_accept_requires_actor_to_equal_accepted_by_and_linked_resource_scope(
    db_url: str,
) -> None:
    fixture = await _seed_write_abac_fixture(db_url, "accept-identity")
    try:
        service = _real_service(
            fixture,
            audit=_Audit(),
            authorizer=_ExactActionAuthorizer(
                fixture.sales_a_actor,
                OpportunityAction.HANDOFF_ACCEPT,
                fixture.tenant,
            ),
        )
        with pytest.raises(PermissionDenied):
            await service.accept_handoff(
                fixture.tenant,
                fixture.handoff_b,
                fixture.sales_b,
                actor=fixture.sales_a_actor,
            )
        assert (await _durable_snapshot(fixture))[1] == ("requested", None)
    finally:
        await fixture.engine.dispose()


async def test_cross_owner_accepted_handoff_denies_before_revealing_state(
    db_url: str,
) -> None:
    """跨 owner 即使目标已接受，也只能观察到资源 ABAC 拒绝。"""
    fixture = await _seed_write_abac_fixture(db_url, "accept-state-order")
    try:
        async with fixture.factory.begin() as session:
            await session.execute(
                update(HandoffRow)
                .where(
                    HandoffRow.tenant_id == fixture.tenant,
                    HandoffRow.handoff_id == fixture.handoff_b,
                )
                .values(
                    state="accepted",
                    accepted_by=fixture.sales_b,
                    accepted_at=_NOW,
                )
            )
        before = await _durable_snapshot(fixture)
        audit = _Audit()
        service = _real_service(
            fixture,
            audit=audit,
            authorizer=_ExactActionAuthorizer(
                fixture.sales_a_actor,
                OpportunityAction.HANDOFF_ACCEPT,
                fixture.tenant,
            ),
        )

        with pytest.raises(PermissionDenied):
            await service.accept_handoff(
                fixture.tenant,
                fixture.handoff_b,
                fixture.sales_a,
                actor=fixture.sales_a_actor,
            )

        assert await _durable_snapshot(fixture) == before
        assert audit.entries == [
            {
                "actor": str(fixture.sales_a),
                "action": OpportunityAction.HANDOFF_ACCEPT.value,
                "tenant_id": str(fixture.tenant),
                "scope": "self",
                "rule": "deny:abac:owner",
            }
        ]
    finally:
        await fixture.engine.dispose()


async def test_successful_write_audits_once_only_after_commit(db_url: str) -> None:
    fixture = await _seed_write_abac_fixture(db_url, "success")
    try:
        trace: list[str] = []
        audit = _Audit(trace)
        service = _real_service(fixture, audit=audit, trace=trace)
        await service.transition(
            fixture.tenant,
            fixture.opportunity_a,
            OpportunityState.ASSIGNED,
            actor=fixture.sales_a_actor,
        )
        snapshot = await _durable_snapshot(fixture)
        own_row = next(row for row in snapshot[0] if row[1] == fixture.opportunity_a)
        assert own_row[2] == "assigned"
        assert trace == ["commit", "audit"]
        assert [entry["rule"] for entry in audit.entries] == [
            "phase1:sales:self:opportunity:transition"
        ]
    finally:
        await fixture.engine.dispose()
