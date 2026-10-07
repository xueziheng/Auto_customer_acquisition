"""承诺账本服务的幂等、确认、履约和逾期升级规则。"""

from __future__ import annotations

import importlib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, Self

import pytest

from domains.commitments.errors import RelativeDueTimeError
from shared.errors import InvalidStateTransition, PermissionDenied, ValidationError
from shared.events.catalog import CommitmentCreated, CommitmentOverdue
from shared.schemas.identifiers import (
    CommitmentId,
    EmployeeId,
    MessageId,
    TenantId,
    new_id,
)

NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
TENANT = TenantId("tenant-one")
_models = importlib.import_module("domains.commitments.models")
Commitment = _models.Commitment
CommitmentStatus = _models.CommitmentStatus
CommitmentType = _models.CommitmentType


class _Repository:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], Any] = {}

    async def add(self, commitment: Any) -> None:
        self.items[(str(commitment.tenant_id), str(commitment.commitment_id))] = deepcopy(
            commitment
        )

    async def add_if_absent(
        self, commitment: Any
    ) -> tuple[Any, bool]:
        duplicate = await self.find_duplicate(
            commitment.tenant_id,
            str(commitment.source_message_id),
            commitment.action,
        )
        if duplicate is not None:
            return duplicate, False
        await self.add(commitment)
        return commitment, True

    async def get(
        self, tenant_id: TenantId, commitment_id: CommitmentId
    ) -> Any | None:
        item = self.items.get((str(tenant_id), str(commitment_id)))
        return deepcopy(item) if item is not None else None

    async def get_for_update(
        self, tenant_id: TenantId, commitment_id: CommitmentId
    ) -> Any | None:
        return await self.get(tenant_id, commitment_id)

    async def update(self, commitment: Any) -> None:
        await self.add(commitment)

    async def find_duplicate(
        self, tenant_id: TenantId, source_message_id: str, action: str
    ) -> Any | None:
        for (stored_tenant, _), item in self.items.items():
            if (
                stored_tenant == str(tenant_id)
                and str(item.source_message_id) == source_message_id
                and item.action == action
            ):
                return deepcopy(item)
        return None

    async def list_due(
        self, tenant_id: TenantId, before: datetime, limit: int
    ) -> list[Any]:
        items = [
            deepcopy(item)
            for (stored_tenant, _), item in self.items.items()
            if stored_tenant == str(tenant_id) and item.is_overdue_at(before)
        ]
        return sorted(items, key=lambda item: (item.due_at, str(item.commitment_id)))[
            :limit
        ]

    async def list_for_employee(
        self,
        tenant_id: TenantId,
        employee_id: EmployeeId,
        statuses: list[Any],
    ) -> list[Any]:
        return [
            deepcopy(item)
            for (stored_tenant, _), item in self.items.items()
            if stored_tenant == str(tenant_id)
            and item.owner == employee_id
            and item.status in statuses
        ]


class _Bus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)

    async def publish_many(self, events: list[Any]) -> None:
        self.events.extend(events)

    def subscribe(self, event_type: type[Any], handler: Any) -> None:
        del event_type, handler


class _UnitOfWork:
    def __init__(self, repository: _Repository, bus: _Bus) -> None:
        self.commitments = repository
        self.bus = bus

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        del exc_type, exc, tb


class _Factory:
    def __init__(self) -> None:
        self.repository = _Repository()
        self.bus = _Bus()

    def __call__(self, tenant_id: TenantId) -> _UnitOfWork:
        del tenant_id
        return _UnitOfWork(self.repository, self.bus)


def _commitment(
    suffix: str = "one",
    *,
    commitment_type: CommitmentType = CommitmentType.EMPLOYEE,
    due_at: datetime = NOW + timedelta(days=1),
) -> Any:
    return Commitment(
        commitment_id=CommitmentId(new_id("com")),
        tenant_id=TENANT,
        commitment_type=commitment_type,
        owner=EmployeeId("employee-one"),
        action=f"Complete agreed action {suffix}",
        due_at=due_at,
        source_message_id=MessageId(f"message-{suffix}"),
        verbatim=f"We will complete agreed action {suffix} tomorrow.",
        created_at=NOW,
        status=(
            CommitmentStatus.WAITING_CUSTOMER
            if commitment_type is CommitmentType.CUSTOMER
            else CommitmentStatus.PENDING
        ),
        extracted_by="team-operations-v1",
    )


def _service(factory: _Factory) -> Any:
    module = __import__(
        "domains.commitments.service_impl",
        fromlist=["CommitmentServiceImpl"],
    )
    return module.CommitmentServiceImpl(factory, now=lambda: NOW)


@pytest.mark.asyncio
async def test_record_extracted_is_atomic_and_idempotent() -> None:
    factory = _Factory()
    service = _service(factory)
    item = _commitment()

    first = await service.record_extracted(TENANT, item)
    duplicate = await service.record_extracted(TENANT, deepcopy(item))

    assert duplicate == first
    assert len(factory.repository.items) == 1
    assert len(factory.bus.events) == 1
    event = factory.bus.events[0]
    assert isinstance(event, CommitmentCreated)
    assert event.commitment_id == str(first)


