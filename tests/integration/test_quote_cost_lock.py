"""报价scope与冻结的真实隔离Postgres测试，外部端口受控且不装配生产。"""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text

from domains.costing import schemas
from domains.costing import service as costing
from domains.costing.permissions import (
    CostingActor,
    CostingScope,
    Phase1CostingAuthorizer,
)
from infra.db.costing_uow import SqlAlchemyCostingUnitOfWork
from shared.schemas.identifiers import CostSheetId, EmployeeId, TenantId
from shared.schemas.money import Money
from tests.integration.test_costing_quote_evidence import ControlledSources, policy
from tests.integration.test_need_units import NOW
from tests.integration.test_need_units import unit_db_case as unit_db_case
from tests.integration.test_need_units import unit_engine as unit_engine
from tests.integration.test_quote_context_locks import ContextCase
from tests.integration.test_quote_context_locks import context_case as context_case


@dataclass
class FreezeCase:
    """真实T2/Need/context/冻结服务，以独立连接检查提交结果。"""

    context: ContextCase
    actor: CostingActor
    cost_sheet_id: CostSheetId
    service: object
    application: object
    scope_command: object
    options: schemas.PricingOptions
    factory: object
    source_access: object
    completion_reader: object
    t2: object
    evidence: object

    @property
    def tenant_id(self):
        return self.context.tenant_id

    @property
    def actor_id(self):
        return self.context.actor_id

    @property
    def opportunity_id(self):
        return self.context.opportunity_id

    @property
    def provider(self):
        return self.context.provider

    async def scope(self, key="scope-1", command=None):
        return await self.application.confirm_scope(
            self.tenant_id,
            self.opportunity_id,
            self.cost_sheet_id,
            command or self.scope_command,
            actor_id=self.actor_id,
            idempotency_key=key,
        )

    async def count(self, kind: str) -> int:
        table = {
            "scope": "cost_scope_confirmations",
            "basis": "costing_quote_bases",
            "operation": "quote_creation_operations",
        }[kind]
        async with self.context.unit.sessions() as session:
            return await session.scalar(
                text(f"SELECT count(*) FROM {table} WHERE tenant_id=:tenant"),
                {"tenant": self.tenant_id},
            )

    async def change_material(self) -> None:
        await self.context.unit.demand.update_need_fields(
            self.tenant_id,
            self.context.unit.need_id,
            {
                "material": {
                    "value": "brass",
                    "quote": "brass",
                    "extracted_by": self.actor_id,
                }
            },
            source_message_id="msg_customer_changed",
            updated_by=self.actor_id,
        )


