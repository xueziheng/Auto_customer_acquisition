"""Company Playbook 不可变持久化与 tenant-bound 仓储验收。"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from domains.organization.schemas import PlaybookProposalCreate
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import (
    ApprovalId,
    EmployeeId,
    IdempotencyKey,
    PlaybookActivationId,
    PlaybookVersionId,
    TenantId,
)

NOW = datetime(2026, 8, 24, 10, tzinfo=UTC)


def _load(module: str, symbol: str):
    try:
        return getattr(importlib.import_module(module), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{symbol} 尚未创建（{exc}）")


def _command(**overrides: object) -> PlaybookProposalCreate:
    values: dict[str, object] = {
        "company_type": "trading_company",
        "minimum_deal_amount": Decimal("10000.00"),
        "minimum_deal_currency": "USD",
        "excluded_categories": ["adult"],
        "sourcing_regions": ["guangdong"],
        "excluded_countries": ["north korea"],
        "monthly_budget_credits": 500,
        "approval_requirements": ["catalog_reference_price"],
        "supply_capabilities_note": "Hardware sourcing.",
    }
    values.update(overrides)
    return PlaybookProposalCreate.model_validate(values)


def _version(
    tenant: str,
    suffix: str,
    number: int,
    *,
    base: object | None = None,
):
    model = _load("domains.organization.models", "CompanyPlaybookVersion")
    base_version_id = getattr(base, "playbook_version_id", None)
    base_content_hash = getattr(base, "content_hash", None)
    return model.from_command(
        tenant_id=TenantId(tenant),
        version_id=PlaybookVersionId(f"pbv_{suffix}"),
        version_number=number,
        command=_command(),
        base_version_id=base_version_id,
        base_content_hash=base_content_hash,
        proposed_by=EmployeeId(f"emp_{suffix}"),
        proposed_at=NOW,
        idempotency_key=IdempotencyKey(f"settings-{suffix}"),
    )


def _activation(
    version: object,
    suffix: str,
    *,
    approval_id: ApprovalId | None = None,
):
    model = _load("domains.organization.models", "PlaybookActivation")
    return model(
        tenant_id=version.tenant_id,
        activation_id=PlaybookActivationId(f"pba_{suffix}"),
        playbook_version_id=version.playbook_version_id,
        content_hash=version.content_hash,
        approval_id=approval_id or ApprovalId(f"apr_{suffix}"),
        change_set_ref=version.change_set_ref,
        approved_by=EmployeeId(f"emp_approver_{suffix}"),
        approved_at=NOW + timedelta(minutes=1),
        activated_by="system:playbook-workflow",
        activated_at=NOW + timedelta(minutes=2),
    )


@pytest_asyncio.fixture
async def organization_uow_factory(
    db_url: str,
) -> AsyncIterator[tuple[Callable[[TenantId], object], AsyncEngine]]:
    from infra.db.session import create_engine_from

    implementation = _load(
        "infra.db.organization_uow", "SqlAlchemyOrganizationUnitOfWork"
    )
    engine = create_engine_from(db_url)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield lambda tenant_id: implementation(session_factory, tenant_id), engine
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_playbook_version_and_activation_roundtrip_preserves_decimal_and_facts(
    organization_uow_factory,
) -> None:
    factory, _ = organization_uow_factory
    tenant = TenantId("tenant-roundtrip")
    version = _version(str(tenant), "roundtrip", 1)
    activation = _activation(version, "roundtrip")

    async with factory(tenant) as uow:
        await uow.versions.add(version)
        await uow.activations.add(activation)

    async with factory(tenant) as uow:
        found = await uow.versions.get(tenant, version.playbook_version_id)
        current = await uow.activations.get_current(tenant)

    assert found == version
    assert found.minimum_deal_value.amount == Decimal("10000.00")
    assert current == activation


@pytest.mark.asyncio
async def test_playbook_repositories_raise_on_cross_tenant_access(
    organization_uow_factory,
) -> None:
    factory, _ = organization_uow_factory
    tenant = TenantId("tenant-isolation-a")
    other = TenantId("tenant-isolation-b")
    version = _version(str(tenant), "isolation", 1)
    async with factory(tenant) as uow:
        await uow.versions.add(version)

    async with factory(tenant) as uow:
        with pytest.raises(TenantIsolationViolation):
            await uow.versions.get(other, version.playbook_version_id)
        with pytest.raises(TenantIsolationViolation):
            await uow.activations.get_current(other)


@pytest.mark.asyncio
async def test_playbook_lookup_by_idempotency_and_version_list_are_tenant_scoped(
    organization_uow_factory,
) -> None:
    factory, _ = organization_uow_factory
    tenant = TenantId("tenant-list")
    first = _version(str(tenant), "list-one", 1)
    second = _version(str(tenant), "list-two", 2, base=first)
    async with factory(tenant) as uow:
        await uow.versions.add(first)
        await uow.versions.add(second)

    async with factory(tenant) as uow:
        found = await uow.versions.find_by_idempotency_key(
            tenant, second.idempotency_key
        )
        versions = await uow.versions.list(tenant, 10)

    assert found == second
    assert [item.version_number for item in versions] == [2, 1]


async def _allocate_version(factory, tenant: TenantId, index: int) -> int:
    async with factory(tenant) as uow:
        number = await uow.versions.next_version_number(tenant)
        version = _version(str(tenant), f"concurrent-{index}", number)
        await uow.versions.add(version)
        return number


@pytest.mark.asyncio
async def test_concurrent_playbook_version_numbers_are_strictly_unique(
    organization_uow_factory,
) -> None:
    factory, _ = organization_uow_factory
    tenant = TenantId("tenant-concurrent-number")

    numbers = await asyncio.gather(
        *(_allocate_version(factory, tenant, index) for index in range(20))
    )

    assert sorted(numbers) == list(range(1, 21))


@pytest.mark.parametrize(
    ("table", "operation"),
    [
        ("company_playbook_versions", "update"),
        ("company_playbook_versions", "delete"),
        ("company_playbook_activations", "update"),
        ("company_playbook_activations", "delete"),
    ],
)
@pytest.mark.asyncio
async def test_playbook_persistence_rejects_every_update_and_delete_with_check_sqlstate(
    organization_uow_factory,
    table: str,
    operation: str,
) -> None:
    factory, engine = organization_uow_factory
    fact_kind = (
        "version" if table == "company_playbook_versions" else "activation"
    )
    suffix = f"immutable-{fact_kind}-{operation}"
    tenant = TenantId(f"tenant-{suffix}")
    version = _version(str(tenant), suffix, 1)
    activation = _activation(version, suffix)
    async with factory(tenant) as uow:
        await uow.versions.add(version)
        await uow.activations.add(activation)

    identity_column = (
        "playbook_version_id"
        if table == "company_playbook_versions"
        else "activation_id"
    )
    identity = (
        str(version.playbook_version_id)
        if table == "company_playbook_versions"
        else str(activation.activation_id)
    )
    statement = (
        f"UPDATE {table} SET tenant_id=tenant_id WHERE tenant_id=:tenant "
        f"AND {identity_column}=:identity"
        if operation == "update"
        else f"DELETE FROM {table} WHERE tenant_id=:tenant "
        f"AND {identity_column}=:identity"
    )

    with pytest.raises(DBAPIError) as captured:
        async with engine.begin() as connection:
            await connection.execute(
                text(statement), {"tenant": str(tenant), "identity": identity}
            )

    assert getattr(captured.value.orig, "sqlstate", None) == "23514"


@pytest.mark.asyncio
async def test_playbook_activation_is_unique_per_version_and_approval(
    organization_uow_factory,
) -> None:
    factory, _ = organization_uow_factory
    tenant = TenantId("tenant-activation-unique")
    first = _version(str(tenant), "activation-one", 1)
    second = _version(str(tenant), "activation-two", 2, base=first)
    original = _activation(first, "activation-shared")
    async with factory(tenant) as uow:
        await uow.versions.add(first)
        await uow.versions.add(second)
        await uow.activations.add(original)

    same_approval = _activation(
        second, "activation-other", approval_id=original.approval_id
    )
    with pytest.raises(IntegrityError):
        async with factory(tenant) as uow:
            await uow.activations.add(same_approval)

    same_version = _activation(first, "activation-second")
    with pytest.raises(IntegrityError):
        async with factory(tenant) as uow:
            await uow.activations.add(same_version)
