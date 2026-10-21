"""历史人工等待自动恢复；分页公平、事件分代与租户查询均须保持。"""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from importlib import import_module
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from domains.prospecting.service import ServicePlanState, tier_policy
from shared.errors import TransientError, ValidationError

NOW = datetime(2026, 10, 10, tzinfo=UTC)


def module():
    try:
        return import_module("workflows.account_discovery.screening_wake")
    except ModuleNotFoundError:
        pytest.fail("联系人筛选唤醒驱动尚未实现")


def adapter():
    try:
        return import_module("infra.db.contact_screening_waits")
    except ModuleNotFoundError:
        pytest.fail("联系人等待扫描适配器尚未实现")


def plan(**patch):
    return ServicePlanState(**{
        "tenant_id": "tenant-a", "tier": "basic", "version": 1,
        "period_started_at": NOW - timedelta(days=1),
        "period_ends_at": NOW + timedelta(days=29),
        "candidates_reserved": 0, "searches_reserved": 0,
        "contacts_reserved": 0, "schedule_enabled": False, **patch,
    })


class Store:
    def __init__(self):
        self.state = plan()
        self.reads = []
        self.plan_reads = 0
        self.error = None

    async def get(self, tenant_id):
        assert tenant_id == "tenant-a"
        self.plan_reads += 1
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        return self.state

    async def get_evidence(self, tenant_id, account_id):
        assert tenant_id == "tenant-a"
        self.reads.append(account_id)
        pytest.fail("历史补证等待恢复不得读取或要求人工证据")

    async def reserve(self, *args, **kwargs):
        pytest.fail("扫描不得预留联系人或调用付费资源")


class Engine:
    def __init__(self):
        self.events = []
        self.accepted = True

    async def deliver_event(self, tenant_id, run_id, event_type, payload):
        self.events.append((tenant_id, run_id, event_type, payload))
        return self.accepted


class Waits:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.requests = []

    async def list_waiting(self, tenant_id, *, after_run_id, limit):
        self.requests.append((tenant_id, after_run_id, limit))
        return next(self.pages)


def waiting(**patch):
    return module().WaitingContactScreening(**{
        "run_id": "run_one", "account_id": "acc_one",
        "step_name": "find_contacts", "wait_version": NOW.isoformat(), **patch,
    })


def page(*items, cursor=None):
    return module().ContactScreeningWaitPage(tuple(items), cursor)


def driver(pages, *, store=None, engine=None, batch_limit=2):
    waits = Waits(pages)
    store = store or Store()
    engine = engine or Engine()
    return module().ContactScreeningWakeDriver(
        tenant_id="tenant-a", waits=waits, service_plans=store,
        engine=engine, batch_limit=batch_limit, clock=lambda: NOW,
    ), waits, store, engine


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["basic", "standard", "advanced"])
async def test_legacy_wait_wakes_without_evidence_or_reservation(tier):
    worker, waits, store, engine = driver([page(waiting())])
    store.state = plan(tier=tier)
    assert await worker.scan_once() == 1
    assert waits.requests == [("tenant-a", None, 2)]
    assert store.reads == []
    tenant, run, event, payload = engine.events[0]
    assert (tenant, run, event) == ("tenant-a", "run_one", "ProspectScreeningReady")
    assert payload["step_name"] == "find_contacts"
    assert payload["wait_version"] == NOW.isoformat()
    assert payload["tier"] == tier
    assert payload["policy_version"] == tier_policy(tier).policy_version
    assert "evidence_digest" not in payload


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [None,
    plan(period_started_at=NOW - timedelta(days=30), period_ends_at=NOW),
    plan(period_started_at=NOW + timedelta(days=1), period_ends_at=NOW + timedelta(days=31)),
    plan(tenant_id="tenant-b")])
async def test_absent_expired_or_foreign_plan_cannot_wake(state):
    worker, _, store, engine = driver([page(waiting())])
    store.state = state
    assert await worker.scan_once() == 0
    assert engine.events == []


@pytest.mark.asyncio
async def test_rotation_passes_inactive_first_page_and_empty_filtered_page():
    pages = [page(waiting(run_id="run_1"), cursor="run_1"),
             page(cursor="run_2"), page(waiting(run_id="run_3")), page()]
    worker, waits, store, engine = driver(pages, batch_limit=1)
    store.state = None
    assert await worker.scan_once() == 0
    assert await worker.scan_once() == 0
    store.state = plan()
    assert await worker.scan_once() == 1
    assert await worker.scan_once() == 0
    assert waits.requests == [("tenant-a", None, 1), ("tenant-a", "run_1", 1),
                              ("tenant-a", "run_2", 1), ("tenant-a", None, 1)]
    assert engine.events[0][1] == "run_3"


@pytest.mark.asyncio
async def test_steps_and_wait_generations_have_distinct_event_payloads():
    first = waiting()
    second = replace(first, step_name="verify_contacts")
    third = replace(second, wait_version=(NOW + timedelta(seconds=1)).isoformat())
    worker, _, _, engine = driver([page(first), page(second), page(third)])
    for _ in range(3):
        assert await worker.scan_once() == 1
    payloads = [json.dumps(event[3], sort_keys=True) for event in engine.events]
    assert len(set(payloads)) == 3