@pytest_asyncio.fixture
async def freeze_case(context_case: ContextCase) -> FreezeCase:
    assert hasattr(costing, "CostingFreezeService"), "缺少成本适用性与冻结公开端口"
    c = context_case
    actor = CostingActor(c.actor_id, "boss", CostingScope.TENANT)

    class Actors:
        async def read_current(self, tenant_id: TenantId, actor_id: EmployeeId):
            from infra.db.repositories.employees import EmployeeRepositoryImpl

            async with c.unit.sessions() as session:
                current = await EmployeeRepositoryImpl(session, tenant_id).get(
                    tenant_id, actor_id
                )
                return (
                    CostingActor(actor_id, current.role.value, CostingScope.TENANT)
                    if current and current.is_active
                    else None
                )

    class Access:
        denied = False
        on_read = None

        async def require(self, tenant_id, evidence, *, actor_id):
            from shared.errors import PermissionDenied

            assert tenant_id == c.tenant_id and actor_id == c.actor_id
            if self.on_read:
                await self.on_read()
            if self.denied:
                raise PermissionDenied("受控来源拒绝")

    class CompletionReader:
        receipt = None

        async def read(self, tenant_id, operation_id, *, actor_id):
            return self.receipt

    actors, access, completion = Actors(), Access(), CompletionReader()
    sources = ControlledSources(c.tenant_id, c.unit.reader.artifact_id)
    sources.allowed_actors = {c.actor_id}
    factory = lambda tid: SqlAlchemyCostingUnitOfWork(
        c.unit.sessions, tid, now=lambda: NOW
    )
    t2 = importlib.import_module(
        "domains.costing.quote_service"
    ).CostingQuoteServiceImpl(factory, sources, actor_reader=actors, now=lambda: NOW)
    await t2.confirm_policy(
        c.tenant_id, policy(sources.artifact), actor=actor, idempotency_key="policy"
    )
    evidence = await t2.confirm_price(
        c.tenant_id,
        schemas.SupplierPriceEvidenceCreate(
            kind="supplier_price",
            opportunity_id=c.opportunity_id,
            need_id=c.unit.need_id,
            supplier_ref="supplier-test",
            specification="Hinge 50mm steel packed in cartons",
            unit="pieces",
            destination="US",
            currency="USD",
            source_ref=sources.artifact,
            locator="line:1",
            amount=Decimal("1.25"),
            basis="quoted",
            quantity_min=100,
            quantity_max=1000,
            moq=100,
            quoted_at=NOW - timedelta(days=1),
            valid_until=NOW + timedelta(days=10),
        ),
        actor=actor,
        idempotency_key="price",
    )
    old = importlib.import_module("domains.costing.service_impl").CostingServiceImpl(
        factory, authorizer=Phase1CostingAuthorizer(c.tenant_id), now=lambda: NOW
    )
    created = await old.create_sheet(
        c.tenant_id,
        c.opportunity_id,
        schemas.CostSheetCreate(
            version_type="quoted",
            quantity=500,
            base_currency="USD",
            quote_currency="USD",
            fx_snapshot_id="fx_cost_test",
            fx_rates=[],
        ),
        actor=actor,
    )
    sheet_id = CostSheetId(created)
    await old.add_item(
        c.tenant_id,
        sheet_id,
        schemas.CostItemCreate(
            item_type="product_purchase",
            amount=Decimal("1.25"),
            currency="USD",
            price_basis="quoted",
            is_per_unit=True,
            source_ref=evidence.evidence_id,
            note=None,
        ),
        actor=actor,
    )
    sheet = await old.get_sheet(c.tenant_id, sheet_id, actor=actor)
    cov_id = await t2.confirm_coverage(
        c.tenant_id,
        sheet_id,
        schemas.CostCoverageCreate(
            expected_sheet_hash=sheet.content_hash,
            acquisition_mode="detail",
            decisions=tuple(
                schemas.CostCoverageDecision(
                    item_type=name,
                    applicable=name == "product_purchase",
                    reason="人工核对",
                    item_bindings=(
                        schemas.CostItemBinding(
                            item_sequence=1,
                            evidence_id=evidence.evidence_id,
                            source_line_ref=evidence.locator,
                            allocation_scope="order:one",
                        ),
                    )
                    if name == "product_purchase"
                    else (),
                )
                for name in costing.cost_item_type_values()
            ),
        ),
        actor=actor,
        idempotency_key="coverage",
    )
    freeze_uow = importlib.import_module(
        "infra.db.costing_freeze_uow"
    ).SqlAlchemyCostingFreezeUow
    freeze_factory = lambda tid: freeze_uow(
        c.unit.sessions,
        tid,
        now=lambda: NOW,
        lock_timeout_ms=1500,
        statement_timeout_ms=3000,
    )
    application = importlib.import_module("workflows.quote_approval.application")
    service = importlib.import_module(
        "domains.costing.freeze_service"
    ).CostingFreezeServiceImpl(
        freeze_factory,
        actors,
        application.DemandNeedFactsValidator(),
        access,
        completion,
        now=lambda: NOW,
    )
    from domains.quotations.service import StrictQuotePreparationPolicy

    app = application.QuotePreparationApplication(
        c.provider, service, StrictQuotePreparationPolicy(), actors
    )
    async with c.provider.open(
        c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.actor_id
    ) as context:
        command = schemas.CostScopeConfirmationCommand(
            coverage_id=cov_id,
            expected_sheet_hash=sheet.content_hash,
            expected_coverage_hash=cov_id,
            expected_need_facts_hash=context.need_facts_hash,
            terms=(),
            valid_until=NOW + timedelta(days=5),
            evidence_bindings=(
                schemas.CostScopeEvidenceBinding(
                    evidence_id=evidence.evidence_id,
                    evidence_hash=evidence.evidence_hash,
                    applicability_note="人工核对完整规格与包装适用",
                ),
            ),
        )
    options = schemas.PricingOptions(
        mode="manual",
        unit_price=Money(Decimal("2.00"), "USD"),
        rounding=schemas.RoundingPolicy(
            unit_places=2, total_places=2, strategy="ROUND_HALF_UP"
        ),
        quote_fx=None,
        algorithm_version="costing-v1",
    )
    return FreezeCase(
        c,
        actor,
        sheet_id,
        service,
        app,
        command,
        options,
        freeze_factory,
        access,
        completion,
        t2,
        evidence,
    )


