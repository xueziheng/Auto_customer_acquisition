"""真实Gateway认领、独立PG预留与真实报价生成；无fake claim。"""

# ruff: noqa: PLC0414 -- 显式复用真实T4/T5/T6 fixtures
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update

from infra.db.quote_file_rate_limit import (
    PostgresQuoteFileExecutionHistoryReader,
    PostgresQuoteFileGenerationRateLimiter,
)
from infra.db.tables import OpportunityRow, ToolCallEventRow, ToolCallRow
from shared.schemas.identifiers import new_id
from tests.integration.test_quote_approval_postgres import bind_real_approvals
from tests.integration.test_quote_file_gateway import (
    approval_case as approval_case,
)
from tests.integration.test_quote_file_gateway import (
    context_case as context_case,
)
from tests.integration.test_quote_file_gateway import (
    file_gateway_case as file_gateway_case,
)
from tests.integration.test_quote_file_gateway import (
    freeze_case as freeze_case,
)
from tests.integration.test_quote_file_gateway import (
    prepared_quote as prepared_quote,
)
from tests.integration.test_quote_file_gateway import (
    quotation_case as quotation_case,
)
from tests.integration.test_quote_file_gateway import (
    recovery_case as recovery_case,
)
from tests.integration.test_quote_file_gateway import (
    unit_db_case as unit_db_case,
)
from tests.integration.test_quote_file_gateway import (
    unit_engine as unit_engine,
)
from tool_gateway.checks.quote_files import QuoteFileRateLimitCheck
from tool_gateway.file_rate_limit import QuoteFileRateLimits
from tool_gateway.pipeline import ToolGateway
from workflows.quote_approval.approvals import read_quote_facts
from workflows.quote_approval.files import QuoteFilesApplication


@pytest_asyncio.fixture
async def rate_case(file_gateway_case):
    c = file_gateway_case
    c.limits = QuoteFileRateLimits(
        maximum_admissions=2,
        window_seconds=60,
        lock_timeout_ms=2000,
        statement_timeout_ms=5000,
    )
    c.rate = PostgresQuoteFileGenerationRateLimiter(
        c.sessions, limits=c.limits, lease_owner="quote-files-test", id_generator=new_id
    )
    c.history = PostgresQuoteFileExecutionHistoryReader(
        c.sessions, statement_timeout_ms=5000
    )
    c.generate._history = c.history
    checks = dict(c.gateway._checks)
    checks["rate_limit"] = QuoteFileRateLimitCheck(c.rate, c.slot)
    c.gateway = ToolGateway(
        c.registry,
        checks,
        c.ledger_factory,
        lease_duration=timedelta(seconds=30),
        lease_owner="quote-files-test",
        now=lambda: datetime.now(UTC),
        id_factory=new_id,
    )
    c.app = QuoteFilesApplication(
        c.gateway,
        c.access,
        c.files,
        c.slot,
        c.ledger,
        c.fingerprints,
        generate_tool_version="v1",
    )
    return c


