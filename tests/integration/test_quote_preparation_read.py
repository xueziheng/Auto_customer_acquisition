"""真实PG准备租约：允许首次缺项，但不放宽原完整context或产生写入。"""

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from domains.quotations.errors import (
    QuotationError,
    QuoteContextError,
    QuoteContextUnavailableError,
)
from domains.quotations.service import (
    QuotePreparationReadServiceImpl,
    StrictQuotePreparationPolicy,
)
from infra.db.quote_context import SqlAlchemyQuoteContextProvider
from infra.db.tables import (
    EmployeeRow,
    OpportunityRow,
    ProspectAccountRow,
    ValidatedNeedRow,
)
from tests.integration.test_need_units import NOW
from tests.integration.test_quotations import (
    quotation_case as quotation_case,  # noqa: PLC0414 - 保持公开类型或测试fixture身份
)
from tests.integration.test_quote_context_locks import ContextCase
from tests.integration.test_quote_context_locks import (
    context_case as context_case,  # noqa: PLC0414 - 保持公开类型或测试fixture身份
)
from tests.integration.test_quote_context_locks import (
    unit_db_case as unit_db_case,  # noqa: PLC0414 - 保持公开类型或测试fixture身份
)
from tests.integration.test_quote_context_locks import (
    unit_engine as unit_engine,  # noqa: PLC0414 - 保持公开类型或测试fixture身份
)
from tests.integration.test_quote_cost_lock import (
    freeze_case as freeze_case,  # noqa: PLC0414 - 保持公开类型或测试fixture身份
)
from tests.unit.test_quote_context_contracts import issuer
from workflows.quote_approval.preparation_facts import DemandQuotePreparationProjector


@pytest_asyncio.fixture
async def first_preparation_case(unit_db_case):
    """从未确认单位的Need开始；不能清除真实不可变receipt来模拟首次。"""
    u = unit_db_case
    await u.demand.update_need_fields(
        u.tenant,
        u.need_id,
        {
            "material": {
                "value": "steel",
                "quote": "steel",
                "extracted_by": u.actor_id,
            },
            "destination": {"value": "US", "quote": "US", "extracted_by": u.actor_id},
        },
        source_message_id="msg_customer_1",
        updated_by=u.actor_id,
    )
    async with u.sessions.begin() as session:
        for eid, role in [(u.actor_id, "boss"), ("emp_owner", "sales")]:
            session.add(
                EmployeeRow(
                    tenant_id=u.tenant,
                    employee_id=eid,
                    name=eid,
                    role=role,
                    is_active=True,
                    created_at=NOW,
                )
            )
        session.add(
            ProspectAccountRow(
                tenant_id=u.tenant,
                account_id="acct_controlled",
                name="Buyer",
                country="US",
                source_signal_refs=[],
                created_at=NOW,
            )
        )
        session.add(
            OpportunityRow(
                tenant_id=u.tenant,
                opportunity_id="opp_context",
                account_id="acct_controlled",
                account_name="Buyer",
                country="US",
                need_id=u.need_id,
                product_category="hinges",
                state="qualified",
                owner="emp_owner",
                created_at=NOW,
            )
        )

    class Issuer:
        async def get_confirmed(self, tenant_id):
            return issuer()

    provider = SqlAlchemyQuoteContextProvider(
        u.sessions, Issuer(), lock_timeout_ms=1000, statement_timeout_ms=2500
    )
    return ContextCase(
        u, provider, u.tenant, u.actor_id, "opp_context", u.actor_id, 500
    )


def reader(c):
    assert hasattr(c.provider, "open_preparation_facts"), "缺少真实PG准备事实租约"
    return QuotePreparationReadServiceImpl(
        c.provider,
        StrictQuotePreparationPolicy(),
        DemandQuotePreparationProjector(),
        now=lambda: NOW,
    )


async def test_preparation_pg_matches_original_context_hash(context_case):
    c = context_case
    service = reader(c)
    actual = await service.get(c.tenant_id, c.opportunity_id, actor_id=c.actor_id)
    async with c.provider.open(
        c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.actor_id
    ) as context:
        assert actual.context_hash == context.context_hash
    assert actual.blockers == ()


async def test_preparation_pg_first_unit_and_issuer_missing(first_preparation_case):
    c = first_preparation_case
    reader(c)

    class MissingIssuer:
        async def get_confirmed(self, tenant_id):
            raise QuotationError("issuer_not_found")

    c.provider = SqlAlchemyQuoteContextProvider(
        c.unit.sessions,
        MissingIssuer(),
        lock_timeout_ms=1000,
        statement_timeout_ms=2500,
    )
    result = await reader(c).get(c.tenant_id, c.opportunity_id, actor_id=c.actor_id)
    assert [(b.field, b.code) for b in result.blockers] == [
        ("unit", "unit_missing"),
        ("issuer", "issuer_missing"),
    ]
    assert result.need.quantity == 500
    assert result.quantity_fact_hash is not None and result.context_hash is None
    with pytest.raises((QuoteContextError, QuoteContextUnavailableError)):
        async with c.provider.open(
            c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.actor_id
        ):
            pytest.fail("正式context仍应拒绝缺项")
    async with c.unit.sessions() as session:
        row = await session.scalar(
            select(ValidatedNeedRow).where(
                ValidatedNeedRow.tenant_id == c.tenant_id,
                ValidatedNeedRow.need_id == c.unit.need_id,
            )
        )
        assert row.unit is None and row.unit_confirmation_id is None


