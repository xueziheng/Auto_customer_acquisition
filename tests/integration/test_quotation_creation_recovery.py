"""真实冻结/报价/完成reader，多连接与进程对象重建；外部来源仅受控。"""

import asyncio
import importlib
from dataclasses import dataclass
from datetime import timedelta

import pytest
import pytest_asyncio

from domains.quotations import schemas as q
from domains.quotations import service as public
from tests.integration.test_quotations import NoSend
from tests.integration.test_quotations import (
    context_case as context_case,  # noqa: PLC0414 -- pytest跨文件fixture显式导出
)
from tests.integration.test_quotations import (
    freeze_case as freeze_case,  # noqa: PLC0414 -- pytest跨文件fixture显式导出
)
from tests.integration.test_quotations import (
    prepared_quote as prepared_quote,  # noqa: PLC0414 -- pytest跨文件fixture显式导出
)
from tests.integration.test_quotations import (
    quotation_case as quotation_case,  # noqa: PLC0414 -- pytest跨文件fixture显式导出
)
from tests.integration.test_quotations import (
    unit_db_case as unit_db_case,  # noqa: PLC0414 -- pytest跨文件fixture显式导出
)
from tests.integration.test_quotations import (
    unit_engine as unit_engine,  # noqa: PLC0414 -- pytest跨文件fixture显式导出
)
from tests.unit.test_quotation_service import (
    UnusedApprovalContext,
    UnusedApprovalPolicy,
    UnusedWorkflowRunReader,
)


@dataclass
class RecoveryCase:
    quotation: object
    freeze: object
    app: object
    command: object

    async def create(self, key="recovery"):
        return await self.app.create(
            self.quotation.tenant,
            self.command,
            actor_id=self.quotation.actor.employee_id,
            idempotency_key=key,
        )

    async def operation(self, key="recovery"):
        return await self.freeze.service.get_creation(
            self.quotation.tenant, key, actor=self.freeze.actor
        )

    def rebuild(self, *, forbid_context=False):
        from infra.db.quote_context import SqlAlchemyQuoteContextProvider
        from workflows.quote_approval.application import QuoteApplicationService
        from workflows.quote_approval.completion_reader import (
            PersistentQuoteCreationCompletionReader,
        )
        from workflows.quote_approval.issuer_reader import PersistentQuoteIssuerReader

        c, f = self.quotation, self.freeze
        c.service = type(c.service)(
            c.factory,
            c.actors,
            public.StrictQuotePreparationPolicy(),
            NoSend(),
            context_provider=UnusedApprovalContext(),
            approval_policy_reader=UnusedApprovalPolicy(),
            workflow_run_reader=UnusedWorkflowRunReader(),
            now=lambda: c.clock[0],
        )
        previous = f.service
        f.service = type(previous)(
            previous._factory,
            previous._actors,
            previous._need,
            previous._sources,
            PersistentQuoteCreationCompletionReader(c.service, c.actors),
            now=lambda: c.clock[0],
        )
        provider = SqlAlchemyQuoteContextProvider(
            c.context.unit.sessions,
            PersistentQuoteIssuerReader(c.service),
            lock_timeout_ms=1500,
            statement_timeout_ms=3000,
        )
        if forbid_context:

            class NoContext:
                def open(self, *args, **kwargs):
                    raise AssertionError("恢复已commit报价不能读取当前context")

            provider = NoContext()
        self.app = QuoteApplicationService(
            provider,
            f.service,
            c.service,
            c.actors,
            public.StrictQuotePreparationPolicy(),
            now=lambda: c.clock[0],
        )


@pytest_asyncio.fixture
async def recovery_case(prepared_quote):
    c, f = prepared_quote
    assert importlib.util.find_spec("workflows.quote_approval.completion_reader"), (
        "缺少真实quotation completion reader"
    )
    i = await f.intent()
    command = q.QuoteDraftCommand(
        **{n: getattr(i, n) for n in q.QuoteDraftCommand.model_fields}
    )
    case = RecoveryCase(c, f, None, command)
    case.rebuild()
    return case


async def test_actual_quote_completes_actual_frozen_operation(recovery_case):
    r = recovery_case
    quote = await r.create()
    operation = await r.operation()
    assert operation.state == "completed"
    assert operation.completion.quote_id == quote.content.quote_id
    assert operation.completion.quote_content_hash == quote.content.content_hash
    assert await r.create() == quote