async def additional_approved_quote(c, suffix):
    """真实新机会/冻结/创建/审批，复用受控来源，不INSERT成功receipt。"""
    from dataclasses import replace
    from decimal import Decimal

    from domains.costing import schemas as cost_schemas
    from domains.costing.permissions import Phase1CostingAuthorizer
    from domains.costing.service import cost_item_type_values
    from domains.costing.service_impl import CostingServiceImpl
    from domains.demand.service import quantity_fact_hash
    from domains.quotations.schemas import QuoteDraftCommand, QuoteWorkflowExecutor
    from infra.db.tables import ValidatedNeedRow
    from tests.integration.test_need_units import ControlledAccess, ControlledReader

    a = c.approval
    q, r = a.quotation, a.creation
    opportunity = "opp_rate_" + suffix
    need = new_id("nd")
    u = q.context.unit
    async with c.sessions.begin() as db:
        original_need = await db.scalar(
            select(ValidatedNeedRow).where(
                ValidatedNeedRow.tenant_id == c.tenant_id,
                ValidatedNeedRow.need_id == u.need_id,
            )
        )
        values = {
            column.key: getattr(original_need, column.key)
            for column in ValidatedNeedRow.__table__.columns
            if column.key
            not in {
                "need_id",
                "unit",
                "unit_confirmation_id",
                "unit_quantity_fact_hash",
            }
        }
        db.add(ValidatedNeedRow(**values, need_id=need))
        old = await db.scalar(
            select(OpportunityRow).where(
                OpportunityRow.tenant_id == c.tenant_id,
                OpportunityRow.opportunity_id == a.quote.content.opportunity_id,
            )
        )
        db.add(
            OpportunityRow(
                tenant_id=c.tenant_id,
                opportunity_id=opportunity,
                account_id=old.account_id,
                account_name=old.account_name,
                country=old.country,
                need_id=need,
                product_category=old.product_category,
                state="qualified",
                owner=old.owner,
                created_at=q.clock[0],
            )
        )
    access = ControlledAccess(c.tenant_id, need, u.actor_id)
    unit = type(u.service)(
        u.factory,
        access,
        ControlledReader(access, u.reader.artifact_id),
        now=lambda: q.clock[0],
    )
    facts = await unit.get_facts(c.tenant_id, need, actor_id=u.actor_id)
    await unit.confirm(
        c.tenant_id,
        need,
        u.command.model_copy(
            update={
                "expected_quantity_fact_hash": quantity_fact_hash(
                    c.tenant_id, need, facts.quantity
                )
            }
        ),
        actor_id=u.actor_id,
        idempotency_key="rate-unit-" + suffix,
    )
    f = r.freeze
    evidence_values = {
        name: getattr(f.evidence, name)
        for name in cost_schemas.SupplierPriceEvidenceCreate.model_fields
    }
    evidence = await f.t2.confirm_price(
        c.tenant_id,
        cost_schemas.SupplierPriceEvidenceCreate(
            **(evidence_values | {"opportunity_id": opportunity, "need_id": need})
        ),
        actor=f.actor,
        idempotency_key="rate-price-" + suffix,
    )
    costs = CostingServiceImpl(
        f.factory,
        authorizer=Phase1CostingAuthorizer(c.tenant_id),
        now=lambda: q.clock[0],
    )
    sheet_id = await costs.create_sheet(
        c.tenant_id,
        opportunity,
        cost_schemas.CostSheetCreate(
            version_type="quoted",
            quantity=500,
            base_currency="USD",
            quote_currency="USD",
            fx_snapshot_id="fx_cost_test",
            fx_rates=[],
        ),
        actor=f.actor,
    )
    await costs.add_item(
        c.tenant_id,
        sheet_id,
        cost_schemas.CostItemCreate(
            item_type="product_purchase",
            amount=Decimal("1.25"),
            currency="USD",
            price_basis="quoted",
            is_per_unit=True,
            source_ref=evidence.evidence_id,
            note=None,
        ),
        actor=f.actor,
    )
    sheet = await costs.get_sheet(c.tenant_id, sheet_id, actor=f.actor)
    coverage = await f.t2.confirm_coverage(
        c.tenant_id,
        sheet_id,
        cost_schemas.CostCoverageCreate(
            expected_sheet_hash=sheet.content_hash,
            acquisition_mode="detail",
            decisions=tuple(
                cost_schemas.CostCoverageDecision(
                    item_type=name,
                    applicable=name == "product_purchase",
                    reason="人工核对",
                    item_bindings=(
                        cost_schemas.CostItemBinding(
                            item_sequence=1,
                            evidence_id=evidence.evidence_id,
                            source_line_ref=evidence.locator,
                            allocation_scope="order:one",
                        ),
                    )
                    if name == "product_purchase"
                    else (),
                )
                for name in cost_item_type_values()
            ),
        ),
        actor=f.actor,
        idempotency_key="rate-coverage-" + suffix,
    )
    async with f.provider.open(
        c.tenant_id, opportunity, f.actor_id, prepared_by=f.actor_id
    ) as context:
        scope_command = f.scope_command.model_copy(
            update={
                "coverage_id": coverage,
                "expected_coverage_hash": coverage,
                "expected_sheet_hash": sheet.content_hash,
                "expected_need_facts_hash": context.need_facts_hash,
                "evidence_bindings": (
                    cost_schemas.CostScopeEvidenceBinding(
                        evidence_id=evidence.evidence_id,
                        evidence_hash=evidence.evidence_hash,
                        applicability_note="人工核对完整规格与包装适用",
                    ),
                ),
            }
        )
    f = replace(
        f,
        context=replace(f.context, opportunity_id=opportunity),
        cost_sheet_id=sheet_id,
        scope_command=scope_command,
        evidence=evidence,
    )
    scope = await f.scope(key="rate-scope-" + suffix)
    intent = await f.intent(scope=scope, unit_price=r.command.unit_price)
    command = QuoteDraftCommand(
        **{name: getattr(intent, name) for name in QuoteDraftCommand.model_fields}
    )
    quote = await r.app.create(
        c.tenant_id,
        command,
        actor_id=q.actor.employee_id,
        idempotency_key="rate-create-" + suffix,
    )
    run = await a.engine.start(
        c.tenant_id,
        "quote_approval",
        quote.content.quote_id,
        {
            "quote_id": quote.content.quote_id,
            "quote_version": quote.content.version,
            "content_hash": quote.content.content_hash,
            "prepared_by": quote.content.prepared_by,
            "initiated_by": q.actor.employee_id,
        },
        "rate-approval-" + suffix,
    )
    b = SimpleNamespace(
        **{
            **vars(a),
            "quote": quote,
            "executor": QuoteWorkflowExecutor(
                workflow_type="quote_approval",
                run_id=run,
                quote_id=quote.content.quote_id,
            ),
        }
    )
    ids = await bind_real_approvals(b)
    for aid in ids:
        await a.approvals.decide(c.tenant_id, aid, approved=True, decided_by=a.decider)
    facts = await read_quote_facts(a.approvals, c.tenant_id, ids)
    async with (
        a.provider.open_for_approval(
            c.tenant_id,
            opportunity,
            a.decider,
            prepared_by=quote.content.prepared_by,
            decider_ids=(a.decider,),
        ) as context,
        q.service.open_approval(
            c.tenant_id, quote.content.quote_id, executor=b.executor
        ) as session,
    ):
        await session.apply(facts, context)
    return quote.content.quote_id


