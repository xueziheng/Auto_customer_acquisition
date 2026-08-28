"""可信 reader 为受控替身，事务/外键/只增/并发均使用真实隔离 Postgres。"""

from __future__ import annotations

import asyncio
import importlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from domains.costing import schemas
from domains.costing.permissions import CostingActor, CostingScope
from domains.costing.service import cost_item_type_values
from infra.db.costing_uow import SqlAlchemyCostingUnitOfWork
from infra.db.tables import OpportunityRow, RawArtifactRow
from shared.errors import IdempotencyConflict, PermissionDenied, ValidationError
from shared.schemas.identifiers import CostSheetId, OpportunityId, TenantId, new_id

NOW = datetime(2026, 8, 28, 9, tzinfo=UTC)
BOSS = CostingActor("emp-boss", "boss", CostingScope.TENANT)
FINANCE = CostingActor("emp-finance", "finance", CostingScope.TENANT)
TABLES = (
    "costing_policies",
    "costing_price_evidence",
    "costing_coverage",
    "costing_quote_fx",
)


def implementation():
    try:
        return importlib.import_module(
            "domains.costing.quote_service"
        ).CostingQuoteServiceImpl
    except (ModuleNotFoundError, AttributeError):
        pytest.fail("成本报价确认服务尚未实现")


class ControlledActors:
    """仅测试当前身份变化；不声称已装配生产员工服务。"""

    def __init__(self, tenant: TenantId):
        self.tenant = tenant
        self.actors = {BOSS.actor_id: BOSS, FINANCE.actor_id: FINANCE}

    async def read_current(self, tenant_id: TenantId, actor_id: str):
        return self.actors.get(actor_id) if tenant_id == self.tenant else None


class ControlledSources:
    """显式受控来源元数据，不访问/核验真实文档原文。"""

    def __init__(self, tenant: TenantId, artifact: str):
        self.tenant, self.artifact = tenant, artifact
        self.changes = {}
        self.on_read = None
        self.allowed_actors = {BOSS.actor_id, FINANCE.actor_id}

    async def read_verified(
        self, tenant_id: TenantId, source_ref: str, locator: str, *, actor_id: str
    ):
        if actor_id not in self.allowed_actors:
            raise PermissionDenied("当前员工无权读取这份原始资料")
        if self.on_read:
            await self.on_read()
        return schemas.SourceEvidence.model_validate(
            {
                "tenant_id": self.tenant,
                "source_ref": source_ref,
                "artifact_id": self.artifact,
                "content_hash": "a" * 64,
                "locator": locator,
                "observed_at": NOW - timedelta(days=1),
                "source_type": "upload",
                **self.changes,
            }
        )


async def setup(engine: AsyncEngine):
    service_type = implementation()
    tenant = TenantId(new_id("tn"))
    opportunity = OpportunityId(new_id("opp"))
    artifact = str(new_id("art"))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions.begin() as session:
        session.add(
            OpportunityRow(
                tenant_id=tenant,
                opportunity_id=opportunity,
                account_id=new_id("acc"),
                need_id="need-one",
                account_name="Controlled Customer",
                country="US",
                product_category="hardware",
            )
        )
        session.add(
            RawArtifactRow(
                tenant_id=tenant,
                artifact_id=artifact,
                kind="pdf",
                content_hash="a" * 64,
                size_bytes=10,
                mime_type="application/pdf",
                object_key=f"raw/{tenant}/{artifact}",
                uploaded_at=NOW - timedelta(days=1),
            )
        )
    actors, sources = ControlledActors(tenant), ControlledSources(tenant, artifact)
    factory = lambda tid: SqlAlchemyCostingUnitOfWork(sessions, tid, now=lambda: NOW)
    service = service_type(factory, sources, actor_reader=actors, now=lambda: NOW)
    return service, tenant, opportunity, artifact, actors, sources, factory, sessions


def policy(source: str, **changes: object):
    return schemas.PricingPolicyCreate.model_validate(
        {
            "category": None,
            "minimum_margin_rate": "0.10",
            "target_margin_rate": "0.20",
            "cost_groups": {name: "goods" for name in cost_item_type_values()},
            "effective_from": (NOW - timedelta(days=1)).isoformat(),
            "source_ref": source,
            **changes,
        }
    )


