"""审批持久请求真实PG验收；员工/报价/run完整闭环另由同模块逐片扩展。"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from domains.approvals.errors import QuoteContractError
from domains.approvals.service_impl import ApprovalServiceImpl
from infra.db.approval_uow import SqlAlchemyApprovalUnitOfWork
from infra.db.tables import ApprovalPackageRow
from tests.integration.test_need_units import migrate
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414
)
from tests.unit.test_approval_service import QuoteAccessCase, submit_quote
from tests.unit.test_quotation_contracts import basis_case


async def test_quote_submit_concurrency_original_limit_and_immutable_decision(
    unit_engine,
):
    sessions = async_sessionmaker(unit_engine, expire_on_commit=False)
    clock = [basis_case()[3]]
    factory = lambda tenant: SqlAlchemyApprovalUnitOfWork(
        sessions, tenant, now=lambda: clock[0]
    )
    service = ApprovalServiceImpl(
        factory, quote_access=QuoteAccessCase(), now=lambda: clock[0]
    )
    first, second = await asyncio.gather(submit_quote(service), submit_quote(service))
    payload, approval_id = first
    assert first[1] == second[1]
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ApprovalPackageRow)
                .where(ApprovalPackageRow.tenant_id == payload.tenant_id)
            )
            == 1
        )
    before = await service.read_fact(payload.tenant_id, approval_id)
    assert before.expires_at_limit == payload.customer.valid_until
    assert before.expires_at == min(
        before.created_at + timedelta(days=2), before.expires_at_limit
    )
    with pytest.raises(QuoteContractError) as error:
        await submit_quote(
            service, limit=payload.customer.valid_until + timedelta(days=1)
        )
    assert error.value.code == "quote_request_conflict"
    async with sessions.begin() as session:
        await session.execute(
            text("""UPDATE approval_packages SET state='approved',
            decided_by='emp_decider',decided_at=:now WHERE tenant_id=:tenant AND approval_id=:approval"""),
            {"now": clock[0], "tenant": payload.tenant_id, "approval": approval_id},
        )
    await service.mark_applied(payload.tenant_id, approval_id, "quote-test-application")
    clock[0] += timedelta(days=30)
    assert (await submit_quote(service))[1] == approval_id
    after = await service.read_fact(payload.tenant_id, approval_id)
    assert after.request_hash == before.request_hash
    assert after.state.value == "applied"
    for assignment in (
        "decision_note='changed'",
        "request_hash=repeat('b',64)",
        "expires_at_limit=expires_at_limit+interval '1 day'",
    ):
        async with sessions.begin() as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    text(
                        f"UPDATE approval_packages SET {assignment} WHERE tenant_id=:tenant AND approval_id=:approval"
                    ),
                    {"tenant": payload.tenant_id, "approval": approval_id},
                )
    assert migrate(unit_engine, "downgrade", "0044") != 0


async def test_real_engine_reader_uses_mvcc_while_handler_holds_run_lock(unit_engine):
    from infra.db.workflow_engine import PostgresWorkflowEngine
    from shared.schemas.identifiers import TenantId
    from workflows.engine.runner import StepDefinition, WorkflowDefinition

    assert hasattr(PostgresWorkflowEngine, "get_run"), "缺少真实workflow get_run读口"
    sessions = async_sessionmaker(unit_engine, expire_on_commit=False)
    observed = []

    class Handler:
        async def execute(self, run):
            async with asyncio.timeout(1):
                stored = await engine.get_run(run.tenant_id, run.run_id)
            observed.append(stored.context)
            return ("complete", None, {})

    engine = PostgresWorkflowEngine(sessions, {"test": Handler()})
    engine.register(
        WorkflowDefinition("quote_approval", 1, (StepDefinition("test", "test"),))
    )
    tenant = TenantId("tn_test")
    run_id = await engine.start(
        tenant,
        "quote_approval",
        "quo_01K00000000000000000000000",
        {"quote_version": 1, "content_hash": "a" * 64},
        "quote-reader-test",
    )
    assert await engine.get_run(TenantId("other"), run_id) is None
    assert await engine.poll_due(tenant, 1) == 1
    assert observed == [{"quote_version": 1, "content_hash": "a" * 64}]
    assert (await engine.get_run(tenant, run_id)).status.value == "completed"