async def reservations(c):
    async with c.sessions() as db:
        return list(
            (
                await db.scalars(
                    select(ToolCallEventRow).where(
                        ToolCallEventRow.tenant_id == c.tenant_id,
                        ToolCallEventRow.stage == "rate_limit",
                        ToolCallEventRow.outcome == "reserved",
                        ToolCallEventRow.rule == "quote-file-generate-v1",
                    )
                )
            ).all()
        )


async def test_over_limit_never_enters_render(rate_case):
    c = rate_case
    quotes = [
        c.quote_id,
        await additional_approved_quote(c, "one"),
        await additional_approved_quote(c, "two"),
    ]
    results = await asyncio.gather(
        *(c.app.generate(c.tenant_id, quote, actor_id=c.sales_id) for quote in quotes),
        return_exceptions=True,
    )
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(failures) == 1 and failures[0].detail.code == "rate_limited"
    assert failures[0].detail.retry_after_seconds >= 1
    rows = await reservations(c)
    assert (
        len(rows) == c.renderer.calls == c.objects.puts == c.limits.maximum_admissions
    )
    assert len({r.tool_call_id for r in rows}) == 2
    assert all(
        r.actor_id == c.sales_id and r.cost_note == "claim_attempt:1" for r in rows
    )


async def test_successful_duplicate_does_not_reserve_again(rate_case):
    c = rate_case
    a = await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.sales_id)
    assert await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.manager_id) == a
    assert len(await reservations(c)) == c.objects.puts == c.renderer.calls == 1