async def test_recover_committed_quote_before_rechecking_context(
    recovery_case, monkeypatch
):
    from domains.costing.errors import CostFreezeUnavailableError

    r = recovery_case

    async def fail(*args, **kwargs):
        raise CostFreezeUnavailableError("dependency_unavailable")

    monkeypatch.setattr(r.freeze.service, "complete_creation", fail)
    with pytest.raises(CostFreezeUnavailableError):
        await r.create()
    operation = await r.operation()
    original = await r.quotation.service.get_by_operation(
        r.quotation.tenant, operation.operation_id, actor=r.quotation.actor
    )
    assert original is not None and operation.state == "frozen"
    await r.freeze.change_material()
    await r.quotation.issuer(key="changed", name="New Supplier Name")
    r.quotation.clock[0] = original.content.valid_until + timedelta(seconds=1)
    r.rebuild(forbid_context=True)
    recovered = await r.create()
    assert recovered.content == original.content
    assert (await r.operation()).completion.quote_id == original.content.quote_id
    assert (
        len(
            await r.quotation.service.list_versions(
                r.quotation.tenant, r.command.opportunity_id, actor=r.quotation.actor
            )
        )
        == 1
    )


async def test_frozen_before_quote_failure_resumes_same_key(recovery_case, monkeypatch):
    r = recovery_case
    repo = importlib.import_module(
        "infra.db.repositories.quotations"
    ).QuotationVersionRepositoryImpl
    original = repo.add

    async def fail(*args):
        raise RuntimeError("controlled quote write failure")

    monkeypatch.setattr(repo, "add", fail)
    with pytest.raises(RuntimeError):
        await r.create()
    operation = await r.operation()
    assert operation.state == "frozen"
    assert await r.freeze.locked_at() is not None
    monkeypatch.setattr(repo, "add", original)
    r.rebuild()
    from domains.costing.errors import CostFreezeError

    with pytest.raises(CostFreezeError) as error:
        await r.app.create(
            r.quotation.tenant,
            r.command,
            actor_id=r.quotation.actor.employee_id,
            idempotency_key="changed-key",
        )
    assert error.value.code == "operation_pending"
    quote = await r.create()
    assert quote.content.operation_id == operation.operation_id
    assert (await r.operation()).state == "completed"


async def test_unknown_commit_is_recovered_from_real_record(recovery_case, monkeypatch):
    from domains.quotations.errors import QuotationUnavailableError
    from infra.db.quotation_uow import SqlAlchemyQuotationUow

    r = recovery_case
    original = SqlAlchemyQuotationUow.commit

    async def unknown(self):
        await original(self)
        raise QuotationUnavailableError("storage_unknown")

    monkeypatch.setattr(SqlAlchemyQuotationUow, "commit", unknown)
    with pytest.raises(QuotationUnavailableError) as e:
        await r.create()
    assert e.value.code == "storage_unknown"
    monkeypatch.setattr(SqlAlchemyQuotationUow, "commit", original)
    r.rebuild(forbid_context=True)
    quote = await r.create()
    assert (await r.operation()).completion.quote_id == quote.content.quote_id


async def test_same_key_both_initially_missing_recheck_after_opportunity_lock(
    recovery_case, monkeypatch
):
    r = recovery_case
    original = r.freeze.service.get_creation
    reads = 0
    gate = asyncio.Event()

    async def synchronized(*args, **kwargs):
        nonlocal reads
        value = await original(*args, **kwargs)
        reads += 1
        if reads <= 2:
            assert value is None
            if reads == 2:
                gate.set()
            await gate.wait()
        return value

    monkeypatch.setattr(r.freeze.service, "get_creation", synchronized)
    one, two = await asyncio.wait_for(asyncio.gather(r.create(), r.create()), 8)
    assert one == two
    assert await r.freeze.count("basis") == 1
    assert await r.freeze.count("operation") == 1