async def test_scope_keeps_original_specification_and_binds_full_need(
    freeze_case: FreezeCase,
) -> None:
    c = freeze_case
    result = await c.scope()
    assert result.need_facts.material.value == "steel"
    assert result.need_facts.packaging.value == "carton"
    assert result.specification != c.evidence.specification
    assert (
        result.evidence_bindings[0].applicability_note == "人工核对完整规格与包装适用"
    )
    assert result.provenance.source_type.value == "employee_input"
    assert result.provenance.source_id == result.confirmation_id
    assert await c.scope() == result
    assert await c.count("scope") == 1


async def test_scope_rejects_changed_need_without_automatic_reconfirmation(
    freeze_case: FreezeCase,
) -> None:
    c = freeze_case
    await c.change_material()
    from domains.costing.errors import CostFreezeError

    with pytest.raises(CostFreezeError) as error:
        await c.scope()
    assert error.value.code == "scope_stale"
    assert await c.count("scope") == 0


async def test_scope_requires_complete_mapping_and_source_access_outside_locks(
    freeze_case: FreezeCase,
) -> None:
    c = freeze_case
    from domains.costing.errors import CostFreezeError, CostFreezePermissionError

    bad = c.scope_command.model_copy(update={"evidence_bindings": ()})
    with pytest.raises(CostFreezeError):
        await c.scope(command=bad)
    c.source_access.on_read = c.context.update_owner_manager
    c.source_access.denied = True
    with pytest.raises(CostFreezePermissionError):
        await c.scope()
    assert await c.count("scope") == 0


async def test_scope_key_conflict_and_concurrent_same_key(
    freeze_case: FreezeCase,
) -> None:
    c = freeze_case
    from domains.costing.errors import CostFreezeError

    one, two = await asyncio.gather(c.scope(), c.scope())
    assert one.confirmation_id == two.confirmation_id
    changed = c.scope_command.model_copy(
        update={"valid_until": NOW + timedelta(days=4)}
    )
    with pytest.raises(CostFreezeError) as error:
        await c.scope(command=changed)
    assert error.value.code == "idempotency_conflict"
    assert await c.count("scope") == 1


async def test_scope_new_manual_confirmation_required_after_material_change(
    freeze_case: FreezeCase,
) -> None:
    c = freeze_case
    first = await c.scope()
    await c.change_material()
    async with c.provider.open(
        c.tenant_id, c.opportunity_id, c.actor_id, prepared_by=c.actor_id
    ) as current:
        changed = c.scope_command.model_copy(
            update={"expected_need_facts_hash": current.need_facts_hash}
        )
    second = await c.scope(key="scope-new-material", command=changed)
    assert first.sheet_hash == second.sheet_hash
    assert first.need_facts.material.value == "steel"
    assert second.need_facts.material.value == "brass"
    assert first.content_hash != second.content_hash
    assert await c.count("scope") == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"valid_until": NOW - timedelta(seconds=1)},
        {"valid_until": NOW + timedelta(days=11)},
        {"expected_sheet_hash": "0" * 64},
        {"expected_coverage_hash": "0" * 64},
    ],
)
async def test_scope_expiry_and_stale_coverage_are_not_confirmed(
    freeze_case: FreezeCase, changes
) -> None:
    from domains.costing.errors import CostFreezeError

    c = freeze_case
    with pytest.raises(CostFreezeError):
        await c.scope(command=c.scope_command.model_copy(update=changes))
    assert await c.count("scope") == 0