async def test_preparation_pg_same_employee_opportunity_need_lock_lease(context_case):
    c = context_case
    reader(c)
    async with c.provider.open_preparation_facts(
        c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.actor_id
    ):
        writer = asyncio.create_task(c.update_owner_manager())
        await c.wait_for_blocked_writer(writer)
        assert not writer.done()
    await asyncio.wait_for(writer, 3)


async def test_preparation_pg_corrupt_required_specification_is_not_missing_blocker(
    context_case,
):
    c = context_case
    reader(c)
    async with c.unit.sessions.begin() as session:
        row = await session.scalar(
            select(ValidatedNeedRow).where(
                ValidatedNeedRow.tenant_id == c.tenant_id,
                ValidatedNeedRow.need_id == c.unit.need_id,
            )
        )
        invalid = {**row.product_category, "value": " hinges "}
        await session.execute(
            update(ValidatedNeedRow)
            .where(
                ValidatedNeedRow.tenant_id == c.tenant_id,
                ValidatedNeedRow.need_id == c.unit.need_id,
            )
            .values(product_category=invalid)
        )
    with pytest.raises(QuoteContextError) as error:
        await reader(c).get(c.tenant_id, c.opportunity_id, actor_id=c.actor_id)
    assert error.value.code == "facts_corrupt"


async def test_coverage_refresh_reads_exact_historical_hash_after_new_confirmation(
    freeze_case,
):
    from domains.costing.schemas import CostCoverageCreate

    c = freeze_case
    assert hasattr(c.t2, "get_coverage"), "缺少精确hash/最新费用清单刷新口"
    first = await c.t2.get_coverage(c.tenant_id, c.cost_sheet_id, actor=c.actor)
    assert first is not None
    decisions = list(first.decisions)
    decisions[1] = decisions[1].model_copy(
        update={"reason": "confirmed not applicable for revised review"}
    )
    command = CostCoverageCreate(
        expected_sheet_hash=first.expected_sheet_hash,
        decisions=tuple(decisions),
        acquisition_mode=first.acquisition_mode,
    )
    newer_hash = await c.t2.confirm_coverage(
        c.tenant_id,
        c.cost_sheet_id,
        command,
        actor=c.actor,
        idempotency_key="coverage-refresh-new",
    )
    assert newer_hash != first.content_hash
    recovered = await c.t2.get_coverage(
        c.tenant_id, c.cost_sheet_id, actor=c.actor, content_hash=first.content_hash
    )
    assert recovered == first
    assert (
        await c.t2.confirm_coverage(
            c.tenant_id,
            c.cost_sheet_id,
            command,
            actor=c.actor,
            idempotency_key="coverage-refresh-new",
        )
        == newer_hash
    )
    assert (
        await c.t2.get_coverage(
            c.tenant_id, c.cost_sheet_id, actor=c.actor, content_hash=newer_hash
        )
    ).content_hash == newer_hash


async def test_scope_refresh_discovers_immutable_history_in_stable_order(freeze_case):
    from datetime import timedelta

    c = freeze_case
    assert hasattr(c.service, "list_scopes"), "缺少同成本表历史scope发现口"
    assert (
        await c.service.list_scopes(c.tenant_id, c.cost_sheet_id, actor=c.actor) == ()
    )
    first = await c.scope()
    second = await c.scope(
        key="scope-second",
        command=c.scope_command.model_copy(
            update={"valid_until": c.scope_command.valid_until - timedelta(hours=1)}
        ),
    )
    result = await c.service.list_scopes(c.tenant_id, c.cost_sheet_id, actor=c.actor)
    assert result == tuple(
        sorted(
            (first, second),
            key=lambda s: (s.provenance.confirmed_at, s.confirmation_id),
        )
    )
    assert await c.count("scope") == 2 and await c.count("basis") == 0


async def test_issuer_refresh_is_authorized_and_null_only_when_absent(quotation_case):
    from domains.quotations.errors import QuotationPermissionError
    from domains.quotations.schemas import QuotationActor

    c = quotation_case
    assert hasattr(c.service, "get_issuer"), "缺少带当前actor保护的抬头刷新口"
    assert await c.service.get_issuer(c.tenant, actor=c.actor) is None
    first = await c.issuer()
    value = await c.service.get_issuer(c.tenant, actor=c.actor)
    assert value.issuer_id == first.issuer_id
    assert "source_quote" not in value.model_dump_json()
    await c.context.update_employee(c.context.actor_id, role="sales")
    with pytest.raises(QuotationPermissionError):
        await c.service.get_issuer(
            c.tenant, actor=QuotationActor(employee_id=c.context.actor_id, role="sales")
        )