def expense(opportunity: str, source: str, **changes: object):
    return schemas.ExpenseEvidenceCreate.model_validate(
        {
            "kind": "confirmed_expense",
            "opportunity_id": opportunity,
            "item_type": "packaging",
            "allocation_scope": "order:one",
            "is_per_unit": False,
            "quantity": 100,
            "currency": "USD",
            "basis": "actual",
            "source_ref": source,
            "locator": "line:1",
            "amount": "12.50",
            "observed_at": "2026-08-27T09:00:00Z",
            "valid_until": None,
            **changes,
        }
    )


async def test_policy_price_fx_persist_provenance_and_conservative_idempotency(
    integration_engine,
):
    service, tenant, opportunity, artifact, _, _, factory, _ = await setup(
        integration_engine
    )
    command = policy(artifact)
    first, replay = await asyncio.gather(
        *[
            service.confirm_policy(
                tenant, command, actor=BOSS, idempotency_key="policy-one"
            )
            for _ in range(2)
        ]
    )
    assert first == replay
    assert first.field_provenance["minimum_margin_rate"].confirmed_by == BOSS.actor_id
    assert first.field_provenance["cost_groups.product_purchase"].source_id == artifact
    assert first.source.artifact_id == artifact
    assert await service.get_policy(tenant, None, actor=FINANCE) == first
    with pytest.raises(IdempotencyConflict):
        await service.confirm_policy(
            tenant,
            policy(artifact, target_margin_rate="0.30"),
            actor=BOSS,
            idempotency_key="policy-one",
        )

    price = await service.confirm_price(
        tenant,
        expense(opportunity, artifact),
        actor=FINANCE,
        idempotency_key="price-one",
    )
    assert price.basis == "actual"
    assert price.field_provenance["quantity"].confirmed_by == FINANCE.actor_id
    assert price.source.content_hash == "a" * 64
    fx_command = schemas.QuoteFxCreate.model_validate(
        {
            "base_currency": "USD",
            "quote_currency": "EUR",
            "rate": "0.9000000000000",
            "observed_at": "2026-08-27T09:00:00Z",
            "source_ref": artifact,
        }
    )
    fx = await service.confirm_quote_fx(
        tenant, fx_command, actor=FINANCE, idempotency_key="fx-one"
    )
    assert fx.rate == Decimal("0.9")
    assert fx.field_provenance["rate"].confirmed_by == FINANCE.actor_id
    assert await service.get_quote_fx(tenant, fx.fx_id, actor=FINANCE) == fx
    async with factory(tenant) as uow:
        loaded = await uow.prices.get(tenant, price.evidence_id)
        assert loaded.value == price
        assert await uow.prices.get_for_update(tenant, price.evidence_id) == loaded


@pytest.mark.parametrize("change", ["departure", "role_change"])
async def test_policy_requires_actual_current_boss_even_during_source_wait(
    integration_engine,
    change,
):
    service, tenant, _, artifact, actors, sources, _, sessions = await setup(
        integration_engine
    )
    with pytest.raises(PermissionDenied):
        await service.confirm_policy(
            tenant, policy(artifact), actor=FINANCE, idempotency_key="finance-denied"
        )
    with pytest.raises(PermissionDenied):
        await service.confirm_policy(
            tenant,
            policy(artifact),
            actor=CostingActor(FINANCE.actor_id, "boss", CostingScope.TENANT),
            idempotency_key="forged",
        )

    async def leave():
        if change == "departure":
            actors.actors.pop(BOSS.actor_id, None)
        else:
            actors.actors[BOSS.actor_id] = CostingActor(
                BOSS.actor_id, "finance", CostingScope.TENANT
            )

    sources.on_read = leave
    with pytest.raises(PermissionDenied):
        await service.confirm_policy(
            tenant, policy(artifact), actor=BOSS, idempotency_key="departed"
        )
    async with sessions() as session:
        count = await session.scalar(
            text("SELECT count(*) FROM costing_policies WHERE tenant_id=:tenant"),
            {"tenant": tenant},
        )
    assert count == 0


