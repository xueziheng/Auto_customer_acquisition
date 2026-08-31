"""成本表 PostgreSQL 持久化与租户隔离。"""

from __future__ import annotations

import asyncio
import importlib
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from domains.costing.errors import LockedCostSheetError
from infra.db.tables import OpportunityRow
from shared.errors import TenantIsolationViolation, ValidationError
from shared.schemas.identifiers import (
    CostSheetId,
    EmployeeId,
    FxSnapshotId,
    OpportunityId,
    TenantId,
    new_id,
)
from shared.schemas.money import CurrencyCode, FxRate, Money, PriceBasis

NOW = datetime(2026, 8, 23, 12, tzinfo=UTC)
TENANT = TenantId("tenant-costing-a")
OTHER_TENANT = TenantId("tenant-costing-b")
_REPO_ROOT = Path(__file__).resolve().parents[2]
_models = importlib.import_module("domains.costing.models")
CostItem = _models.CostItem
CostItemType = _models.CostItemType
CostSheet = _models.CostSheet
CostSheetVersion = _models.CostSheetVersion
MarginRule = _models.MarginRule
RiskAcceptance = _models.RiskAcceptance


def _costing_schema(conn: Connection) -> dict[str, dict[str, bool]]:
    inspector = inspect(conn)
    return {
        table: {
            str(column["name"]): not bool(column["nullable"])
            for column in inspector.get_columns(table)
        }
        for table in (
            "cost_sheets",
            "cost_items",
            "cost_sheet_fx_rates",
            "margin_rules",
        )
    }


async def test_costing_tables_persist_every_business_row_with_tenant_scope(
    integration_engine: AsyncEngine,
) -> None:
    async with integration_engine.connect() as connection:
        schema = await connection.run_sync(_costing_schema)

    assert set(schema["cost_sheets"]) == {
        "tenant_id",
        "cost_sheet_id",
        "opportunity_id",
        "version_type",
        "version_number",
        "quantity",
        "base_currency",
        "quote_currency",
        "fx_snapshot_id",
        "created_by",
        "created_at",
        "locked_at",
        "risk_accepted_by",
        "risk_accepted_at",
        "risk_justification",
        "source_sourcing_case_id",
        "source_option_id",
        "source_product_id",
        "source_candidate_id",
    }
    assert set(schema["cost_items"]) == {
        "tenant_id",
        "cost_sheet_id",
        "item_sequence",
        "item_type",
        "amount",
        "currency",
        "price_basis",
        "is_per_unit",
        "note",
        "source_ref",
        "entered_by",
    }
    assert set(schema["cost_sheet_fx_rates"]) == {
        "tenant_id",
        "cost_sheet_id",
        "base_currency",
        "quote_currency",
        "rate",
        "observed_at",
        "source",
    }
    assert set(schema["margin_rules"]) == {
        "tenant_id",
        "margin_rule_id",
        "category",
        "minimum_margin_rate",
        "target_margin_rate",
        "effective_from",
    }
    assert all(columns["tenant_id"] for columns in schema.values())