async def test_role_change_during_source_read_is_rejected(
    freeze_case: FreezeCase,
) -> None:
    from domains.costing.errors import CostFreezePermissionError

    c = freeze_case

    async def change_role():
        await c.context.update_employee(c.actor_id, role="finance")

    c.source_access.on_read = change_role
    with pytest.raises(CostFreezePermissionError):
        await c.scope()
    assert await c.count("scope") == 0


async def test_scope_write_exception_rolls_back_entire_transaction(
    freeze_case: FreezeCase, monkeypatch
) -> None:
    c = freeze_case
    repo = importlib.import_module(
        "infra.db.repositories.costing_freeze"
    ).CostingFreezeRepositoryImpl
    original = repo.add_scope

    async def interrupted(self, tenant_id, record):
        await original(self, tenant_id, record)
        raise RuntimeError("受控插入后中断")

    monkeypatch.setattr(repo, "add_scope", interrupted)
    with pytest.raises(RuntimeError):
        await c.scope()
    assert await c.count("scope") == 0
    await c.context.update_owner_manager()


async def test_scope_is_immutable_and_nonempty_downgrade_is_refused(
    freeze_case: FreezeCase,
) -> None:
    from sqlalchemy.exc import DBAPIError

    from tests.integration.test_need_units import migrate

    c = freeze_case
    scope = await c.scope()
    for statement in (
        "UPDATE cost_scope_confirmations SET content_hash=content_hash WHERE tenant_id=:tenant",
        "DELETE FROM cost_scope_confirmations WHERE tenant_id=:tenant",
    ):
        with pytest.raises(DBAPIError):
            async with c.context.unit.sessions.begin() as session:
                await session.execute(text(statement), {"tenant": c.tenant_id})
    assert migrate(c.context.unit.engine, "downgrade", "0042") != 0
    assert (
        await c.service.get_scope(c.tenant_id, scope.confirmation_id, actor=c.actor)
        == scope
    )


@pytest.mark.parametrize("change", [{"tenant_id": "tenant-other"}, {"payload": []}])
async def test_scope_database_rejects_cross_tenant_fk_and_invalid_json(
    freeze_case: FreezeCase, change
) -> None:
    from sqlalchemy import insert, select
    from sqlalchemy.exc import DBAPIError

    from infra.db.tables import CostScopeConfirmationRow

    c = freeze_case
    await c.scope()
    async with c.context.unit.sessions() as session:
        original = (
            (
                await session.execute(
                    select(CostScopeConfirmationRow.__table__).where(
                        CostScopeConfirmationRow.tenant_id == c.tenant_id
                    )
                )
            )
            .mappings()
            .one()
        )
    values = (
        dict(original)
        | {"confirmation_id": "csc_bad", "idempotency_key": "invalid-copy"}
        | change
    )
    with pytest.raises(DBAPIError):
        async with c.context.unit.sessions.begin() as session:
            await session.execute(insert(CostScopeConfirmationRow).values(**values))
    assert await c.count("scope") == 1


@pytest.mark.parametrize("key", [" bad", "bad\x00key", "x" * 129])
async def test_scope_invalid_key_has_fixed_error(
    freeze_case: FreezeCase, key: str
) -> None:
    from domains.costing.errors import CostFreezeError

    with pytest.raises(CostFreezeError) as error:
        await freeze_case.scope(key=key)
    assert error.value.code == "invalid_input"
