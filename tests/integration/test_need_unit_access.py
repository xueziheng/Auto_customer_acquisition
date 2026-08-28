"""独立PG连接验证Employee→Opportunity授权租约，Need锁留给内层确认。"""

import asyncio
import importlib

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from domains.demand import service
from domains.opportunities.permissions import Phase1OpportunityAuthorizer
from infra.db.quote_evidence_context import SqlAlchemyQuoteEvidenceContextReader
from infra.db.tables import EmployeeRow, OpportunityRow, ValidatedNeedRow
from tests.integration.test_quote_context_locks import (
    context_case as context_case,  # noqa: PLC0414 - 保持公开类型或测试fixture身份
)
from tests.integration.test_quote_context_locks import (
    unit_db_case as unit_db_case,  # noqa: PLC0414 - 保持公开类型或测试fixture身份
)
from tests.integration.test_quote_context_locks import (
    unit_engine as unit_engine,  # noqa: PLC0414 - 保持公开类型或测试fixture身份
)
from tests.integration.test_quote_preparation_read import (
    first_preparation_case as first_preparation_case,  # noqa: PLC0414 - 保持公开类型或测试fixture身份
)


def authorizer(c):
    assert hasattr(service, "NeedUnitScopeReader"), "缺少单位授权真实范围租约"
    sql = importlib.import_module("infra.db.need_unit_scope")
    workflow = importlib.import_module("workflows.quote_approval.need_unit_access")
    return workflow.CurrentNeedUnitAuthorizer(
        SqlAlchemyQuoteEvidenceContextReader(
            c.unit.sessions, statement_timeout_ms=2500
        ),
        sql.SqlAlchemyNeedUnitScopeReader(
            c.unit.sessions, lock_timeout_ms=1000, statement_timeout_ms=2500
        ),
        Phase1OpportunityAuthorizer(c.tenant_id),
    )


async def test_unit_guard_preserves_unassigned_opportunity_access(
    first_preparation_case,
):
    c = first_preparation_case
    auth = authorizer(c)
    async with c.unit.sessions.begin() as session:
        await session.execute(
            update(OpportunityRow)
            .where(
                OpportunityRow.tenant_id == c.tenant_id,
                OpportunityRow.opportunity_id == c.opportunity_id,
            )
            .values(owner=None)
        )
    async with auth.guard(
        c.tenant_id, c.unit.need_id, c.actor_id, action="confirm"
    ) as access:
        assert (access.need_id, access.account_id) == (
            c.unit.need_id,
            "acct_controlled",
        )
        for model, predicate in (
            (EmployeeRow, EmployeeRow.employee_id == c.actor_id),
            (OpportunityRow, OpportunityRow.opportunity_id == c.opportunity_id),
        ):
            async with c.unit.sessions() as other:
                with pytest.raises(DBAPIError) as error:
                    await other.execute(
                        select(model)
                        .where(model.tenant_id == c.tenant_id, predicate)
                        .with_for_update(nowait=True)
                    )
                assert error.value.orig.sqlstate == "55P03"
        async with c.unit.sessions.begin() as other:
            need = await other.scalar(
                select(ValidatedNeedRow)
                .where(
                    ValidatedNeedRow.tenant_id == c.tenant_id,
                    ValidatedNeedRow.need_id == c.unit.need_id,
                )
                .with_for_update(nowait=True)
            )
            assert need.need_id == c.unit.need_id


async def test_unit_guard_does_not_require_active_owner(context_case):
    c = context_case
    auth = authorizer(c)
    await c.update_employee("emp_owner", is_active=False)
    result = await auth.check(c.tenant_id, c.unit.need_id, c.actor_id, action="read")
    assert result.actor_id == c.actor_id
    async with c.unit.sessions.begin() as other:
        await other.execute(text("SET LOCAL lock_timeout = '1000ms'"))
        await other.execute(
            update(EmployeeRow)
            .where(
                EmployeeRow.tenant_id == c.tenant_id,
                EmployeeRow.employee_id == c.actor_id,
            )
            .values(role="finance")
        )
    with pytest.raises(service.NeedUnitPermissionError):
        await auth.check(c.tenant_id, c.unit.need_id, c.actor_id, action="confirm")