async def expire_retry(c, call_id):
    """只推进可变lease/retry测试时钟，不改append-only历史事件。"""
    async with c.sessions.begin() as db:
        now = await db.scalar(text("SELECT clock_timestamp()"))
        await db.execute(
            update(ToolCallRow)
            .where(
                ToolCallRow.tenant_id == c.tenant_id,
                ToolCallRow.tool_call_id == call_id,
            )
            .values(lease_expires_at=now - timedelta(seconds=1), retry_after_at=None)
        )


@pytest.mark.parametrize("committed", [False, True])
async def test_unknown_then_rate_overwrite_still_never_regenerates(
    rate_case, monkeypatch, committed
):
    from shared.schemas.generated_documents import GeneratedDocumentError

    c = rate_case
    original_put = c.document_store.put_pdf
    put_attempts = 0

    async def unknown(*args, **kwargs):
        nonlocal put_attempts
        put_attempts += 1
        if committed:
            await original_put(*args, **kwargs)
        raise GeneratedDocumentError("commit_unknown")

    monkeypatch.setattr(c.document_store, "put_pdf", unknown)
    with pytest.raises(Exception) as first:
        await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.sales_id)
    assert first.value.detail.code == "reconciliation_required"
    canonical = first.value.detail.tool_call_id
    await expire_retry(c, canonical)
    smaller = c.limits.model_copy(update={"maximum_admissions": 1})
    c.gateway._checks["rate_limit"]._limiter = PostgresQuoteFileGenerationRateLimiter(
        c.sessions, limits=smaller, lease_owner="quote-files-test", id_generator=new_id
    )
    with pytest.raises(Exception) as limited:
        await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.manager_id)
    assert limited.value.detail.code == "rate_limited"
    async with c.sessions() as db:
        row = await db.scalar(
            select(ToolCallRow).where(
                ToolCallRow.tenant_id == c.tenant_id,
                ToolCallRow.tool_call_id == canonical,
            )
        )
        assert row.error_category == "rate_limited" and row.attempt_count == 2
    with pytest.raises(Exception) as before_retry:
        await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.manager_id)
    assert before_retry.value.detail.original_generation_call_id is None
    assert len(await reservations(c)) == 1 and put_attempts == 1
    await expire_retry(c, canonical)
    c.gateway._checks["rate_limit"]._limiter = c.rate
    if committed:
        file = await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.manager_id)
        assert file.quote_id == c.quote_id
    else:
        with pytest.raises(Exception) as unresolved:
            await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.manager_id)
        assert unresolved.value.detail.code == "reconciliation_required"
    assert put_attempts == c.renderer.calls == 1
    events = await reservations(c)
    assert len(events) == 2 and {e.tool_call_id for e in events} == {canonical}
    assert {(e.actor_id, e.cost_note) for e in events} == {
        (c.sales_id, "claim_attempt:1"),
        (c.manager_id, "claim_attempt:3"),
    }
    async with c.sessions() as db:
        received = list(
            (
                await db.scalars(
                    select(ToolCallRow).where(ToolCallRow.tenant_id == c.tenant_id)
                )
            ).all()
        )
        executed = list(
            (
                await db.scalars(
                    select(ToolCallEventRow).where(
                        ToolCallEventRow.tenant_id == c.tenant_id,
                        ToolCallEventRow.tool_call_id == canonical,
                        ToolCallEventRow.stage == "ledger",
                        ToolCallEventRow.outcome == "executing",
                    )
                )
            ).all()
        )
    assert len(received) == 4 and len(executed) == 2


