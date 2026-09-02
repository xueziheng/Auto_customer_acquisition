"""需求就绪与需求簇成员事实到 durable Sourcing Admission 的接线。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from apps.scheduler_worker import sourcing_events as _sourcing_events
from domains.demand.schemas import NeedClusterPriorityFacts
from domains.sourcing.permissions import SourcingActor, SourcingScope
from domains.sourcing.schemas import NeedFact, SourcingNeedSnapshot
from shared.errors import TransientError, ValidationError
from shared.events.catalog import (
    NeedBecameSourcingReady,
    NeedClusterMembershipChanged,
    NeedValidated,
)
from shared.schemas.evidence import EvidenceLevel
from shared.schemas.identifiers import (
    EmployeeId,
    NeedClusterId,
    SourcingAdmissionId,
    SourcingCaseId,
    SourcingPrioritySnapshotId,
    TenantId,
    ValidatedNeedId,
)
from shared.schemas.provenance import ProvenanceSummary, SourceType

NOW = datetime(2026, 9, 2, 10, tzinfo=UTC)
TENANT = TenantId("tenant-trigger")
OTHER_TENANT = TenantId("tenant-other")
NEED_ID = ValidatedNeedId("need-trigger")
CLUSTER_ID = NeedClusterId("cluster-trigger")
CASE_ID = SourcingCaseId("src-trigger")
ADMISSION_ID = SourcingAdmissionId("sad-trigger")
SYSTEM = SourcingActor("system:sourcing", TENANT, SourcingScope.SYSTEM, "system")
SourcingTriggerHandler = _sourcing_events.SourcingTriggerHandler
_safe_context = _sourcing_events._safe_context


def _membership_handler_type() -> type[Any]:
    handler_type = getattr(_sourcing_events, "SourcingClusterMembershipHandler", None)
    if handler_type is None:
        pytest.fail("RED：SourcingClusterMembershipHandler 尚未实现")
    return handler_type


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


def _facts(
    *,
    need_id: str = NEED_ID,
    cluster_id: str | None = CLUSTER_ID,
    count: object = 8,
    observed_at: object = NOW,
) -> NeedClusterPriorityFacts:
    return NeedClusterPriorityFacts(
        need_id=need_id,
        cluster_id=cluster_id,
        cluster_member_count=count,  # type: ignore[arg-type]
        facts_observed_at=observed_at,  # type: ignore[arg-type]
    )


class _Reader:
    def __init__(self, snapshot: SourcingNeedSnapshot | None = None) -> None:
        self.snapshot = snapshot or _snapshot()
        self.calls: list[tuple[TenantId, ValidatedNeedId]] = []

    async def read(self, tenant_id: TenantId, need_id: ValidatedNeedId):
        self.calls.append((tenant_id, need_id))
        return self.snapshot


class _Demand:
    def __init__(
        self,
        facts: NeedClusterPriorityFacts | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.facts = facts or _facts()
        self.error = error
        self.calls: list[tuple[TenantId, ValidatedNeedId]] = []

    async def get_cluster_priority_facts(
        self, tenant_id: TenantId, need_id: ValidatedNeedId
    ) -> NeedClusterPriorityFacts:
        self.calls.append((tenant_id, need_id))
        if self.error is not None:
            raise self.error
        return self.facts


class _Sourcing:
    def __init__(
        self,
        *,
        enqueue_error: Exception | None = None,
        refresh_result: tuple[SourcingPrioritySnapshotId, ...] | None = None,
    ) -> None:
        self.enqueue_error = enqueue_error
        self.refresh_result = (
            (SourcingPrioritySnapshotId("sps-refreshed"),)
            if refresh_result is None
            else refresh_result
        )
        self.open_calls: list[tuple[Any, ...]] = []
        self.enqueue_calls: list[tuple[Any, ...]] = []
        self.refresh_calls: list[tuple[Any, ...]] = []
        self.block_cluster_calls: list[tuple[Any, ...]] = []

    async def open_case(self, tenant_id, command, *, actor):
        self.open_calls.append((tenant_id, command, actor))
        return CASE_ID

    async def enqueue_admission(
        self, tenant_id, case_id, need_id, *, ready_at, command, actor
    ):
        self.enqueue_calls.append(
            (tenant_id, case_id, need_id, ready_at, command, actor)
        )
        if self.enqueue_error is not None:
            raise self.enqueue_error
        return ADMISSION_ID

    async def refresh_cluster_admissions(
        self, tenant_id, changed_need_id, *, facts, refreshed_at, actor
    ):
        self.refresh_calls.append(
            (tenant_id, changed_need_id, facts, refreshed_at, actor)
        )
        return self.refresh_result

    async def block_cluster_admissions(
        self, tenant_id, cluster_id, changed_need_id, *, blocked_at, actor
    ):
        self.block_cluster_calls.append(
            (tenant_id, cluster_id, changed_need_id, blocked_at, actor)
        )
        return ()


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


def _membership(
    *,
    tenant_id: object = TENANT,
    cluster_id: object = CLUSTER_ID,
    changed_need_id: object = NEED_ID,
    member_count: object = 2,
    occurred_at: object = NOW + timedelta(minutes=1),
) -> NeedClusterMembershipChanged:
    return NeedClusterMembershipChanged(
        tenant_id=cast(TenantId, tenant_id),
        occurred_at=cast(datetime, occurred_at),
        cluster_id=cast(NeedClusterId, cluster_id),
        changed_need_id=cast(ValidatedNeedId, changed_need_id),
        member_count=member_count,  # type: ignore[arg-type]
    )


def _trigger(reader=None, sourcing=None, demand=None) -> SourcingTriggerHandler:
    return SourcingTriggerHandler(
        sourcing=cast(Any, sourcing or _Sourcing()),
        demand=cast(Any, demand or _Demand()),
        need_reader=reader or _Reader(),
        tenant_id=TENANT,
        sourcing_actor=SYSTEM,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("event", [_validated(), _became_ready()])
async def test_each_readiness_event_reads_once_and_only_enqueues(event: object) -> None:
    """回归变异：重新注入 engine.start、重复读事实或漏掉 durable enqueue 均应失败。"""

    reader, demand, sourcing = _Reader(), _Demand(), _Sourcing()
    handler = _trigger(reader, sourcing, demand)

    await handler.handle(event)

    assert reader.calls == [(TENANT, NEED_ID)]
    assert demand.calls == [(TENANT, NEED_ID)]
    assert len(sourcing.open_calls) == len(sourcing.enqueue_calls) == 1
    open_command = sourcing.open_calls[0][1]
    assert open_command.trigger_key == f"sourcing-case:v2:{TENANT}:{NEED_ID}"
    tenant, case_id, need_id, ready_at, command, actor = sourcing.enqueue_calls[0]
    assert (tenant, case_id, need_id, ready_at, actor) == (
        TENANT,
        CASE_ID,
        NEED_ID,
        NOW,
        SYSTEM,
    )
    assert command.blocked_reason is None
    assert command.facts is not None
    assert command.facts.model_dump() == {
        "need_id": NEED_ID,
        "cluster_id": CLUSTER_ID,
        "cluster_member_count": 8,
        "facts_observed_at": NOW,
    }
    assert not hasattr(handler, "_engine")


@pytest.mark.asyncio
async def test_low_completeness_cross_tenant_and_unknown_event_have_zero_io() -> None:
    reader, demand, sourcing = _Reader(), _Demand(), _Sourcing()
    handler = _trigger(reader, sourcing, demand)

    await handler.handle(_validated(completeness=2))
    await handler.handle(_validated(tenant_id=OTHER_TENANT))
    with pytest.raises(ValidationError, match="未知寻源触发事件类型"):
        await handler.handle(object())

    assert reader.calls == demand.calls == []
    assert sourcing.open_calls == sourcing.enqueue_calls == []


@pytest.mark.asyncio
async def test_permanently_invalid_priority_shape_creates_only_fixed_blocked_admission() -> (
    None
):
    """永久结构损坏必须可消费且固定阻断，不能伪造排序事实或泄漏异常。"""

    demand = _Demand(_facts(count=False))
    sourcing = _Sourcing()

    await _trigger(sourcing=sourcing, demand=demand).handle(_validated())

    assert len(sourcing.enqueue_calls) == 1
    command = sourcing.enqueue_calls[0][4]
    assert command.facts is None
    assert command.blocked_reason == "priority_facts_invalid"


@pytest.mark.asyncio
async def test_transient_priority_read_stays_retryable_detached_and_does_not_enqueue() -> (
    None
):
    demand = _Demand(
        error=TransientError(
            "postgres://user:secret@demand/private",
            context={"credential": "raw-token"},
        )
    )
    sourcing = _Sourcing()

    with pytest.raises(TransientError, match="^需求簇优先级事实暂不可用$") as caught:
        await _trigger(sourcing=sourcing, demand=demand).handle(_validated())

    assert caught.value.context == {}
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert "raw-token" not in str(caught.value)
    assert len(sourcing.open_calls) == 1
    assert sourcing.enqueue_calls == []


@pytest.mark.asyncio
async def test_existing_non_opened_case_replay_is_acknowledged_without_bypass() -> None:
    """既有 starting/terminal V1/V2 由 service 拒绝时保持原样，不毒化重复事件。"""

    sourcing = _Sourcing(enqueue_error=ValidationError("untrusted-case-state"))

    await _trigger(sourcing=sourcing).handle(_validated())

    assert len(sourcing.open_calls) == len(sourcing.enqueue_calls) == 1


@pytest.mark.asyncio
async def test_membership_refresh_uses_current_demand_facts_not_event_count() -> None:
    demand = _Demand(_facts(count=9, observed_at=NOW + timedelta(minutes=2)))
    sourcing = _Sourcing()
    handler = _membership_handler_type()(
        demand=demand,
        sourcing=sourcing,
        tenant_id=TENANT,
        sourcing_actor=SYSTEM,
    )

    await handler.handle(_membership(member_count=2))

    assert demand.calls == [(TENANT, NEED_ID)]
    assert len(sourcing.refresh_calls) == 1
    tenant, changed_need, facts, refreshed_at, actor = sourcing.refresh_calls[0]
    assert (tenant, changed_need, actor) == (TENANT, NEED_ID, SYSTEM)
    assert facts.cluster_member_count == 9
    assert facts.cluster_id == CLUSTER_ID
    assert refreshed_at == NOW + timedelta(minutes=2)


@pytest.mark.asyncio
async def test_membership_before_admission_and_duplicate_are_safe_service_calls() -> (
    None
):
    """无目标时 service 返回空；重复事件仍以同一 current facts 交给 hash 幂等层。"""

    demand = _Demand()
    sourcing = _Sourcing(refresh_result=())
    handler = _membership_handler_type()(
        demand=demand,
        sourcing=sourcing,
        tenant_id=TENANT,
        sourcing_actor=SYSTEM,
    )

    event = _membership()
    await handler.handle(event)
    await handler.handle(event)

    assert demand.calls == [(TENANT, NEED_ID), (TENANT, NEED_ID)]
    assert len(sourcing.refresh_calls) == 2


@pytest.mark.asyncio
async def test_out_of_order_membership_for_old_cluster_is_no_op() -> None:
    demand = _Demand(_facts(cluster_id="cluster-current", count=10))
    sourcing = _Sourcing()
    handler = _membership_handler_type()(
        demand=demand,
        sourcing=sourcing,
        tenant_id=TENANT,
        sourcing_actor=SYSTEM,
    )

    await handler.handle(_membership(cluster_id=NeedClusterId("cluster-old")))

    assert demand.calls == [(TENANT, NEED_ID)]
    assert sourcing.refresh_calls == []
    assert sourcing.block_cluster_calls == []


@pytest.mark.asyncio
async def test_permanently_invalid_membership_facts_block_exact_cluster_targets() -> None:
    """把永久非法事实静默 no-op 会让旧 waiting 排序永久继续生效。"""

    demand = _Demand(_facts(count=False))
    sourcing = _Sourcing()
    handler = _membership_handler_type()(
        demand=demand,
        sourcing=sourcing,
        tenant_id=TENANT,
        sourcing_actor=SYSTEM,
    )

    await handler.handle(_membership(member_count=999))

    assert sourcing.refresh_calls == []
    assert sourcing.block_cluster_calls == [
        (
            TENANT,
            CLUSTER_ID,
            NEED_ID,
            NOW + timedelta(minutes=1),
            SYSTEM,
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("member_count", [0, -1, False, True, 1.0])
async def test_membership_rejects_non_positive_exact_integer_before_io(
    member_count: object,
) -> None:
    demand, sourcing = _Demand(), _Sourcing()
    handler = _membership_handler_type()(
        demand=demand,
        sourcing=sourcing,
        tenant_id=TENANT,
        sourcing_actor=SYSTEM,
    )

    with pytest.raises(ValidationError, match="需求簇成员变更事件载荷无效"):
        await handler.handle(_membership(member_count=member_count))

    assert demand.calls == sourcing.refresh_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event",
    [
        _membership(tenant_id=" "),
        _membership(cluster_id=None),
        _membership(cluster_id="cluster\x00unsafe"),
        _membership(changed_need_id=""),
        _membership(occurred_at=NOW.replace(tzinfo=None)),
    ],
)
async def test_membership_rejects_invalid_tenant_and_ids_before_io(
    event: NeedClusterMembershipChanged,
) -> None:
    demand, sourcing = _Demand(), _Sourcing()
    handler = _membership_handler_type()(
        demand=demand,
        sourcing=sourcing,
        tenant_id=TENANT,
        sourcing_actor=SYSTEM,
    )

    with pytest.raises(ValidationError, match="需求簇成员变更事件载荷无效"):
        await handler.handle(event)

    assert demand.calls == sourcing.refresh_calls == []


@pytest.mark.asyncio
async def test_membership_transient_facts_failure_retries_without_refresh() -> None:
    demand = _Demand(error=RuntimeError("postgres://user:secret@demand/private"))
    sourcing = _Sourcing()
    handler = _membership_handler_type()(
        demand=demand,
        sourcing=sourcing,
        tenant_id=TENANT,
        sourcing_actor=SYSTEM,
    )

    with pytest.raises(TransientError, match="^需求簇优先级事实暂不可用$") as caught:
        await handler.handle(_membership())

    assert caught.value.context == {}
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert sourcing.refresh_calls == []
    assert sourcing.block_cluster_calls == []


def test_safe_context_remains_available_for_atomic_task8_migration() -> None:
    assert _safe_context(_snapshot(), str(CASE_ID)) == {
        "case_id": str(CASE_ID),
        "need_id": str(NEED_ID),
        "need_snapshot_hash": "a" * 64,
        "product_category": "industrial hinges",
        "keywords": ["stainless steel"],
    }
