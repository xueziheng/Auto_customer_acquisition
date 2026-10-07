"""READ COMMITTED真实独立连接context lease，来源与抬头仍为受控端口。"""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update

from infra.db.tables import (
    EmployeeRow,
    OpportunityRow,
    ProspectAccountRow,
)
from shared.schemas.identifiers import EmployeeId, OpportunityId, TenantId
from tests.integration.test_need_units import NOW, UnitDbCase, unit_db_case, unit_engine
from tests.unit.test_quote_context_contracts import issuer

__all__ = ["unit_db_case", "unit_engine"]


@dataclass
class ContextCase:
    """所有更新通过独立连接；等待用PG阻塞事实而不是固定sleep。"""
    unit: UnitDbCase
    provider: object
    tenant_id: TenantId
    actor_id: EmployeeId
    opportunity_id: OpportunityId
    prepared_by: EmployeeId
    quantity: int

    async def update_employee(self, employee_id: EmployeeId, **changes: object) -> None:
        repo_type = importlib.import_module("infra.db.repositories.employees").EmployeeRepositoryImpl
        async with self.unit.sessions.begin() as session:
            await session.execute(text("SET LOCAL lock_timeout = '2500ms'"))
            repo = repo_type(session, self.tenant_id)
            current = await repo.get(self.tenant_id, employee_id)
            assert current is not None
            for key, value in changes.items():
                setattr(current, key, type(current.role)(value) if key == "role" else value)
            await repo.update(current)

    async def update_owner_manager(self) -> None:
        await self.update_employee(EmployeeId("emp_owner"), manager_id=EmployeeId("emp_manager"))

    async def assign(self, owner: EmployeeId) -> None:
        """使用真实机会assign，不把账户归属转移混为机会owner更新。"""
        from domains.opportunities.permissions import (
            Actor,
            OpportunityScope,
            Phase1OpportunityAuthorizer,
            ScopeLevel,
        )
        from infra.db.unit_of_work import SqlAlchemyOpportunityUnitOfWork
        from tests.integration.test_opportunity_write_abac import _Audit, _UnusedScorer
        impl = importlib.import_module("domains.opportunities.service_impl")
        svc = impl.OpportunityServiceImpl(lambda: SqlAlchemyOpportunityUnitOfWork(self.unit.sessions, self.tenant_id),
            _UnusedScorer(), impl.HandoffPolicy(sla_seconds=300, backlog_threshold=20),
            authorizer=Phase1OpportunityAuthorizer(self.tenant_id), audit=_Audit(), now=lambda: NOW)
        await svc.assign(self.tenant_id, self.opportunity_id, owner, self.actor_id,
            actor=Actor(actor_id=self.actor_id, role="boss", scope=OpportunityScope(level=ScopeLevel.TENANT)))

    async def wait_for_blocked_writer(self, writer: asyncio.Task) -> None:
        async with asyncio.timeout(2):
            async with self.unit.sessions() as session:
                while True:
                    if writer.done():
                        await writer
                        pytest.fail("写方在lease仍持锁时已提交")
                    await session.execute(text("SELECT pg_stat_clear_snapshot()"))
                    blocked = await session.scalar(text("SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname=current_database() AND wait_event_type='Lock' "
                        "AND pid<>pg_backend_pid()"))
                    if blocked:
                        return
                    await asyncio.sleep(0)


async def test_lease_preserves_consumer_validation_error(context_case: ContextCase) -> None:
    c=context_case
    failure=ValueError("受控调用方错误")
    with pytest.raises(ValueError) as caught:
        async with c.provider.open(c.tenant_id,c.opportunity_id,c.actor_id,prepared_by=c.actor_id):
            raise failure
    assert caught.value is failure
    await c.update_owner_manager()


async def test_cleanup_error_is_sanitized_even_after_consumer_failure(context_case: ContextCase,monkeypatch) -> None:
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import AsyncSession

    from domains.quotations.errors import QuoteContextUnavailableError
    c=context_case
    original=AsyncSession.rollback
    async def failed(session):
        await original(session)
        raise SQLAlchemyError("受控清理底层错误")
    monkeypatch.setattr(AsyncSession,"rollback",failed)
    with pytest.raises(QuoteContextUnavailableError) as error:
        async with c.provider.open(c.tenant_id,c.opportunity_id,c.actor_id,prepared_by=c.actor_id):
            raise ValueError("受控业务错误")
    assert error.value.code=="storage_unknown"
    assert "底层错误" not in str(error.value)