@pytest.mark.parametrize("wrong_version", [False, True])
async def test_executing_is_not_reclaimed_after_lease_expiry(
    rate_case, monkeypatch, wrong_version
):
    c = rate_case
    reached, release = asyncio.Event(), asyncio.Event()
    original_record = c.files.record_file

    async def pause(*args, **kwargs):
        reached.set()
        await release.wait()
        return await original_record(*args, **kwargs)

    monkeypatch.setattr(c.files, "record_file", pause)
    old = asyncio.create_task(
        c.app.generate(c.tenant_id, c.quote_id, actor_id=c.sales_id)
    )
    try:
        await asyncio.wait_for(reached.wait(), 15)
        async with c.sessions() as db:
            row = await db.scalar(
                select(ToolCallRow).where(
                    ToolCallRow.tenant_id == c.tenant_id,
                    ToolCallRow.status == "executing",
                )
            )
            canonical = row.tool_call_id
        await expire_retry(c, canonical)
        if wrong_version:
            async with c.sessions.begin() as db:
                await db.execute(
                    update(ToolCallRow)
                    .where(
                        ToolCallRow.tenant_id == c.tenant_id,
                        ToolCallRow.tool_call_id == canonical,
                    )
                    .values(tool_version="v0")
                )
        with pytest.raises(Exception) as current:
            await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.manager_id)
        assert current.value.detail.code == (
            "idempotency_conflict" if wrong_version else "reconciliation_required"
        )
        assert current.value.detail.original_generation_call_id == (
            None if wrong_version else canonical
        )
        assert len(await reservations(c)) == c.renderer.calls == c.objects.puts == 1
    finally:
        release.set()
        await old


@pytest.mark.parametrize("failure", ["close", "close_validation", "commit", "cancel"])
async def test_reservation_unknown_or_cancel_never_enters_renderer(rate_case, failure):
    from shared.errors import ValidationError

    c = rate_case

    class Transaction:
        def __init__(self, real):
            self.real = real

        async def __aenter__(self):
            return await self.real.__aenter__()

        async def __aexit__(self, *args):
            await self.real.__aexit__(*args)
            if args[0] is None:
                if failure == "cancel":
                    raise asyncio.CancelledError()
                raise RuntimeError("controlled-commit-response")

    class Session:
        def __init__(self):
            self.real = c.sessions()

        async def __aenter__(self):
            await self.real.__aenter__()
            return self

        async def __aexit__(self, *args):
            await self.real.__aexit__(*args)
            if failure.startswith("close") and args[0] is None:
                raise (
                    ValidationError("controlled-close-response")
                    if failure == "close_validation"
                    else RuntimeError("controlled-close-response")
                )

        def begin(self):
            return (
                self.real.begin()
                if failure.startswith("close")
                else Transaction(self.real.begin())
            )

        def __getattr__(self, name):
            return getattr(self.real, name)

    c.gateway._checks["rate_limit"]._limiter = PostgresQuoteFileGenerationRateLimiter(
        Session, limits=c.limits, lease_owner="quote-files-test", id_generator=new_id
    )
    with pytest.raises(
        asyncio.CancelledError if failure == "cancel" else Exception
    ) as error:
        await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.sales_id)
    if failure != "cancel":
        assert error.value.detail.code == "dependency_unavailable"
    assert (
        len(await reservations(c)) == 1
        and c.renderer.calls == c.objects.puts == 0
        and c.slot.take() is None
    )


async def test_advisory_lock_timeout_and_wrong_claim_version_fail_closed(rate_case):
    c = rate_case
    c.gateway._checks["rate_limit"]._limiter = PostgresQuoteFileGenerationRateLimiter(
        c.sessions,
        limits=c.limits.model_copy(update={"lock_timeout_ms": 50}),
        lease_owner="quote-files-test",
        id_generator=new_id,
    )
    async with c.sessions.begin() as holding:
        await holding.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"quote-file-rate-v1:{c.tenant_id}"},
        )
        with pytest.raises(Exception) as timeout:
            await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.sales_id)
    assert timeout.value.detail.code == "dependency_unavailable"
    canonical = timeout.value.detail.tool_call_id
    await expire_retry(c, canonical)
    async with c.sessions.begin() as db:
        await db.execute(
            update(ToolCallRow)
            .where(
                ToolCallRow.tenant_id == c.tenant_id,
                ToolCallRow.tool_call_id == canonical,
            )
            .values(tool_version="v0")
        )
    with pytest.raises(Exception) as mismatch:
        await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.sales_id)
    assert mismatch.value.detail.code == "reconciliation_required"
    assert len(await reservations(c)) == c.renderer.calls == c.objects.puts == 0