def _run_alembic(db_url: str, *command: str) -> None:
    result = subprocess.run(
        [sys.executable, "scripts/run_alembic.py", *command],
        cwd=_REPO_ROOT,
        env={**os.environ, "DATABASE_URL": db_url},
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, "成本表迁移往返失败（不输出连接内容）"


@pytest.mark.asyncio
async def test_costing_migration_downgrade_and_upgrade_roundtrip(db_url: str) -> None:
    from infra.db.session import create_engine_from

    tables = {
        "cost_sheets",
        "cost_items",
        "cost_sheet_fx_rates",
        "margin_rules",
    }
    try:
        _run_alembic(db_url, "downgrade", "0029")
        downgraded = create_engine_from(db_url)
        try:
            async with downgraded.connect() as connection:
                names = set(
                    await connection.run_sync(
                        lambda sync: inspect(sync).get_table_names()
                    )
                )
            assert not tables & names
        finally:
            await downgraded.dispose()

        _run_alembic(db_url, "upgrade", "head")
        upgraded = create_engine_from(db_url)
        try:
            async with upgraded.connect() as connection:
                names = set(
                    await connection.run_sync(
                        lambda sync: inspect(sync).get_table_names()
                    )
                )
            assert tables <= names
        finally:
            await upgraded.dispose()
    finally:
        _run_alembic(db_url, "upgrade", "head")


def _repository_types() -> tuple[type[Any], type[Any]]:
    try:
        module = importlib.import_module("infra.db.repositories.costing")
    except ModuleNotFoundError:
        pytest.fail("成本表 PostgreSQL Repository 尚未实现")
    return module.CostSheetRepositoryImpl, module.MarginRuleRepositoryImpl


def _opportunity(tenant_id: TenantId, opportunity_id: OpportunityId) -> OpportunityRow:
    return OpportunityRow(
        opportunity_id=str(opportunity_id),
        tenant_id=str(tenant_id),
        account_id=str(new_id("acc")),
        account_name="Costing Test Account",
        country="US",
        need_id=str(new_id("need")),
        product_category="hardware",
    )


def _sheet(
    tenant_id: TenantId,
    opportunity_id: OpportunityId,
    *,
    version_number: int = 1,
    locked_at: datetime | None = None,
) -> Any:
    return CostSheet(
        cost_sheet_id=CostSheetId(new_id("cst")),
        tenant_id=tenant_id,
        opportunity_id=opportunity_id,
        version_type=CostSheetVersion.QUOTED,
        version_number=version_number,
        quantity=100,
        base_currency="USD",
        quote_currency="EUR",
        created_at=NOW,
        items=[
            CostItem(
                item_type=CostItemType.PRODUCT_PURCHASE,
                amount=Money(Decimal("7.125"), CurrencyCode("CNY")),
                price_basis=PriceBasis.QUOTED,
                is_per_unit=True,
                source_ref="supplier-quote-one",
                entered_by=EmployeeId("employee-costing"),
            ),
            CostItem(
                item_type=CostItemType.PACKAGING,
                amount=Money(Decimal("0.25"), CurrencyCode("USD")),
                price_basis=PriceBasis.INDICATIVE,
                is_per_unit=True,
                note="待员工确认",
            ),
        ],
        fx_snapshot_id=FxSnapshotId("fx-snapshot-costing"),
        fx_rates=(
            FxRate(
                base=CurrencyCode("CNY"),
                quote=CurrencyCode("USD"),
                rate=Decimal("0.140000000000"),
                observed_at=NOW - timedelta(hours=1),
                source="manual-fx-snapshot",
            ),
        ),
        created_by=EmployeeId("employee-costing"),
        locked_at=locked_at,
        risk_acceptance=RiskAcceptance(
            accepted_by=EmployeeId("boss-costing"),
            accepted_at=NOW,
            justification="客户要求当天给预算范围，已逐次审批参考价风险。",
        ),
    )


@pytest.mark.asyncio
async def test_cost_sheet_repository_roundtrips_items_fx_and_risk_without_cross_tenant_access(
    integration_session: AsyncSession,
) -> None:
    repository_type, _ = _repository_types()
    opportunity_id = OpportunityId(new_id("opp"))
    integration_session.add(_opportunity(TENANT, opportunity_id))
    await integration_session.flush()
    repository = repository_type(integration_session, TENANT)
    sheet = _sheet(TENANT, opportunity_id)

    await repository.add(sheet)
    loaded = await repository.get(TENANT, sheet.cost_sheet_id)
    versions = await repository.list_by_opportunity(TENANT, opportunity_id)

    assert loaded == sheet
    assert versions == [sheet]
    with pytest.raises(TenantIsolationViolation):
        await repository.get(OTHER_TENANT, sheet.cost_sheet_id)


@pytest.mark.asyncio
async def test_locked_cost_sheet_rejects_repository_and_direct_child_mutation(
    integration_session: AsyncSession,
) -> None:
    repository_type, _ = _repository_types()
    opportunity_id = OpportunityId(new_id("opp"))
    integration_session.add(_opportunity(TENANT, opportunity_id))
    await integration_session.flush()
    repository = repository_type(integration_session, TENANT)
    sheet = _sheet(TENANT, opportunity_id)
    await repository.add(sheet)
    sheet.locked_at = NOW + timedelta(minutes=1)
    await repository.update(sheet)
    await integration_session.commit()

    sheet.items.append(
        CostItem(
            item_type=CostItemType.INSURANCE,
            amount=Money(Decimal("0.01"), CurrencyCode("USD")),
            price_basis=PriceBasis.QUOTED,
            is_per_unit=True,
            source_ref="supplier-quote-one",
            entered_by=EmployeeId("employee-costing"),
        )
    )
    with pytest.raises(LockedCostSheetError):
        await repository.update(sheet)
    await integration_session.rollback()

    with pytest.raises(DBAPIError):
        await integration_session.execute(
            text(
                "UPDATE cost_items SET amount = amount + 1 "
                "WHERE tenant_id = :tenant_id AND cost_sheet_id = :cost_sheet_id"
            ),
            {
                "tenant_id": str(TENANT),
                "cost_sheet_id": str(sheet.cost_sheet_id),
            },
        )


@pytest.mark.asyncio
async def test_database_rejects_cost_item_outside_canonical_vocabulary(
    integration_session: AsyncSession,
) -> None:
    repository_type, _ = _repository_types()
    opportunity_id = OpportunityId(new_id("opp"))
    integration_session.add(_opportunity(TENANT, opportunity_id))
    await integration_session.flush()
    sheet = _sheet(TENANT, opportunity_id)
    await repository_type(integration_session, TENANT).add(sheet)

    with pytest.raises(DBAPIError):
        await integration_session.execute(
            text(
                "INSERT INTO cost_items "
                "(tenant_id, cost_sheet_id, item_sequence, item_type, amount, "
                "currency, price_basis, is_per_unit) VALUES "
                "(:tenant_id, :cost_sheet_id, 99, 'invented_cost', 1, "
                "'USD', 'quoted', true)"
            ),
            {
                "tenant_id": str(TENANT),
                "cost_sheet_id": str(sheet.cost_sheet_id),
            },
        )


@pytest.mark.asyncio
async def test_locking_sheet_serializes_against_concurrent_child_insert(
    integration_engine: AsyncEngine,
) -> None:
    repository_type, _ = _repository_types()
    opportunity_id = OpportunityId(new_id("opp"))
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    async with factory() as seed, seed.begin():
        seed.add(_opportunity(TENANT, opportunity_id))
        await seed.flush()
        sheet = _sheet(TENANT, opportunity_id)
        await repository_type(seed, TENANT).add(sheet)

    async with factory() as locker, factory() as writer:
        await locker.begin()
        sheet.locked_at = NOW + timedelta(minutes=1)
        await repository_type(locker, TENANT).update(sheet)

        insert_task = asyncio.create_task(
            writer.execute(
                text(
                    "INSERT INTO cost_items "
                    "(tenant_id, cost_sheet_id, item_sequence, item_type, amount, "
                    "currency, price_basis, is_per_unit) VALUES "
                    "(:tenant_id, :cost_sheet_id, 99, 'insurance', 1, "
                    "'USD', 'quoted', true)"
                ),
                {
                    "tenant_id": str(TENANT),
                    "cost_sheet_id": str(sheet.cost_sheet_id),
                },
            )
        )
        await asyncio.sleep(0.05)
        assert insert_task.done() is False

        await locker.commit()
        with pytest.raises(DBAPIError):
            await insert_task
        await writer.rollback()


@pytest.mark.asyncio
async def test_concurrent_version_allocation_is_unique_within_opportunity(
    integration_engine: AsyncEngine,
) -> None:
    repository_type, _ = _repository_types()
    opportunity_id = OpportunityId(new_id("opp"))
    async with AsyncSession(integration_engine, expire_on_commit=False) as session:
        session.add(_opportunity(TENANT, opportunity_id))
        await session.commit()
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)

    async def create_one() -> int:
        async with factory() as session, session.begin():
            repository = repository_type(session, TENANT)
            version = await repository.next_version_number(
                TENANT, opportunity_id, CostSheetVersion.QUOTED.value
            )
            await repository.add(
                _sheet(TENANT, opportunity_id, version_number=version)
            )
            return version

    versions = await asyncio.gather(*(create_one() for _ in range(8)))

    assert sorted(versions) == list(range(1, 9))