async def test_effective_policy_never_upgrades_legacy_unconfirmed_margin_rules(
    integration_engine,
):
    service, tenant, _, artifact, _, _, factory, _ = await setup(integration_engine)
    models = importlib.import_module("domains.costing.models")
    async with factory(tenant) as uow:
        await uow.margin_rules.set_rule(
            models.MarginRule(
                tenant_id=tenant,
                category=None,
                minimum_margin_rate=Decimal("0.1"),
                target_margin_rate=Decimal("0.2"),
                effective_from=NOW - timedelta(days=3),
            )
        )
    with pytest.raises(ValidationError):
        await service.get_policy(tenant, None, actor=BOSS)
    default = await service.confirm_policy(
        tenant, policy(artifact), actor=BOSS, idempotency_key="global"
    )
    future = policy(
        artifact,
        category="hardware",
        effective_from=(NOW + timedelta(days=1)).isoformat(),
    )
    await service.confirm_policy(tenant, future, actor=BOSS, idempotency_key="future")
    assert (
        await service.get_policy(tenant, "hardware", actor=BOSS)
    ).policy_id == default.policy_id
    category = await service.confirm_policy(
        tenant,
        policy(artifact, category="hardware"),
        actor=BOSS,
        idempotency_key="category",
    )
    assert (
        await service.get_policy(tenant, "hardware", actor=BOSS)
    ).policy_id == category.policy_id


async def test_four_evidence_tables_are_tenant_scoped(integration_engine):
    async with integration_engine.connect() as connection:
        names = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
        assert set(TABLES) <= set(names)
        for table in TABLES:
            columns = await connection.run_sync(
                lambda conn, table=table: inspect(conn).get_columns(table)
            )
            assert {column["name"] for column in columns} >= {
                "tenant_id",
                "idempotency_key",
                "request_hash",
                "payload",
                "confirmed_by",
                "confirmed_at",
            }


async def make_sheet(factory, tenant, opportunity, artifact, **changes):
    models = importlib.import_module("domains.costing.models")
    sheet = models.CostSheet(
        cost_sheet_id=CostSheetId(new_id("cst")),
        tenant_id=tenant,
        opportunity_id=opportunity,
        version_type=models.CostSheetVersion.QUOTED,
        version_number=1,
        quantity=100,
        base_currency="USD",
        quote_currency="EUR",
        created_at=NOW,
        fx_snapshot_id="fx-cost-old",
        items=[
            models.CostItem(
                **{
                    "item_type": models.CostItemType.PACKAGING,
                    "amount": importlib.import_module("shared.schemas.money").Money(
                        Decimal("12.50"), "USD"
                    ),
                    "price_basis": "actual",
                    "is_per_unit": False,
                    "source_ref": artifact,
                    "entered_by": FINANCE.actor_id,
                    "item_sequence": 7,
                    **changes,
                }
            )
        ],
    )
    async with factory(tenant) as uow:
        await uow.sheets.add(sheet)
    async with factory(tenant) as uow:
        return await uow.sheets.get(tenant, sheet.cost_sheet_id)


def coverage(sheet, evidence, **changes):
    from domains.costing.service import cost_sheet_content_hash

    item_type = sheet.items[0].item_type.value
    return schemas.CostCoverageCreate.model_validate(
        {
            "expected_sheet_hash": cost_sheet_content_hash(sheet),
            "acquisition_mode": "detail",
            "decisions": [
                {
                    "item_type": name,
                    "applicable": name == item_type,
                    "reason": "人工核对该订单不适用" if name != item_type else "",
                    "item_bindings": [
                        {
                            "item_sequence": 7,
                            "evidence_id": evidence.evidence_id,
                            "source_line_ref": evidence.locator,
                            "allocation_scope": "order:one",
                        }
                    ]
                    if name == item_type
                    else [],
                }
                for name in cost_item_type_values()
            ],
            **changes,
        }
    )