async def test_corrupt_shared_provenance_is_fixed_context_error(context_case: ContextCase) -> None:
    from domains.quotations.errors import QuoteContextError
    from infra.db.tables import ValidatedNeedRow
    c=context_case
    async with c.unit.sessions.begin() as session:
        row=await session.scalar(select(ValidatedNeedRow).where(ValidatedNeedRow.tenant_id==c.tenant_id,
            ValidatedNeedRow.need_id==c.unit.need_id))
        material={**row.material,"provenance":{**row.material["provenance"],"source_quote":""}}
        await session.execute(update(ValidatedNeedRow).where(ValidatedNeedRow.tenant_id==c.tenant_id,
            ValidatedNeedRow.need_id==c.unit.need_id).values(material=material))
    with pytest.raises(QuoteContextError) as error:
        async with c.provider.open(c.tenant_id,c.opportunity_id,c.actor_id,prepared_by=c.actor_id):
            pytest.fail("损坏事实不得形成lease")
    assert error.value.code=="facts_corrupt"


@pytest_asyncio.fixture
async def context_case(unit_db_case: UnitDbCase) -> ContextCase:
    from domains.quotations import service
    assert hasattr(service, "QuoteContextProvider"), "缺少上下文持锁端口"
    provider_type = importlib.import_module("infra.db.quote_context").SqlAlchemyQuoteContextProvider
    u = unit_db_case
    await u.confirm()
    await u.demand.update_need_fields(u.tenant, u.need_id, {
        "material": {"value": "steel", "quote": "steel", "extracted_by": u.actor_id},
        "size_spec": {"value": "50 mm", "quote": "50 mm", "extracted_by": u.actor_id},
        "packaging": {"value": "carton", "quote": "carton", "extracted_by": u.actor_id},
        "destination": {"value": "US", "quote": "US", "extracted_by": u.actor_id},
    }, source_message_id="msg_customer_1", updated_by=u.actor_id)
    async with u.sessions.begin() as session:
        assert await session.scalar(text("SHOW transaction_isolation")) == "read committed"
        for eid, role in [(u.actor_id, "boss"), ("emp_owner", "sales"), ("emp_manager", "manager")]:
            session.add(EmployeeRow(tenant_id=u.tenant, employee_id=eid, name=eid, role=role,
                                    is_active=True, created_at=NOW))
        session.add(ProspectAccountRow(tenant_id=u.tenant, account_id="acct_controlled", name="Buyer",
            country="US", source_signal_refs=[], created_at=NOW))
        session.add(OpportunityRow(tenant_id=u.tenant, opportunity_id="opp_context",
            account_id="acct_controlled", account_name="Buyer", country="US", need_id=u.need_id,
            product_category="hinges", state="qualified", owner="emp_owner", created_at=NOW))

    class ControlledIssuer:
        async def get_confirmed(self, tenant_id: TenantId):
            assert tenant_id == u.tenant
            return issuer()

    return ContextCase(u, provider_type(u.sessions, ControlledIssuer(), lock_timeout_ms=1000,
        statement_timeout_ms=2500), u.tenant, u.actor_id, OpportunityId("opp_context"), u.actor_id, 500)


async def test_context_does_not_lock_after_scope_exit(context_case: ContextCase) -> None:
    c = context_case
    async with c.provider.open(c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.prepared_by) as context:
        assert context.need_facts.quantity.value == c.quantity
        assert context.runtime.current_actor.employee_id == c.actor_id
        assert "source_quote" in context.need_facts.model_dump_json()
    await c.update_owner_manager()


@pytest.mark.parametrize("employee_id,changes", [("emp_owner", {"manager_id": "emp_manager"}),
    ("emp_test", {"is_active": False}), ("emp_test", {"role": "finance"})])
async def test_context_blocks_real_employee_update(context_case: ContextCase, employee_id: str,
                                                  changes: dict[str, object]) -> None:
    c = context_case
    async with c.provider.open(c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.prepared_by):
        writer = asyncio.create_task(c.update_employee(EmployeeId(employee_id), **changes))
        await c.wait_for_blocked_writer(writer)
        assert not writer.done()
    await asyncio.wait_for(writer, 3)
    if not changes.get("is_active", True):
        return
    async with c.provider.open(c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.prepared_by) as context:
        target = context.runtime.owner if employee_id == "emp_owner" else context.runtime.current_actor
        for key, value in changes.items():
            assert getattr(target, key) == value


@pytest.mark.parametrize("field,value", [("quantity", 600), ("material", "brass"),
    ("size_spec", "60 mm"), ("packaging", "box"), ("required_by", "2026-10-01")])
async def test_context_blocks_real_need_updates(context_case: ContextCase, field: str, value: object) -> None:
    c = context_case
    async with c.provider.open(c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.prepared_by) as before:
        writer = asyncio.create_task(c.unit.demand.update_need_fields(c.tenant_id, c.unit.need_id,
            {field: {"value": value, "quote": str(value), "extracted_by": c.actor_id}},
            source_message_id="msg_customer_changed", updated_by=c.actor_id))
        await c.wait_for_blocked_writer(writer)
        assert not writer.done()
    await asyncio.wait_for(writer, 3)
    if field != "quantity":
        async with c.provider.open(c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.prepared_by) as after:
            assert after.need_facts_hash != before.need_facts_hash


