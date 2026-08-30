"""需求就绪事件到 Sourcing Case V2 Run 的安全、幂等接线。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from apps.scheduler_worker.sourcing_events import SourcingTriggerHandler
from domains.sourcing.permissions import SourcingActor, SourcingScope
from domains.sourcing.schemas import NeedFact, SourcingNeedSnapshot
from shared.errors import ValidationError
from shared.events.catalog import NeedBecameSourcingReady, NeedValidated
from shared.schemas.evidence import EvidenceLevel
from shared.schemas.identifiers import (
    EmployeeId,
    RunId,
    SourcingCaseId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.provenance import ProvenanceSummary, SourceType

NOW = datetime(2026, 8, 30, 10, tzinfo=UTC)
TENANT = TenantId("tenant-trigger")
OTHER_TENANT = TenantId("tenant-other")
NEED_ID = ValidatedNeedId("need-trigger")
CASE_ID = SourcingCaseId("src-trigger")
SYSTEM = SourcingActor("system:sourcing", TENANT, SourcingScope.SYSTEM, "system")


def _provenance() -> ProvenanceSummary:
    return ProvenanceSummary(
        source_type=SourceType.CONVERSATION,
        source_id="msg-trigger",
        extracted_by="human",
        extracted_at=NOW,
        confirmed_by=EmployeeId("emp-trigger"),
        confirmed_at=NOW,
    )


def _snapshot(*, need_id: ValidatedNeedId = NEED_ID) -> SourcingNeedSnapshot:
    return SourcingNeedSnapshot(
        need_id=need_id,
        completeness=3,
        derivation_version="need-completeness-v1",
        product_category=NeedFact(value="Industrial Hinges", provenance=_provenance()),
        material=NeedFact(value="Stainless Steel", provenance=_provenance()),
        quantity=NeedFact(value=5000, provenance=_provenance()),
        snapshot_hash="a" * 64,
    )


class _Reader:
    def __init__(self, snapshot: SourcingNeedSnapshot | None = None) -> None:
        self.snapshot = snapshot or _snapshot()
        self.calls = 0

    async def read(self, tenant_id, need_id):
        self.calls += 1
        return self.snapshot


class _Sourcing:
    def __init__(self) -> None:
        self.commands: list[Any] = []
        self.created_keys: set[str] = set()

    async def open_case(self, tenant_id, command, *, actor):
        self.commands.append((tenant_id, command, actor))
        self.created_keys.add(command.trigger_key)
        return CASE_ID


class _Engine:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail_first = fail_first
        self._run = RunId("run-trigger")
        self.created_keys: set[str] = set()

    async def start(self, *args, **kwargs):
        self.calls.append((*args, kwargs))
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("postgres-dsn-secret")
        self.created_keys.add(args[4])
        return self._run


def _validated(*, completeness: int = 3, tenant_id: TenantId = TENANT) -> NeedValidated:
    return NeedValidated(
        tenant_id=tenant_id,
        occurred_at=NOW,
        need_id=NEED_ID,
        category="industrial hinges",
        evidence_level=EvidenceLevel.CUSTOMER_QUANTITY_AND_TIMING,
        completeness=completeness,
    )


def _became_ready(*, tenant_id: TenantId = TENANT) -> NeedBecameSourcingReady:
    return NeedBecameSourcingReady(
        tenant_id=tenant_id,
        occurred_at=NOW,
        need_id=NEED_ID,
        completeness=3,
    )


def _handler(reader=None, sourcing=None, engine=None):
    return SourcingTriggerHandler(
        engine=engine or _Engine(),
        sourcing=sourcing or _Sourcing(),
        need_reader=reader or _Reader(),
        tenant_id=TENANT,
        sourcing_actor=SYSTEM,
    )


@pytest.mark.asyncio
async def test_both_need_events_share_exact_case_and_run_key_with_allowlist_context() -> (
    None
):
    reader = _Reader()
    sourcing = _Sourcing()
    engine = _Engine()
    handler = _handler(reader, sourcing, engine)

    await handler.handle(_validated())
    await handler.handle(_became_ready())

    assert reader.calls == 2
    assert len(sourcing.commands) == len(engine.calls) == 2
    expected_key = f"sourcing-case:v2:{TENANT}:{NEED_ID}"
    assert [item[1].trigger_key for item in sourcing.commands] == [
        expected_key,
        expected_key,
    ]
    assert sourcing.created_keys == engine.created_keys == {expected_key}
    for call in engine.calls:
        tenant_id, workflow_type, subject_ref, context, idempotency_key, kwargs = call
        assert (tenant_id, workflow_type, subject_ref, idempotency_key, kwargs) == (
            TENANT,
            "sourcing_case",
            str(CASE_ID),
            expected_key,
            {},
        )
        assert context == {
            "case_id": str(CASE_ID),
            "need_id": str(NEED_ID),
            "need_snapshot_hash": "a" * 64,
            "product_category": "industrial hinges",
            "keywords": ["stainless steel"],
        }


@pytest.mark.asyncio
async def test_low_completeness_cross_tenant_and_unknown_event_have_zero_io() -> None:
    reader = _Reader()
    sourcing = _Sourcing()
    engine = _Engine()
    handler = _handler(reader, sourcing, engine)

    await handler.handle(_validated(completeness=2))
    await handler.handle(_validated(tenant_id=OTHER_TENANT))
    with pytest.raises(ValidationError, match="未知寻源触发事件类型"):
        await handler.handle(object())

    assert reader.calls == 0
    assert sourcing.commands == engine.calls == []


@pytest.mark.asyncio
async def test_need_reader_mismatch_stops_before_open_and_start() -> None:
    reader = _Reader(_snapshot(need_id=ValidatedNeedId("need-other")))
    sourcing = _Sourcing()
    engine = _Engine()
    handler = _handler(reader, sourcing, engine)

    with pytest.raises(ValidationError, match="可信需求快照与触发事件不匹配"):
        await handler.handle(_became_ready())

    assert reader.calls == 1
    assert sourcing.commands == engine.calls == []


@pytest.mark.asyncio
async def test_opened_case_is_reused_when_first_workflow_start_fails() -> None:
    reader = _Reader()
    sourcing = _Sourcing()
    engine = _Engine(fail_first=True)
    handler = _handler(reader, sourcing, engine)

    with pytest.raises(ValidationError, match="寻源工作流启动失败") as caught:
        await handler.handle(_validated())
    assert "postgres-dsn-secret" not in str(caught.value)

    await handler.handle(_validated())

    assert len(sourcing.commands) == len(engine.calls) == 2
    assert sourcing.created_keys == engine.created_keys
    assert engine.calls[0][4] == engine.calls[1][4]
    assert engine.calls[0][2] == engine.calls[1][2] == str(CASE_ID)