async def test_coverage_binds_persisted_sequence_and_actual_expense_without_moq(
    integration_engine,
):
    service, tenant, opportunity, artifact, _, _, factory, _ = await setup(
        integration_engine
    )
    price = await service.confirm_price(
        tenant, expense(opportunity, artifact), actor=FINANCE, idempotency_key="price"
    )
    sheet = await make_sheet(factory, tenant, opportunity, artifact)
    assert sheet.items[0].item_sequence == 7
    command = coverage(sheet, price)
    result = await service.confirm_coverage(
        tenant, sheet.cost_sheet_id, command, actor=FINANCE, idempotency_key="coverage"
    )
    assert len(result) == 64
    assert (
        await service.confirm_coverage(
            tenant,
            sheet.cost_sheet_id,
            command,
            actor=FINANCE,
            idempotency_key="coverage",
        )
        == result
    )
    async with factory(tenant) as uow:
        record = await uow.coverage.get(tenant, result)
        assert record.value.decisions[5].item_type == "packaging"
        assert record.value.decisions[5].item_bindings[0].item_sequence == 7
        assert (
            record.value.field_provenance["acquisition_mode"].confirmed_by
            == FINANCE.actor_id
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"is_per_unit": True},
        {"quantity": 200},
        {"currency": "EUR"},
        {"amount": "13"},
        {"basis": "quoted"},
        {"allocation_scope": "other-order"},
    ],
)
async def test_coverage_rejects_amount_currency_or_allocation_mismatch(
    integration_engine, changes
):
    service, tenant, opportunity, artifact, _, _, factory, _ = await setup(
        integration_engine
    )
    evidence = await service.confirm_price(
        tenant,
        expense(opportunity, artifact, **changes),
        actor=FINANCE,
        idempotency_key="price",
    )
    sheet = await make_sheet(factory, tenant, opportunity, artifact)
    with pytest.raises(ValidationError):
        await service.confirm_coverage(
            tenant,
            sheet.cost_sheet_id,
            coverage(sheet, evidence),
            actor=FINANCE,
            idempotency_key="coverage",
        )


@pytest.mark.parametrize(
    ("per_unit", "amount", "allowed"),
    [
        (True, "1.20", True),
        (False, "120.00", True),
        (False, "1.20", False),
        (True, "120.00", False),
    ],
)
async def test_supplier_price_binding_checks_unit_vs_order_amount(
    integration_engine, per_unit, amount, allowed
):
    from shared.schemas.money import Money

    service, tenant, opportunity, artifact, _, _, factory, _ = await setup(
        integration_engine
    )
    command = schemas.SupplierPriceEvidenceCreate.model_validate(
        {
            "kind": "supplier_price",
            "opportunity_id": opportunity,
            "need_id": "need-one",
            "supplier_ref": "supplier:one",
            "specification": "hinge-304",
            "unit": "piece",
            "destination": "US",
            "currency": "USD",
            "basis": "quoted",
            "source_ref": artifact,
            "locator": "line:1",
            "quantity_min": 100,
            "quantity_max": 500,
            "moq": 100,
            "amount": "1.20",
            "quoted_at": "2026-08-27T09:00:00Z",
            "valid_until": "2026-09-27T09:00:00Z",
        }
    )
    evidence = await service.confirm_price(
        tenant, command, actor=FINANCE, idempotency_key="price"
    )
    models = importlib.import_module("domains.costing.models")
    sheet = await make_sheet(
        factory,
        tenant,
        opportunity,
        artifact,
        item_type=models.CostItemType.PRODUCT_PURCHASE,
        amount=Money(Decimal(amount), "USD"),
        price_basis="quoted",
        is_per_unit=per_unit,
    )
    if allowed:
        result = await service.confirm_coverage(
            tenant,
            sheet.cost_sheet_id,
            coverage(sheet, evidence),
            actor=FINANCE,
            idempotency_key="coverage",
        )
        assert len(result) == 64
    else:
        with pytest.raises(ValidationError):
            await service.confirm_coverage(
                tenant,
                sheet.cost_sheet_id,
                coverage(sheet, evidence),
                actor=FINANCE,
                idempotency_key="coverage",
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"tenant_id": "other-tenant"},
        {"content_hash": "b" * 64},
        {"source_ref": "other-ref"},
        {"locator": "other-line"},
        {"source_type": "agent_inference"},
        {"source_type": "unknown"},
        {"observed_at": NOW + timedelta(hours=1)},
        {"source_type": "web_page", "source_url": None},
    ],
)
async def test_untrusted_source_projection_never_persists_confirmation(
    integration_engine, changes
):
    service, tenant, opportunity, artifact, _, sources, _, sessions = await setup(
        integration_engine
    )
    sources.changes = changes
    with pytest.raises(ValidationError):
        await service.confirm_price(
            tenant,
            expense(opportunity, artifact),
            actor=FINANCE,
            idempotency_key="invalid-source",
        )
    async with sessions() as session:
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM costing_price_evidence WHERE tenant_id=:tenant"
                ),
                {"tenant": tenant},
            )
            == 0
        )