async def test_each_admission_is_counted_and_history_uses_real_events(rate_case):
    from tool_gateway.file_rate_limit import QuoteFileRateError
    from tool_gateway.repository import ToolCallEventRecord

    c = rate_case
    reached, release = asyncio.Event(), asyncio.Event()
    requests = []

    class Gate:
        async def reserve(self, tenant, request):
            requests.append(request)
            reached.set()
            await release.wait()
            return await c.rate.reserve(tenant, request)

    c.gateway._checks["rate_limit"]._limiter = Gate()
    task = asyncio.create_task(
        c.app.generate(c.tenant_id, c.quote_id, actor_id=c.sales_id)
    )
    try:
        await asyncio.wait_for(reached.wait(), 15)
        request = requests[0]
        assert (await c.rate.reserve(c.tenant_id, request)).outcome == "reserved"
        assert (await c.rate.reserve(c.tenant_id, request)).outcome == "reserved"
        limited = await c.rate.reserve(c.tenant_id, request)
        assert (
            limited.outcome == "limited" and 1 <= limited.retry_after_seconds <= 86400
        )
        assert len(await reservations(c)) == 2 and c.renderer.calls == 0
        for changed in (
            request.model_copy(update={"request_fingerprint": "f" * 64}),
            request.model_copy(update={"tool_version": "v0"}),
        ):
            with pytest.raises(QuoteFileRateError) as error:
                await c.rate.reserve(c.tenant_id, changed)
            assert error.value.code == "claim_invalid"
        with pytest.raises(QuoteFileRateError) as other_tenant:
            await c.rate.reserve(new_id("tn"), request)
        assert other_tenant.value.code == "claim_invalid"
        async with c.ledger_factory(c.tenant_id) as uow:
            await uow.calls.mark_executing(c.tenant_id, request.canonical_call_id)
        with pytest.raises(QuoteFileRateError) as zero:
            await c.history.has_prior_execution(c.tenant_id, request)
        assert zero.value.code == "claim_invalid"
        for index in (1, 2):
            async with c.ledger_factory(c.tenant_id) as uow:
                await uow.calls.append_event(
                    ToolCallEventRecord(
                        tenant_id=c.tenant_id,
                        event_id=new_id("tce"),
                        tool_call_id=request.canonical_call_id,
                        stage="ledger",
                        outcome="executing",
                        rule=None,
                        category=None,
                        actor_id=c.sales_id,
                        run_id=None,
                        campaign_id=None,
                        message_attempt_id=None,
                        occurred_at=datetime.now(UTC),
                        duration_ms=0,
                        cost_note=None,
                    )
                )
            assert await c.history.has_prior_execution(c.tenant_id, request) is (
                index >= 2
            )
    finally:
        release.set()
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], Exception)
    assert c.renderer.calls == c.objects.puts == 0