@pytest.mark.asyncio
async def test_repeated_wait_has_stable_identity_but_current_tier_changes_it():
    worker, _, store, engine = driver([page(waiting()) for _ in range(3)])
    assert await worker.scan_once() == 1
    assert await worker.scan_once() == 1
    assert engine.events[0][3] == engine.events[1][3]
    store.state = plan(tier="standard")
    assert await worker.scan_once() == 1
    assert engine.events[2][3]["tier"] == "standard"
    assert engine.events[2][3] != engine.events[0][3]
    assert store.reads == []


@pytest.mark.asyncio
async def test_one_bad_run_is_isolated_and_exception_text_is_not_logged(caplog):
    worker, _, store, engine = driver([page(waiting(run_id="r1"), waiting(run_id="r2"))])
    store.error = RuntimeError("private://credential-and-customer-source")
    assert await worker.scan_once() == 1
    assert engine.events[0][1] == "r2"
    assert "credential" not in caplog.text
    assert "暂未唤醒" in caplog.text


@pytest.mark.asyncio
async def test_cancellation_stops_remaining_runs():
    worker, _, store, engine = driver([page(waiting(run_id="r1"), waiting(run_id="r2"))])
    store.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await worker.scan_once()
    assert store.plan_reads == 1
    assert store.reads == []
    assert engine.events == []


@pytest.mark.asyncio
async def test_delivery_noop_is_not_counted():
    engine = Engine()
    engine.accepted = False
    worker, _, _, _ = driver([page(waiting())], engine=engine)
    assert await worker.scan_once() == 0


@pytest.mark.parametrize("limit", [0, -1, True, 1001])
def test_unbounded_or_invalid_batch_is_rejected(limit):
    with pytest.raises(ValidationError):
        driver([], batch_limit=limit)


class Session:
    def __init__(self, rows=(), error=None):
        self.rows, self.error, self.statement = rows, error, None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def execute(self, statement):
        self.statement = statement
        if self.error:
            raise self.error
        return SimpleNamespace(all=lambda: self.rows)


def row(run_id="run_1", context=None, **patch):
    return SimpleNamespace(run_id=run_id, context=context or {"account_id": "acc_one"},
        step_name="find_contacts", updated_at=NOW, **patch)


@pytest.mark.asyncio
async def test_adapter_query_scopes_both_tables_and_only_current_waiting_contacts():
    session = Session([row()])
    reader = adapter().PostgresContactScreeningWaitReader(lambda: session)
    result = await reader.list_waiting("tenant-a", after_run_id="run_0", limit=2)
    assert result.waits == (waiting(run_id="run_1", wait_version="0:" + NOW.isoformat()),)
    assert result.next_after_run_id is None
    sql = str(session.statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    for clause in ("workflow_runs.tenant_id = 'tenant-a'", "workflow_steps.tenant_id = 'tenant-a'",
        "workflow_runs.run_id = workflow_steps.run_id", "workflow_runs.current_step = workflow_steps.step_name",
        "workflow_runs.workflow_type = 'account_discovery'", "workflow_runs.status = 'running'",
        "workflow_steps.status = 'waiting_event'", "workflow_runs.run_id > 'run_0'",
        "'find_contacts'", "'verify_contacts'", "ORDER BY workflow_runs.run_id", "LIMIT 2"):
        assert clause in sql


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [[], 1, {"account_id": None}, {"account_id": " "},
    {"account_id": "acc\nother"}, {"account_id": "a" * 41}, {"account_id": ["acc"]}])
async def test_invalid_context_is_skipped_but_raw_page_cursor_advances(bad):
    session = Session([SimpleNamespace(run_id="run_1", context=bad,
        step_name="find_contacts", updated_at=NOW)])
    reader = adapter().PostgresContactScreeningWaitReader(lambda: session)
    result = await reader.list_waiting("tenant-a", after_run_id=None, limit=1)
    assert result.waits == ()
    assert result.next_after_run_id == "run_1"


@pytest.mark.asyncio
async def test_adapter_database_errors_are_sanitized():
    reader = adapter().PostgresContactScreeningWaitReader(
        lambda: Session(error=SQLAlchemyError("private://credential")))
    with pytest.raises(TransientError) as caught:
        await reader.list_waiting("tenant-a", after_run_id=None, limit=1)
    assert "credential" not in str(caught.value)


@pytest.mark.asyncio
async def test_wait_generation_changes_version_even_when_clock_is_static():
    session = Session([row(context={"account_id": "acc_one", "contact_screening_wait_generation": 7})])
    reader = adapter().PostgresContactScreeningWaitReader(lambda: session)
    first = await reader.list_waiting("tenant-a", after_run_id=None, limit=2)
    session.rows = [row(context={"account_id": "acc_one", "contact_screening_wait_generation": 8})]
    second = await reader.list_waiting("tenant-a", after_run_id=None, limit=2)
    assert first.waits[0].wait_version == "7:" + NOW.isoformat()
    assert second.waits[0].wait_version == "8:" + NOW.isoformat()