async def test_original_source_acl_is_not_implied_by_costing_role(integration_engine):
    service, tenant, opportunity, artifact, _, sources, _, sessions = await setup(
        integration_engine
    )
    sources.allowed_actors = {BOSS.actor_id}
    with pytest.raises(ValidationError):
        await service.confirm_price(
            tenant,
            expense(opportunity, artifact),
            actor=FINANCE,
            idempotency_key="source-denied",
        )
    async with sessions() as session:
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM costing_price_evidence WHERE tenant_id=:tenant"
                ),
                {"tenant": tenant},
            )
            == 0
        )


async def all_records(engine):
    setup_result = await setup(engine)
    service, tenant, opportunity, artifact, _, _, factory, _ = setup_result
    policy_view = await service.confirm_policy(
        tenant, policy(artifact), actor=BOSS, idempotency_key="policy"
    )
    price = await service.confirm_price(
        tenant, expense(opportunity, artifact), actor=FINANCE, idempotency_key="price"
    )
    fx = await service.confirm_quote_fx(
        tenant,
        schemas.QuoteFxCreate(
            base_currency="USD",
            quote_currency="EUR",
            rate=Decimal("0.9"),
            source_ref=artifact,
            observed_at=NOW,
        ),
        actor=FINANCE,
        idempotency_key="fx",
    )
    sheet = await make_sheet(factory, tenant, opportunity, artifact)
    coverage_id = await service.confirm_coverage(
        tenant,
        sheet.cost_sheet_id,
        coverage(sheet, price),
        actor=FINANCE,
        idempotency_key="coverage",
    )
    return (
        setup_result,
        sheet,
        (policy_view.policy_id, price.evidence_id, coverage_id, fx.fx_id),
    )


async def test_all_four_tables_reject_update_delete_and_cross_tenant_foreign_keys(
    integration_engine,
):
    result, _, identities = await all_records(integration_engine)
    _, tenant, _, _, _, _, factory, sessions = result
    other = TenantId(new_id("tn"))
    tables_module = importlib.import_module("infra.db.tables")
    rows = [
        tables_module.CostingPolicyRow,
        tables_module.CostingPriceEvidenceRow,
        tables_module.CostingCoverageRow,
        tables_module.CostingQuoteFxRow,
    ]
    for table, row, identity in zip(TABLES, rows, identities, strict=True):
        async with sessions() as session:
            loaded = (
                await session.execute(select(row).where(row.tenant_id == tenant))
            ).scalar_one()
            values = {
                column.name: getattr(loaded, column.name)
                for column in row.__table__.columns
            }
            values["tenant_id"] = other
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(row.__table__.insert().values(**values))
            for mutation in (
                f"UPDATE {table} SET confirmed_by='tampered' WHERE tenant_id=:tenant",
                f"DELETE FROM {table} WHERE tenant_id=:tenant",
            ):
                with pytest.raises(DBAPIError):
                    async with session.begin_nested():
                        await session.execute(text(mutation), {"tenant": tenant})
        async with factory(other) as uow:
            repo = getattr(
                uow,
                {
                    "costing_policies": "policies",
                    "costing_price_evidence": "prices",
                    "costing_coverage": "coverage",
                    "costing_quote_fx": "quote_fx",
                }[table],
            )
            assert await repo.get(other, identity) is None


async def test_coverage_rejects_stale_sheet_hash_after_concurrent_append(
    integration_engine,
):
    from dataclasses import replace

    service, tenant, opportunity, artifact, _, _, factory, _ = await setup(
        integration_engine
    )
    evidence = await service.confirm_price(
        tenant, expense(opportunity, artifact), actor=FINANCE, idempotency_key="price"
    )
    sheet = await make_sheet(factory, tenant, opportunity, artifact)
    original = coverage(sheet, evidence)
    async with factory(tenant) as uow:
        changed = await uow.sheets.get_for_update(tenant, sheet.cost_sheet_id)
        changed.items.append(replace(changed.items[0], item_sequence=12))
        await uow.sheets.update(changed)
    with pytest.raises(ValidationError, match="改变"):
        await service.confirm_coverage(
            tenant,
            sheet.cost_sheet_id,
            original,
            actor=FINANCE,
            idempotency_key="stale",
        )