async def test_future_events_and_lease_are_conservative_retry_bounds(rate_case):
    from tool_gateway.repository import ToolCallEventRecord

    c = rate_case
    reached, release = asyncio.Event(), asyncio.Event()
    requests = []

    class Gate:
        async def reserve(self, tenant, request):
            requests.append(request)
            reached.set()
            await release.wait()
            return await c.rate.reserve(tenant, request)

    c.gateway._checks["rate_limit"]._limiter = Gate()
    task = asyncio.create_task(
        c.app.generate(c.tenant_id, c.quote_id, actor_id=c.sales_id)
    )
    try:
        await asyncio.wait_for(reached.wait(), 15)
        request = requests[0]
        async with c.sessions() as db:
            now = await db.scalar(text("SELECT clock_timestamp()"))
        async with c.ledger_factory(c.tenant_id) as uow:
            for when in (now - timedelta(seconds=61), now + timedelta(seconds=10)):
                await uow.calls.append_event(
                    ToolCallEventRecord(
                        tenant_id=c.tenant_id,
                        event_id=new_id("tce"),
                        tool_call_id=request.canonical_call_id,
                        stage="rate_limit",
                        outcome="reserved",
                        rule="quote-file-generate-v1",
                        category=None,
                        actor_id=c.sales_id,
                        run_id=None,
                        campaign_id=None,
                        message_attempt_id=None,
                        occurred_at=when,
                        duration_ms=0,
                        cost_note="claim_attempt:1",
                    )
                )
        small = PostgresQuoteFileGenerationRateLimiter(
            c.sessions,
            limits=c.limits.model_copy(
                update={"maximum_admissions": 1, "window_seconds": 1}
            ),
            lease_owner="quote-files-test",
            id_generator=new_id,
        )
        limited = await small.reserve(c.tenant_id, request)
        assert limited.outcome == "limited" and 20 <= limited.retry_after_seconds <= 30
        # 增大N仍把未来事件计入，但已过窗口的事件不再占位。
        assert (await c.rate.reserve(c.tenant_id, request)).outcome == "reserved"
        assert len(await reservations(c)) == 3
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_waiting_for_admission_does_not_cache_formal_authorization(rate_case):
    c = rate_case
    reached, release = asyncio.Event(), asyncio.Event()

    class Gate:
        async def reserve(self, tenant, request):
            result = await c.rate.reserve(tenant, request)
            reached.set()
            await release.wait()
            return result

    c.gateway._checks["rate_limit"]._limiter = Gate()
    task = asyncio.create_task(
        c.app.generate(c.tenant_id, c.quote_id, actor_id=c.sales_id)
    )
    try:
        await asyncio.wait_for(reached.wait(), 15)
        c.approval.quotation.clock[0] += timedelta(days=40)
    finally:
        release.set()
    with pytest.raises(Exception) as expired:
        await task
    assert expired.value.detail.code == "quote_expired"
    assert len(await reservations(c)) == 1 and c.renderer.calls == c.objects.puts == 0


async def test_only_check_clock_changes_do_not_change_canonical(rate_case):
    c = rate_case
    ticks = 0

    def clock():
        nonlocal ticks
        ticks += 1
        return c.approval.quotation.clock[0] + timedelta(microseconds=ticks)

    c.access._now = clock
    await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.sales_id)
    assert ticks >= 5 and c.renderer.calls == c.objects.puts == 1


async def test_claimed_reclaim_requires_expired_lease_and_new_reservation(rate_case):
    c = rate_case
    reached = asyncio.Event()
    calls = []

    class Gate:
        async def reserve(self, tenant, request):
            calls.append(request)
            reached.set()
            await asyncio.Event().wait()

    c.gateway._checks["rate_limit"]._limiter = Gate()
    old = asyncio.create_task(
        c.app.generate(c.tenant_id, c.quote_id, actor_id=c.sales_id)
    )
    try:
        await asyncio.wait_for(reached.wait(), 15)
        with pytest.raises(Exception) as leased:
            await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.manager_id)
        assert (
            leased.value.detail.code == "reconciliation_required"
            and leased.value.detail.original_generation_call_id is None
        )
        assert len(calls) == 1 and len(await reservations(c)) == 0
    finally:
        old.cancel()
        with pytest.raises(asyncio.CancelledError):
            await old
    await expire_retry(c, calls[0].canonical_call_id)
    c.gateway._checks["rate_limit"]._limiter = c.rate
    await c.app.generate(c.tenant_id, c.quote_id, actor_id=c.manager_id)
    events = await reservations(c)
    assert len(events) == 1 and events[0].tool_call_id == calls[0].canonical_call_id
    assert (
        events[0].actor_id == c.manager_id and events[0].cost_note == "claim_attempt:2"
    )
    assert c.renderer.calls == c.objects.puts == 1