async def new_command(r, key, *, previous=None):
    """每个竞争者有真实独立cost sheet/coverage/scope，绝不复制假父行。"""
    from decimal import Decimal

    from domains.costing import schemas as cost
    from domains.costing import service as cost_public
    from domains.costing.permissions import Phase1CostingAuthorizer
    from shared.schemas.identifiers import CostSheetId

    c, f = r.quotation, r.freeze
    old = importlib.import_module("domains.costing.service_impl").CostingServiceImpl(
        f.factory, authorizer=Phase1CostingAuthorizer(c.tenant), now=lambda: c.clock[0]
    )
    sheet_id = CostSheetId(
        await old.create_sheet(
            c.tenant,
            r.command.opportunity_id,
            cost.CostSheetCreate(
                version_type="quoted",
                quantity=500,
                base_currency="USD",
                quote_currency="USD",
                fx_snapshot_id="fx_cost_test",
                fx_rates=[],
            ),
            actor=f.actor,
        )
    )
    await old.add_item(
        c.tenant,
        sheet_id,
        cost.CostItemCreate(
            item_type="product_purchase",
            amount=Decimal("1.25"),
            currency="USD",
            price_basis="quoted",
            is_per_unit=True,
            source_ref=f.evidence.evidence_id,
            note=None,
        ),
        actor=f.actor,
    )
    sheet = await old.get_sheet(c.tenant, sheet_id, actor=f.actor)
    coverage = await f.t2.confirm_coverage(
        c.tenant,
        sheet_id,
        cost.CostCoverageCreate(
            expected_sheet_hash=sheet.content_hash,
            acquisition_mode="detail",
            decisions=tuple(
                cost.CostCoverageDecision(
                    item_type=name,
                    applicable=name == "product_purchase",
                    reason="人工核对",
                    item_bindings=(
                        cost.CostItemBinding(
                            item_sequence=1,
                            evidence_id=f.evidence.evidence_id,
                            source_line_ref=f.evidence.locator,
                            allocation_scope="order:one",
                        ),
                    )
                    if name == "product_purchase"
                    else (),
                )
                for name in cost_public.cost_item_type_values()
            ),
        ),
        actor=f.actor,
        idempotency_key=key + "-coverage",
    )
    scope = await f.application.confirm_scope(
        c.tenant,
        r.command.opportunity_id,
        sheet_id,
        f.scope_command.model_copy(
            update={
                "coverage_id": coverage,
                "expected_coverage_hash": coverage,
                "expected_sheet_hash": sheet.content_hash,
                "valid_until": c.clock[0] + timedelta(days=1),
            }
        ),
        actor_id=c.actor.employee_id,
        idempotency_key=key + "-scope",
    )
    intent = await f.intent(
        scope=scope,
        cost_sheet_id=sheet_id,
        replaces_quote_id=previous.content.quote_id if previous else None,
        expected_quote_version=previous.content.version if previous else None,
    )
    return q.QuoteDraftCommand(
        **{n: getattr(intent, n) for n in q.QuoteDraftCommand.model_fields}
    )


@pytest.mark.parametrize("revision", [False, True])
async def test_losing_sheet_has_not_frozen_at_first_or_revision_race(
    recovery_case, revision
):
    from sqlalchemy import text

    from domains.quotations.errors import QuotationError

    r = recovery_case
    previous = await r.create("original") if revision else None
    commands = [
        await new_command(r, "racer-one", previous=previous),
        await new_command(r, "racer-two", previous=previous),
    ]
    gate = asyncio.Event()

    async def run(index):
        await gate.wait()
        try:
            return await r.app.create(
                r.quotation.tenant,
                commands[index],
                actor_id=r.quotation.actor.employee_id,
                idempotency_key=f"racer-{index}",
            )
        except QuotationError as e:
            return e

    tasks = [asyncio.create_task(run(index)) for index in range(2)]
    gate.set()
    result = await asyncio.wait_for(asyncio.gather(*tasks), 8)
    winners = [item for item in result if isinstance(item, q.QuoteDetailView)]
    assert len(winners) == 1
    loser = next(
        index for index, item in enumerate(result) if isinstance(item, QuotationError)
    )
    assert result[loser].code == (
        "revision_conflict" if revision else "active_quote_exists"
    )
    assert await r.operation(f"racer-{loser}") is None
    async with r.quotation.context.unit.sessions() as session:
        assert (
            await session.scalar(
                text(
                    "SELECT locked_at FROM cost_sheets WHERE tenant_id=:tenant AND cost_sheet_id=:sheet"
                ),
                {"tenant": r.quotation.tenant, "sheet": commands[loser].cost_sheet_id},
            )
            is None
        )
    assert winners[0].content.version == (2 if revision else 1)


async def change_state(c, quote, target):
    """受控状态准备只验证报价持久矩阵，不声称T5审批已装配。"""
    from shared.schemas.identifiers import new_id

    async with c.factory(c.tenant) as uow:
        await uow.quotes.lock_opportunity(c.tenant, quote.content.opportunity_id)
        current = await uow.quotes.get(
            c.tenant, quote.content.quote_id, for_update=True
        )
        event = q.QuoteStateEvent(
            event_id=new_id("qev"),
            quote_id=current.content.quote_id,
            from_state=current.state,
            to_state=q.QuoteState(target),
            actor_id=c.actor.employee_id,
            reason="revision",
            at=c.clock[0],
            reference_id=None,
        )
        assert await uow.quotes.transition(
            c.tenant,
            current.content.quote_id,
            current.state,
            q.QuoteState(target),
            event,
        )
        await uow.commit()