@pytest.mark.asyncio
async def test_margin_rule_repository_prefers_category_and_never_invents_default(
    integration_session: AsyncSession,
) -> None:
    _, repository_type = _repository_types()
    repository = repository_type(integration_session, TENANT, now=lambda: NOW)
    global_rule = MarginRule(
        tenant_id=TENANT,
        minimum_margin_rate=Decimal("0.10"),
        target_margin_rate=Decimal("0.20"),
        effective_from=NOW - timedelta(days=2),
    )
    category_rule = MarginRule(
        tenant_id=TENANT,
        category="hardware",
        minimum_margin_rate=Decimal("0.15"),
        target_margin_rate=Decimal("0.25"),
        effective_from=NOW - timedelta(days=1),
    )
    future_rule = MarginRule(
        tenant_id=TENANT,
        category="hardware",
        minimum_margin_rate=Decimal("0.30"),
        target_margin_rate=Decimal("0.40"),
        effective_from=NOW + timedelta(days=1),
    )
    for rule in (global_rule, category_rule, future_rule):
        await repository.set_rule(rule)

    assert await repository.get_effective(TENANT, "hardware") == category_rule
    assert await repository.get_effective(TENANT, "other") == global_rule
    with pytest.raises(TenantIsolationViolation):
        await repository.get_effective(OTHER_TENANT, None)

    empty_repository = repository_type(
        integration_session,
        TenantId("tenant-without-margin-rule"),
        now=lambda: NOW,
    )
    with pytest.raises(ValidationError, match="利润规则未配置"):
        await empty_repository.get_effective(
            TenantId("tenant-without-margin-rule"), None
        )