@pytest.mark.parametrize(("line_two", "allowed"), [("line:1", False), ("line:2", True)])
async def test_same_artifact_distinct_lines_allowed_but_duplicate_line_rejected(
    integration_engine, line_two, allowed
):
    from dataclasses import replace

    service, tenant, opportunity, artifact, _, _, factory, _ = await setup(
        integration_engine
    )
    first = await service.confirm_price(
        tenant,
        expense(opportunity, artifact),
        actor=FINANCE,
        idempotency_key="price-one",
    )
    second = await service.confirm_price(
        tenant,
        expense(opportunity, artifact, locator=line_two),
        actor=FINANCE,
        idempotency_key="price-two",
    )
    sheet = await make_sheet(factory, tenant, opportunity, artifact)
    sheet.items.append(replace(sheet.items[0], item_sequence=12))
    async with factory(tenant) as uow:
        await uow.sheets.update(sheet)
    payload = coverage(sheet, first).model_dump(mode="json")
    payload["decisions"][5]["item_bindings"].append(
        {
            "item_sequence": 12,
            "evidence_id": second.evidence_id,
            "source_line_ref": line_two,
            "allocation_scope": "order:one",
        }
    )
    command = schemas.CostCoverageCreate.model_validate(payload)
    if allowed:
        assert (
            len(
                await service.confirm_coverage(
                    tenant,
                    sheet.cost_sheet_id,
                    command,
                    actor=FINANCE,
                    idempotency_key="coverage",
                )
            )
            == 64
        )
    else:
        with pytest.raises(ValidationError, match="重复"):
            await service.confirm_coverage(
                tenant,
                sheet.cost_sheet_id,
                command,
                actor=FINANCE,
                idempotency_key="coverage",
            )


async def test_policy_same_key_replay_does_not_hide_current_role_revocation(
    integration_engine,
):
    service, tenant, _, artifact, actors, _, _, _ = await setup(integration_engine)
    await service.confirm_policy(
        tenant, policy(artifact), actor=BOSS, idempotency_key="policy"
    )
    actors.actors[BOSS.actor_id] = CostingActor(
        BOSS.actor_id, "finance", CostingScope.TENANT
    )
    with pytest.raises(PermissionDenied):
        await service.confirm_policy(
            tenant, policy(artifact), actor=BOSS, idempotency_key="policy"
        )


async def test_duplicate_coverage_with_new_key_is_explicit_conflict(integration_engine):
    service, tenant, opportunity, artifact, _, _, factory, _ = await setup(
        integration_engine
    )
    price = await service.confirm_price(
        tenant, expense(opportunity, artifact), actor=FINANCE, idempotency_key="price"
    )
    sheet = await make_sheet(factory, tenant, opportunity, artifact)
    command = coverage(sheet, price)
    first = await service.confirm_coverage(
        tenant, sheet.cost_sheet_id, command, actor=FINANCE, idempotency_key="first"
    )
    with pytest.raises(IdempotencyConflict):
        await service.confirm_coverage(
            tenant,
            sheet.cost_sheet_id,
            command,
            actor=FINANCE,
            idempotency_key="second",
        )
    async with factory(tenant) as uow:
        assert (await uow.coverage.get(tenant, first)).idempotency_key == "first"


@pytest.mark.parametrize(
    ("item_type", "mode", "allowed"),
    [
        ("customer_acquisition", "summary", True),
        ("customer_acquisition", "detail", False),
        ("contact_data_cost", "detail", True),
        ("contact_data_cost", "summary", False),
    ],
)
async def test_acquisition_summary_and_detail_are_mutually_exclusive(
    integration_engine, item_type, mode, allowed
):
    service, tenant, opportunity, artifact, _, _, factory, _ = await setup(
        integration_engine
    )
    price = await service.confirm_price(
        tenant,
        expense(opportunity, artifact, item_type=item_type),
        actor=FINANCE,
        idempotency_key="price",
    )
    models = importlib.import_module("domains.costing.models")
    sheet = await make_sheet(
        factory, tenant, opportunity, artifact, item_type=models.CostItemType(item_type)
    )
    command = coverage(sheet, price, acquisition_mode=mode)
    if allowed:
        assert (
            len(
                await service.confirm_coverage(
                    tenant,
                    sheet.cost_sheet_id,
                    command,
                    actor=FINANCE,
                    idempotency_key="coverage",
                )
            )
            == 64
        )
    else:
        with pytest.raises(ValidationError, match="口径"):
            await service.confirm_coverage(
                tenant,
                sheet.cost_sheet_id,
                command,
                actor=FINANCE,
                idempotency_key="coverage",
            )