async def _assert_authorization_rows_locked(c, expected):
    for model, predicate in (
        (EmployeeRow, EmployeeRow.employee_id == c.actor_id),
        (OpportunityRow, OpportunityRow.opportunity_id == c.opportunity_id),
    ):
        async with c.unit.sessions() as other:
            statement = (
                select(model)
                .where(model.tenant_id == c.tenant_id, predicate)
                .with_for_update(nowait=True)
            )
            if expected:
                with pytest.raises(DBAPIError) as error:
                    await other.execute(statement)
                assert error.value.orig.sqlstate == "55P03"
            else:
                assert (await other.execute(statement)).scalar_one() is not None


async def test_real_unit_service_commits_under_authorization_and_reads_sources_without_locks(
    first_preparation_case,
):
    from domains.demand.unit_service import NeedUnitServiceImpl
    from infra.db.need_unit_uow import SqlAlchemyNeedUnitUnitOfWork
    from tests.integration.test_need_units import NOW

    c = first_preparation_case
    observations = []

    class ObservedUow(SqlAlchemyNeedUnitUnitOfWork):
        async def __aexit__(self, exc_type, exc, tb):
            # 只在真实receipt INSERT发生的事务观察锁；不代替真实commit。
            if self._session.new:
                pytest.fail("原仓储应在返回前flush真实receipt")
            await super().__aexit__(exc_type, exc, tb)
            if await c.unit.confirmation_count():
                await _assert_authorization_rows_locked(c, True)
                observations.append("committed_under_guard")

    class Reader:
        async def read_verified(self, query):
            await _assert_authorization_rows_locked(c, False)
            observations.append("source_unlocked")
            return await c.unit.reader.read_verified(query)

        async def authorize_reference(self, tenant_id, need_id, actor_id, source):
            await _assert_authorization_rows_locked(c, False)
            observations.append("reference_unlocked")
            return await c.unit.reader.authorize_reference(
                tenant_id, need_id, actor_id, source
            )

    svc = NeedUnitServiceImpl(
        lambda tenant: ObservedUow(
            c.unit.sessions, tenant, lock_timeout_ms=1000, statement_timeout_ms=2500
        ),
        authorizer(c),
        Reader(),
        now=lambda: NOW,
    )
    result = await svc.confirm(
        c.tenant_id,
        c.unit.need_id,
        c.unit.command,
        actor_id=c.actor_id,
        idempotency_key="actual-unit-access",
    )
    assert result.unit.value == "pieces" and await c.unit.confirmation_count() == 1
    assert observations == ["source_unlocked", "committed_under_guard"]
    await _assert_authorization_rows_locked(c, False)


@pytest.mark.parametrize(
    "change,expected", [("missing", "need_not_found"), ("account", "facts_corrupt")]
)
async def test_unit_scope_rejects_missing_opportunity_and_wrong_account(
    first_preparation_case, change, expected
):
    c = first_preparation_case
    async with c.unit.sessions.begin() as session:
        await session.execute(
            update(OpportunityRow)
            .where(
                OpportunityRow.tenant_id == c.tenant_id,
                OpportunityRow.opportunity_id == c.opportunity_id,
            )
            .values(
                **(
                    {"need_id": "need_other"}
                    if change == "missing"
                    else {"account_id": "acct_wrong"}
                )
            )
        )
    with pytest.raises(service.NeedUnitError) as error:
        await authorizer(c).check(
            c.tenant_id, c.unit.need_id, c.actor_id, action="read"
        )
    assert error.value.code == expected
    await _assert_authorization_rows_locked(c, False)


async def test_unit_scope_real_lock_timeout_and_cancellation_release_connection(
    first_preparation_case,
):
    from infra.db.need_unit_scope import SqlAlchemyNeedUnitScopeReader

    c = first_preparation_case
    async with c.unit.sessions.begin() as locker:
        await locker.execute(
            select(EmployeeRow)
            .where(
                EmployeeRow.tenant_id == c.tenant_id,
                EmployeeRow.employee_id == c.actor_id,
            )
            .with_for_update()
        )
        scope = SqlAlchemyNeedUnitScopeReader(
            c.unit.sessions, lock_timeout_ms=100, statement_timeout_ms=1000
        )
        with pytest.raises(service.NeedUnitUnavailableError) as error:
            async with scope.open(c.tenant_id, c.unit.need_id, c.actor_id):
                pytest.fail("必须等待真实Employee锁")
        assert error.value.code == "lock_timeout"

        async def waiting():
            async with authorizer(c).guard(
                c.tenant_id, c.unit.need_id, c.actor_id, action="confirm"
            ):
                pytest.fail("必须等待真实Employee锁")

        task = asyncio.create_task(waiting())
        await c.wait_for_blocked_writer(task)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await _assert_authorization_rows_locked(c, False)