@pytest.mark.asyncio
async def test_confirm_can_replace_uncertain_due_time_then_fulfill() -> None:
    factory = _Factory()
    service = _service(factory)
    item = _commitment()
    item.due_at_uncertain = True
    await service.record_extracted(TENANT, item)

    await service.confirm(
        TENANT,
        item.commitment_id,
        EmployeeId("employee-one"),
        "2026-08-25T09:30:00+08:00",
    )
    confirmed = await factory.repository.get(TENANT, item.commitment_id)
    assert confirmed is not None
    assert confirmed.confirmed_by == "employee-one"
    assert confirmed.confirmed_at == NOW
    assert confirmed.due_at.isoformat() == "2026-08-25T09:30:00+08:00"
    assert confirmed.due_at_uncertain is False

    await service.fulfill(TENANT, item.commitment_id, EmployeeId("employee-one"))
    fulfilled = await factory.repository.get(TENANT, item.commitment_id)
    assert fulfilled is not None
    assert fulfilled.status is CommitmentStatus.FULFILLED
    assert fulfilled.fulfilled_at == NOW


@pytest.mark.asyncio
async def test_confirm_rejects_relative_due_time_and_terminal_transition() -> None:
    factory = _Factory()
    service = _service(factory)
    item = _commitment()
    await service.record_extracted(TENANT, item)

    with pytest.raises(RelativeDueTimeError):
        await service.confirm(
            TENANT,
            item.commitment_id,
            EmployeeId("employee-reviewer"),
            "tomorrow",
        )
    await service.confirm(
        TENANT, item.commitment_id, EmployeeId("employee-one")
    )
    await service.fulfill(TENANT, item.commitment_id, EmployeeId("employee-one"))
    with pytest.raises(InvalidStateTransition):
        await service.confirm(
            TENANT, item.commitment_id, EmployeeId("employee-one")
        )


@pytest.mark.asyncio
async def test_scan_overdue_notifies_once_then_escalates_employee_once() -> None:
    factory = _Factory()
    service = _service(factory)
    item = _commitment(due_at=NOW - timedelta(hours=2))
    item.confirmed_by = EmployeeId("employee-reviewer")
    item.confirmed_at = NOW - timedelta(days=1)
    await factory.repository.add(item)

    assert await service.scan_overdue(TENANT) == 1
    first = await factory.repository.get(TENANT, item.commitment_id)
    assert first is not None and first.status is CommitmentStatus.OVERDUE
    assert first.escalated_at is None
    assert await service.scan_overdue(TENANT) == 0
    second = await factory.repository.get(TENANT, item.commitment_id)
    assert second is not None and second.escalated_at == NOW
    assert await service.scan_overdue(TENANT) == 0
    assert len(factory.bus.events) == 2
    assert all(isinstance(event, CommitmentOverdue) for event in factory.bus.events)
    assert factory.bus.events[0].overdue_seconds == 7200


@pytest.mark.asyncio
async def test_customer_commitment_does_not_repeat_manager_escalation() -> None:
    factory = _Factory()
    service = _service(factory)
    item = _commitment(
        commitment_type=CommitmentType.CUSTOMER,
        due_at=NOW - timedelta(hours=1),
    )
    item.confirmed_by = EmployeeId("employee-reviewer")
    item.confirmed_at = NOW - timedelta(days=1)
    await factory.repository.add(item)

    assert await service.scan_overdue(TENANT) == 1
    assert await service.scan_overdue(TENANT) == 0
    assert len(factory.bus.events) == 1


@pytest.mark.asyncio
async def test_service_rejects_cross_tenant_and_unconfirmed_fulfillment() -> None:
    factory = _Factory()
    service = _service(factory)
    item = _commitment()
    with pytest.raises(ValidationError, match="租户"):
        await service.record_extracted(TenantId("tenant-other"), item)
    await service.record_extracted(TENANT, item)
    with pytest.raises(InvalidStateTransition, match="未确认"):
        await service.fulfill(
            TENANT, item.commitment_id, EmployeeId("employee-one")
        )


@pytest.mark.asyncio
async def test_only_owner_can_confirm_or_fulfill_commitment() -> None:
    factory = _Factory()
    service = _service(factory)
    item = _commitment()
    await service.record_extracted(TENANT, item)

    with pytest.raises(PermissionDenied):
        await service.confirm(
            TENANT, item.commitment_id, EmployeeId("employee-other")
        )
    await service.confirm(TENANT, item.commitment_id, EmployeeId("employee-one"))
    with pytest.raises(PermissionDenied):
        await service.fulfill(
            TENANT, item.commitment_id, EmployeeId("employee-other")
        )


@pytest.mark.asyncio
async def test_record_rejects_id_that_notification_pipeline_cannot_route() -> None:
    factory = _Factory()
    service = _service(factory)
    item = _commitment()
    item.commitment_id = CommitmentId("commitment-free-text")

    with pytest.raises(ValidationError, match="承诺标识"):
        await service.record_extracted(TENANT, item)