async def test_not_applicable_cannot_hide_existing_confirmed_cost(integration_engine):
    service, tenant, opportunity, artifact, _, _, factory, _ = await setup(
        integration_engine
    )
    price = await service.confirm_price(
        tenant, expense(opportunity, artifact), actor=FINANCE, idempotency_key="price"
    )
    sheet = await make_sheet(factory, tenant, opportunity, artifact)
    payload = coverage(sheet, price).model_dump(mode="json")
    payload["decisions"][5].update(
        applicable=False, reason="员工误标不适用", item_bindings=[]
    )
    with pytest.raises(ValidationError, match="遗漏"):
        await service.confirm_coverage(
            tenant,
            sheet.cost_sheet_id,
            schemas.CostCoverageCreate.model_validate(payload),
            actor=FINANCE,
            idempotency_key="hidden",
        )


async def test_price_and_coverage_references_cannot_target_another_tenant(
    integration_engine,
):
    result, _, _ = await all_records(integration_engine)
    _, tenant, _, _, _, _, _, sessions = result
    other, other_sheet, _ = await all_records(integration_engine)
    other_opportunity = other[2]
    tables_module = importlib.import_module("infra.db.tables")
    for row, field, foreign_id, identity in (
        (
            tables_module.CostingPriceEvidenceRow,
            "opportunity_id",
            other_opportunity,
            "evidence_id",
        ),
        (
            tables_module.CostingCoverageRow,
            "cost_sheet_id",
            other_sheet.cost_sheet_id,
            "coverage_id",
        ),
    ):
        async with sessions() as session:
            loaded = (
                await session.execute(select(row).where(row.tenant_id == tenant))
            ).scalar_one()
            values = {
                column.name: getattr(loaded, column.name)
                for column in row.__table__.columns
            }
            values.update(
                {
                    field: foreign_id,
                    identity: new_id("ce"),
                    "idempotency_key": "cross-parent",
                }
            )
            with pytest.raises(DBAPIError) as error:
                async with session.begin_nested():
                    await session.execute(row.__table__.insert().values(**values))
            assert getattr(error.value.orig, "sqlstate", None) == "23503"


@pytest.mark.parametrize(
    "changes",
    [
        {"basis": "indicative"},
        {"quantity_min": 200},
        {"valid_until": "2026-08-28T08:00:00Z"},
    ],
)
async def test_supplier_reference_price_expiry_or_wrong_quantity_never_supports_coverage(
    integration_engine, changes
):
    from shared.schemas.money import Money

    service, tenant, opportunity, artifact, _, _, factory, _ = await setup(
        integration_engine
    )
    command = schemas.SupplierPriceEvidenceCreate.model_validate(
        {
            "kind": "supplier_price",
            "opportunity_id": opportunity,
            "need_id": "need-one",
            "supplier_ref": "supplier:one",
            "specification": "hinge-304",
            "unit": "piece",
            "destination": "US",
            "currency": "USD",
            "basis": "quoted",
            "source_ref": artifact,
            "locator": "line:1",
            "quantity_min": 100,
            "quantity_max": 500,
            "moq": 100,
            "amount": "1.20",
            "quoted_at": "2026-08-27T09:00:00Z",
            "valid_until": "2026-09-27T09:00:00Z",
            **changes,
        }
    )
    evidence = await service.confirm_price(
        tenant, command, actor=FINANCE, idempotency_key="price"
    )
    models = importlib.import_module("domains.costing.models")
    sheet = await make_sheet(
        factory,
        tenant,
        opportunity,
        artifact,
        item_type=models.CostItemType.PRODUCT_PURCHASE,
        amount=Money(Decimal("1.20"), "USD"),
        price_basis=command.basis,
        is_per_unit=True,
    )
    with pytest.raises(ValidationError):
        await service.confirm_coverage(
            tenant,
            sheet.cost_sheet_id,
            coverage(sheet, evidence),
            actor=FINANCE,
            idempotency_key="coverage",
        )