async def test_two_contexts_parallel_and_actor_owner_deduplicated(context_case: ContextCase) -> None:
    c = context_case
    async with c.unit.sessions.begin() as session:
        await session.execute(update(OpportunityRow).where(OpportunityRow.tenant_id == c.tenant_id,
            OpportunityRow.opportunity_id == c.opportunity_id).values(owner=c.actor_id))
    async with c.provider.open(c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.prepared_by):
        async with asyncio.timeout(1):
            async with c.provider.open(c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.prepared_by) as context:
                assert context.owner_id == c.actor_id


async def test_cancel_releases_context_locks(context_case: ContextCase) -> None:
    c = context_case
    entered = asyncio.Event()
    async def lease():
        async with c.provider.open(c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.prepared_by):
            entered.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(lease())
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(c.update_owner_manager(), 2)


async def test_real_assign_blocks_under_lease(context_case: ContextCase) -> None:
    c = context_case
    async with c.provider.open(c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.prepared_by):
        writer = asyncio.create_task(c.assign(c.actor_id))
        await c.wait_for_blocked_writer(writer)
    await asyncio.wait_for(writer, 3)
    async with c.provider.open(c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.prepared_by) as current:
        assert current.owner_id == c.actor_id


async def test_assign_after_bootstrap_is_context_changed(context_case: ContextCase) -> None:
    c = context_case
    from domains.quotations.errors import QuoteContextError
    from infra.db.quote_context import SqlAlchemyQuoteContextProvider
    class ChangedIssuer:
        async def get_confirmed(self, tenant_id: TenantId):
            await c.assign(c.actor_id)
            return issuer()
    provider = SqlAlchemyQuoteContextProvider(c.unit.sessions, ChangedIssuer(),
        lock_timeout_ms=1000, statement_timeout_ms=2500)
    with pytest.raises(QuoteContextError) as error:
        async with provider.open(c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.prepared_by):
            pytest.fail("旧bootstrap不得继续使用")
    assert error.value.code == "context_changed"
    await c.update_owner_manager()


async def test_fk_key_share_compatible_and_account_owner_is_distinct(context_case: ContextCase) -> None:
    c = context_case
    from infra.db.tables import OwnershipLockRow
    async with c.provider.open(c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.prepared_by) as context:
        async with asyncio.timeout(1):
            async with c.unit.sessions.begin() as session:
                session.add(OwnershipLockRow(lock_id="lock_test", tenant_id=c.tenant_id,
                    account_id=context.account_id, owner=c.actor_id, locked_at=NOW, locked_by_rule="manual"))
        assert context.owner_id == "emp_owner"


@pytest.mark.parametrize("change", [{"tenant_id": "tn_other"}, {"account_id": "acct_wrong"}, {"owner": "emp_missing"}])
async def test_context_rejects_missing_cross_tenant_or_account_mismatch(context_case: ContextCase,
                                                                      change: dict[str, str]) -> None:
    c = context_case
    from domains.quotations.errors import QuoteContextError
    async with c.unit.sessions.begin() as session:
        await session.execute(update(OpportunityRow).where(OpportunityRow.tenant_id == c.tenant_id,
            OpportunityRow.opportunity_id == c.opportunity_id).values(**change))
    with pytest.raises(QuoteContextError):
        async with c.provider.open(c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.prepared_by):
            pytest.fail("错误关联不能进入报价准备")


async def test_context_lock_timeout_closes_transaction(context_case: ContextCase) -> None:
    c = context_case
    from domains.quotations.errors import QuoteContextUnavailableError
    async with c.unit.sessions.begin() as holder:
        await holder.execute(select(EmployeeRow).where(EmployeeRow.tenant_id == c.tenant_id,
            EmployeeRow.employee_id == c.actor_id).with_for_update())
        with pytest.raises(QuoteContextUnavailableError) as error:
            async with c.provider.open(c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.prepared_by):
                pytest.fail("被写锁阻塞的读取不得通过")
        assert error.value.code == "lock_timeout"
    await asyncio.wait_for(c.update_owner_manager(), 2)


async def test_same_quantity_new_source_changes_hash(context_case: ContextCase) -> None:
    c = context_case
    from domains.demand.errors import NeedUnitError
    from domains.demand.service import require_current_unit
    async with c.provider.open(c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.prepared_by) as before:
        pass
    await c.unit.change_quantity(500)
    async with c.provider.open(c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.prepared_by) as after:
        assert before.need_facts_hash != after.need_facts_hash
        with pytest.raises(NeedUnitError) as error:
            require_current_unit(after.need_facts)
        assert error.value.code == "unit_stale"