@pytest.mark.parametrize("materialized_expiry", [False, True])
async def test_e2_latest_expired_keeps_old_expired_and_creates_version(
    recovery_case, materialized_expiry
):
    r = recovery_case
    old = await r.create()
    r.quotation.clock[0] = old.content.valid_until + timedelta(seconds=1)
    if materialized_expiry:
        await change_state(r.quotation, old, "expired")
    command = await new_command(r, "e2", previous=old)
    new = await r.app.create(
        r.quotation.tenant,
        command,
        actor_id=r.quotation.actor.employee_id,
        idempotency_key="e2",
    )
    assert new.content.version == 2
    assert new.content.replaced_quote_version == 1
    assert (
        await r.quotation.service.get(
            r.quotation.tenant, old.content.quote_id, actor=r.quotation.actor
        )
    ).state == q.QuoteState.EXPIRED


@pytest.mark.parametrize("terminal", ["accepted", "rejected"])
async def test_terminal_quote_allows_explicit_new_fact_without_replacing(
    recovery_case, terminal
):
    from domains.quotations.errors import QuotationError

    r = recovery_case
    old = await r.create()
    for state in (
        ["pending_approval", "approved", "sent", "accepted"]
        if terminal == "accepted"
        else ["pending_approval", "rejected"]
    ):
        await change_state(r.quotation, old, state)
    with pytest.raises(QuotationError):
        await r.app.create(
            r.quotation.tenant,
            r.command,
            actor_id=r.quotation.actor.employee_id,
            idempotency_key="old-sheet-new-key",
        )
    command = await new_command(r, "terminal-new")
    new = await r.app.create(
        r.quotation.tenant,
        command,
        actor_id=r.quotation.actor.employee_id,
        idempotency_key="terminal-new",
    )
    assert new.content.version == 2 and new.content.replaces_quote_id is None
    operation = await r.operation("terminal-new")
    assert operation.state == "completed" and operation.completion.quote_version == 2
    assert (
        await r.app.create(
            r.quotation.tenant,
            command,
            actor_id=r.quotation.actor.employee_id,
            idempotency_key="terminal-new",
        )
        == new
    )
    assert (
        await r.quotation.service.get(
            r.quotation.tenant, old.content.quote_id, actor=r.quotation.actor
        )
    ).state.value == terminal


async def test_revision_transaction_rolls_back_previous_state_and_new_content(
    recovery_case, monkeypatch
):
    r = recovery_case
    old = await r.create()
    command = await new_command(r, "rollback-revision", previous=old)
    repo = importlib.import_module(
        "infra.db.repositories.quotations"
    ).QuotationVersionRepositoryImpl
    add = repo.add

    async def fail_after_insert(self, *args):
        await add(self, *args)
        raise RuntimeError("controlled revision rollback")

    monkeypatch.setattr(repo, "add", fail_after_insert)
    with pytest.raises(RuntimeError):
        await r.app.create(
            r.quotation.tenant,
            command,
            actor_id=r.quotation.actor.employee_id,
            idempotency_key="rollback-revision",
        )
    assert (
        await r.quotation.service.get(
            r.quotation.tenant, old.content.quote_id, actor=r.quotation.actor
        )
    ).state == q.QuoteState.DRAFT
    assert (
        len(
            await r.quotation.service.list_versions(
                r.quotation.tenant, old.content.opportunity_id, actor=r.quotation.actor
            )
        )
        == 1
    )
    assert (await r.operation("rollback-revision")).state == "frozen"


async def test_expiry_races_revision_without_overwriting_new_active(recovery_case):
    r = recovery_case
    old = await r.create()
    r.quotation.clock[0] = old.content.valid_until + timedelta(seconds=1)
    command = await new_command(r, "expiry-race", previous=old)
    quote, expired = await asyncio.gather(
        r.app.create(
            r.quotation.tenant,
            command,
            actor_id=r.quotation.actor.employee_id,
            idempotency_key="expiry-race",
        ),
        r.quotation.service.expire_overdue(r.quotation.tenant, limit=10),
    )
    assert expired in (0, 1)
    versions = await r.quotation.service.list_versions(
        r.quotation.tenant, old.content.opportunity_id, actor=r.quotation.actor
    )
    assert [v.state for v in versions] == [q.QuoteState.DRAFT, q.QuoteState.EXPIRED]
    assert versions[0] == quote and versions[0].content.version == 2
