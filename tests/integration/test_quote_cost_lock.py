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
from shared.schemas.quote_creation import (
    QuoteCreationCompletion,
    QuoteCreationIntent,
    QuoteRoundingInput,
    QuoteTerm,
)
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

    async def intent(self, *, scope=None, **changes: object) -> QuoteCreationIntent:
        scope = scope or await self.scope()
        async with self.provider.open(
            self.tenant_id,
            self.opportunity_id,
            self.actor_id,
            prepared_by=self.actor_id,
        ) as context:
            values = {
                "tenant_id": self.tenant_id,
                "prepared_by": self.actor_id,
                "opportunity_id": self.opportunity_id,
                "cost_sheet_id": self.cost_sheet_id,
                "expected_context_hash": context.context_hash,
                "expected_sheet_hash": scope.sheet_hash,
                "valid_until": scope.valid_until,
                "unit_price": self.options.unit_price,
                "rounding": QuoteRoundingInput(**self.options.rounding.model_dump()),
                "quote_fx_ref": None,
                "terms": scope.terms,
                "replaces_quote_id": None,
                "expected_quote_version": None,
                "scope_confirmation_id": scope.confirmation_id,
                "scope_confirmation_hash": scope.content_hash,
            }
        return QuoteCreationIntent(**(values | changes))

    async def freeze(
        self,
        *,
        key: str,
        intent: QuoteCreationIntent | None = None,
        options: schemas.PricingOptions | None = None,
    ) -> schemas.FrozenCostBasis:
        assert hasattr(self.application, "freeze"), "缺少同lease冻结应用行为"
        return await self.application.freeze(
            self.tenant_id,
            intent or await self.intent(),
            options or self.options,
            actor_id=self.actor_id,
            idempotency_key=key,
        )

    def receipt(
        self, basis: schemas.FrozenCostBasis, **changes: object
    ) -> QuoteCreationCompletion:
        return QuoteCreationCompletion(
            **(
                {
                    "tenant_id": self.tenant_id,
                    "operation_id": basis.operation_id,
                    "request_hash": basis.request_hash,
                    "basis_id": basis.basis_id,
                    "quote_id": "quote_controlled",
                    "quote_version": 1,
                    "quote_content_hash": "d" * 64,
                    "replaces_quote_id": None,
                    "replaced_quote_version": None,
                }
                | changes
            )
        )

    async def locked_at(self):
        async with self.context.unit.sessions() as session:
            return await session.scalar(
                text(
                    "SELECT locked_at FROM cost_sheets WHERE tenant_id=:tenant AND cost_sheet_id=:sheet"
                ),
                {"tenant": self.tenant_id, "sheet": self.cost_sheet_id},
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


async def test_pending_cannot_be_bypassed_with_new_key(freeze_case: FreezeCase) -> None:
    from domains.costing.errors import CostFreezeError

    c = freeze_case
    first = await c.freeze(key="creation-1")
    with pytest.raises(CostFreezeError) as error:
        await c.freeze(key="creation-2")
    assert error.value.code == "operation_pending"
    assert (await c.freeze(key="creation-1")).basis_id == first.basis_id
    assert await c.count("operation") == await c.count("basis") == 1
    assert await c.locked_at() == NOW
    assert first.price_evidence[0].specification == c.evidence.specification
    assert first.scope_confirmation.need_facts == first.need_facts


async def test_freeze_concurrent_same_key_has_one_committed_effect(
    freeze_case: FreezeCase,
) -> None:
    c = freeze_case
    intent = await c.intent()
    first, second = await asyncio.gather(
        c.freeze(key="same", intent=intent), c.freeze(key="same", intent=intent)
    )
    assert first == second
    assert await c.count("operation") == await c.count("basis") == 1


async def test_freeze_same_key_full_intent_matrix_conflicts(
    freeze_case: FreezeCase,
) -> None:
    from domains.costing.errors import CostFreezeError

    c = freeze_case
    original = await c.intent()
    await c.freeze(key="same", intent=original)
    for changes in [
        {"terms": (QuoteTerm(kind="payment_terms", text="Prepaid."),)},
        {"valid_until": NOW + timedelta(days=4)},
        {"replaces_quote_id": "quote_other", "expected_quote_version": 1},
        {"replaces_quote_id": "quote_other", "expected_quote_version": 2},
        {"scope_confirmation_id": "csc_other"},
        {"scope_confirmation_hash": "a" * 64},
        {"cost_sheet_id": "cs_other"},
    ]:
        with pytest.raises(CostFreezeError) as error:
            await c.freeze(key="same", intent=original.model_copy(update=changes))
        assert error.value.code == "idempotency_conflict"
    assert await c.count("operation") == 1


async def test_material_change_does_not_reuse_old_scope_at_freeze(
    freeze_case: FreezeCase,
) -> None:
    from domains.costing.errors import CostFreezeError

    c = freeze_case
    scope = await c.scope()
    await c.change_material()
    intent = await c.intent(scope=scope)
    with pytest.raises(CostFreezeError) as error:
        await c.freeze(key="new-material", intent=intent)
    assert error.value.code == "scope_stale"
    assert await c.count("operation") == 0 and await c.locked_at() is None


async def test_completed_receipt_enables_explicit_revision_without_mutation(
    freeze_case: FreezeCase,
) -> None:
    from domains.costing.errors import CostFreezeError

    c = freeze_case
    first = await c.freeze(key="first")
    with pytest.raises(CostFreezeError):
        await c.service.complete_creation(
            c.tenant_id, first.operation_id, actor=c.actor
        )
    c.completion_reader.receipt = c.receipt(first)
    completed = await c.service.complete_creation(
        c.tenant_id, first.operation_id, actor=c.actor
    )
    assert completed.state == "completed"
    assert (
        await c.service.complete_creation(
            c.tenant_id, first.operation_id, actor=c.actor
        )
        == completed
    )
    c.completion_reader.receipt = c.receipt(first, quote_id="another")
    with pytest.raises(CostFreezeError) as error:
        await c.service.complete_creation(
            c.tenant_id, first.operation_id, actor=c.actor
        )
    assert error.value.code == "revision_conflict"
    scope = await c.scope(
        key="scope-revision",
        command=c.scope_command.model_copy(
            update={"valid_until": NOW + timedelta(days=4)}
        ),
    )
    intent = await c.intent(
        scope=scope, replaces_quote_id="quote_controlled", expected_quote_version=1
    )
    second = await c.freeze(key="revision", intent=intent)
    assert (
        second.basis_id != first.basis_id
        and second.scope_confirmation.confirmation_id
        != first.scope_confirmation.confirmation_id
    )
    assert second.sheet_hash == first.sheet_hash
    assert await c.locked_at() == NOW
    assert (
        await c.service.get_frozen(c.tenant_id, first.basis_id, actor=c.actor) == first
    )


async def test_calculate_is_read_only_and_uses_confirmed_coverage(
    freeze_case: FreezeCase,
) -> None:
    c = freeze_case
    assert hasattr(c.application, "calculate"), "缺少只读完整上下文测算"
    result = await c.application.calculate(
        c.tenant_id,
        c.opportunity_id,
        c.cost_sheet_id,
        c.options,
        quote_fx_ref=None,
        actor_id=c.actor_id,
    )
    assert result.displayed_total == Money(Decimal("1000.00"), "USD")
    assert (
        await c.count("operation") == await c.count("basis") == 0
        and await c.locked_at() is None
    )


@pytest.mark.parametrize(
    "point", ["basis_after", "operation_after", "locked_at_before"]
)
async def test_freeze_write_faults_roll_back_all_effects(
    freeze_case: FreezeCase, monkeypatch, point: str
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    c = freeze_case
    intent = await c.intent()
    original = AsyncSession.execute

    async def fault(session, statement, *args, **kwargs):
        table = getattr(getattr(statement, "table", None), "name", None)
        if (
            point == "locked_at_before"
            and getattr(statement, "is_update", False)
            and table == "cost_sheets"
        ):
            raise RuntimeError("受控首次锁定前故障")
        result = await original(session, statement, *args, **kwargs)
        if getattr(statement, "is_insert", False) and table == {
            "basis_after": "costing_quote_bases",
            "operation_after": "quote_creation_operations",
        }.get(point):
            raise RuntimeError("受控持久写入后故障")
        return result

    monkeypatch.setattr(AsyncSession, "execute", fault)
    with pytest.raises(RuntimeError):
        await c.freeze(key="fault", intent=intent)
    assert await c.count("basis") == await c.count("operation") == 0
    assert await c.locked_at() is None
    await c.context.update_owner_manager()


async def test_unknown_commit_recovers_original_key_without_new_effect(
    freeze_case: FreezeCase, monkeypatch
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from domains.costing.errors import CostFreezeUnavailableError
    from infra.db.repositories.costing_freeze import CostingFreezeRepositoryImpl

    c = freeze_case
    intent = await c.intent()
    add, commit = CostingFreezeRepositoryImpl.add_frozen, AsyncSession.commit

    async def mark(repo, *args, **kwargs):
        await add(repo, *args, **kwargs)
        repo._session.info["controlled_commit_lost"] = True

    async def lost(session):
        await commit(session)
        if session.info.pop("controlled_commit_lost", False):
            raise RuntimeError("受控提交回包丢失")

    monkeypatch.setattr(CostingFreezeRepositoryImpl, "add_frozen", mark)
    monkeypatch.setattr(AsyncSession, "commit", lost)
    with pytest.raises(CostFreezeUnavailableError) as error:
        await c.freeze(key="recover", intent=intent)
    assert error.value.code == "storage_unknown"
    operation = await c.service.get_creation(c.tenant_id, "recover", actor=c.actor)
    assert operation is not None
    recovered = await c.freeze(key="recover", intent=intent)
    assert recovered.basis_id == operation.basis_id
    assert await c.count("operation") == await c.count("basis") == 1


async def test_context_lease_covers_actual_costing_commit(
    freeze_case: FreezeCase, monkeypatch
) -> None:
    from sqlalchemy.ext.asyncio import AsyncSession

    from infra.db.repositories.costing_freeze import CostingFreezeRepositoryImpl

    c = freeze_case
    intent = await c.intent()
    add, commit = CostingFreezeRepositoryImpl.add_frozen, AsyncSession.commit
    writers = []

    async def mark(repo, *args, **kwargs):
        await add(repo, *args, **kwargs)
        repo._session.info["controlled_check_lease"] = True

    async def checked(session):
        if session.info.pop("controlled_check_lease", False):
            writer = asyncio.create_task(c.context.update_owner_manager())
            writers.append(writer)
            await c.context.wait_for_blocked_writer(writer)
        await commit(session)

    monkeypatch.setattr(CostingFreezeRepositoryImpl, "add_frozen", mark)
    monkeypatch.setattr(AsyncSession, "commit", checked)
    await c.freeze(key="lease", intent=intent)
    await asyncio.wait_for(asyncio.gather(*writers), 2)
    assert len(writers) == 1


async def test_same_sheet_different_keys_concurrent_have_one_pending(
    freeze_case: FreezeCase,
) -> None:
    from domains.costing.errors import CostFreezeError

    c = freeze_case
    intent = await c.intent()
    results = await asyncio.gather(
        c.freeze(key="a", intent=intent),
        c.freeze(key="b", intent=intent),
        return_exceptions=True,
    )
    assert sum(isinstance(x, schemas.FrozenCostBasis) for x in results) == 1
    errors = [x for x in results if isinstance(x, CostFreezeError)]
    assert len(errors) == 1 and errors[0].code == "operation_pending"
    assert await c.count("operation") == 1


async def test_calculate_coverage_selection_is_exact_and_deterministic(
    freeze_case: FreezeCase,
) -> None:
    from domains.costing.quote_repository import EvidenceRecord
    from domains.costing.quote_service import view_content_hash

    c = freeze_case
    async with c.factory(c.tenant_id) as uow:
        current = await uow.coverage.get(c.tenant_id, c.scope_command.coverage_id)
    first = current.value
    baseline = await c.application.calculate(
        c.tenant_id,
        c.opportunity_id,
        c.cost_sheet_id,
        c.options,
        quote_fx_ref=None,
        actor_id=c.actor_id,
    )
    # 同时间且不同内容hash，稳定按coverage_id降序；另有不匹配sheet hash不能候补。
    candidates = [first]
    for suffix, sheet_hash in [
        ("tie", first.expected_sheet_hash),
        ("other-hash", "f" * 64),
        ("latest", first.expected_sheet_hash),
    ]:
        confirmed_at = NOW + timedelta(seconds=1) if suffix == "latest" else NOW
        changes = dict(
            first.model_dump(
                mode="python",
                include={"expected_sheet_hash", "decisions", "acquisition_mode"},
            )
        )
        changes["expected_sheet_hash"] = sheet_hash
        changes["decisions"] = tuple(
            d.model_copy(update={"reason": suffix}) for d in first.decisions
        )
        command = schemas.CostCoverageCreate(**changes)
        # 精确构造持久历史fixture，不给当前sheet确认错误hash。
        from domains.costing.quote_service import provenance_fields
        from shared.schemas.provenance import Provenance, SourceType

        payload = {
            **command.model_dump(mode="python"),
            "cost_sheet_id": c.cost_sheet_id,
        }
        fields = {
            name: Provenance(
                SourceType.EMPLOYEE_INPUT,
                c.cost_sheet_id,
                "human",
                confirmed_at,
                EmployeeId(c.actor.actor_id),
                confirmed_at,
            )
            for name in provenance_fields(payload)
        }
        value = schemas.CostCoverageView(
            **payload,
            coverage_id="placeholder",
            content_hash="0" * 64,
            confirmed_by=c.actor.actor_id,
            confirmed_at=confirmed_at,
            field_provenance=fields,
        )
        digest = view_content_hash(value)
        value = value.model_copy(update={"coverage_id": digest, "content_hash": digest})
        async with c.factory(c.tenant_id) as uow:
            await uow.coverage.add(
                EvidenceRecord(c.tenant_id, digest, suffix, "1" * 64, value)
            )
        if suffix == "tie":
            candidates.append(value)
            async with c.factory(c.tenant_id) as uow:
                tie = await uow.coverage.get_for_sheet_hash(
                    c.tenant_id, c.cost_sheet_id, first.expected_sheet_hash
                )
                assert tie.value.coverage_id == max(v.coverage_id for v in candidates)
        if suffix == "latest":
            candidates.append(value)
    async with c.factory(c.tenant_id) as uow:
        selected = await uow.coverage.get_for_sheet_hash(
            c.tenant_id, c.cost_sheet_id, first.expected_sheet_hash
        )
        assert (
            selected.value.coverage_id
            == max(
                candidates, key=lambda v: (v.confirmed_at, v.coverage_id)
            ).coverage_id
        )
        assert (
            await uow.coverage.get_for_sheet_hash(
                c.tenant_id, c.cost_sheet_id, "e" * 64
            )
            is None
        )
        assert (
            await uow.coverage.get_for_sheet_hash(
                c.tenant_id, CostSheetId("cs_other"), first.expected_sheet_hash
            )
            is None
        )
    async with c.factory(TenantId("tenant_other")) as uow:
        assert (
            await uow.coverage.get_for_sheet_hash(
                TenantId("tenant_other"), c.cost_sheet_id, first.expected_sheet_hash
            )
            is None
        )

    c.service._now = lambda: NOW + timedelta(seconds=1)
    result = await c.application.calculate(
        c.tenant_id,
        c.opportunity_id,
        c.cost_sheet_id,
        c.options,
        quote_fx_ref=None,
        actor_id=c.actor_id,
    )
    assert result.inputs_hash != baseline.inputs_hash
    assert result.displayed_total == baseline.displayed_total


async def test_completed_revision_lookup_is_exact(freeze_case: FreezeCase) -> None:
    c = freeze_case
    first = await c.freeze(key="first")
    c.completion_reader.receipt = c.receipt(first)
    completed = await c.service.complete_creation(
        c.tenant_id, first.operation_id, actor=c.actor
    )
    async with c.factory(c.tenant_id) as uow:
        for sheet, quote, version in [
            (c.cost_sheet_id, "other", 1),
            (c.cost_sheet_id, "quote_controlled", 2),
            (CostSheetId("cs_other"), "quote_controlled", 1),
        ]:
            assert (
                await uow.freezes.completed_for_revision(
                    c.tenant_id, sheet, quote, version
                )
                is None
            )
        assert (
            await uow.freezes.completed_for_revision(
                c.tenant_id, c.cost_sheet_id, "quote_controlled", 1
            )
            == completed
        )
    async with c.factory(TenantId("tenant_other")) as uow:
        assert (
            await uow.freezes.completed_for_revision(
                TenantId("tenant_other"), c.cost_sheet_id, "quote_controlled", 1
            )
            is None
        )


async def test_policy_confirmation_waits_for_frozen_selection(
    freeze_case: FreezeCase, monkeypatch
) -> None:
    from domains.costing.errors import CostFreezeError
    from infra.db.repositories.costing_quote import PricingPolicyRepositoryImpl

    c = freeze_case
    intent = await c.intent()
    selected, release = asyncio.Event(), asyncio.Event()
    original = PricingPolicyRepositoryImpl.get_effective

    async def guarded(repo, *args, **kwargs):
        result = await original(repo, *args, **kwargs)
        selected.set()
        await asyncio.wait_for(release.wait(), 2)
        return result

    monkeypatch.setattr(PricingPolicyRepositoryImpl, "get_effective", guarded)
    freezing = asyncio.create_task(c.freeze(key="policy-race", intent=intent))
    await asyncio.wait_for(selected.wait(), 2)
    command = policy(
        c.evidence.source_ref, minimum_margin_rate=Decimal("0.15"), effective_from=NOW
    )
    confirming = asyncio.create_task(
        c.t2.confirm_policy(
            c.tenant_id, command, actor=c.actor, idempotency_key="policy-new"
        )
    )
    await c.context.wait_for_blocked_writer(confirming)
    release.set()
    basis, new_policy = await asyncio.gather(freezing, confirming)
    assert basis.policy_id != new_policy.policy_id
    with pytest.raises(CostFreezeError) as error:
        await c.freeze(key="policy-race", intent=intent)
    assert error.value.code == "context_changed"


async def test_clock_is_read_only_after_policy_wait(freeze_case: FreezeCase) -> None:
    from domains.costing.errors import CostFreezeError

    c = freeze_case
    intent = await c.intent()
    times = []
    clock = [NOW]

    def now():
        times.append(clock[0])
        return clock[0]

    c.service._now = now
    async with c.factory(c.tenant_id) as holder:
        await holder.policies.lock_selection(c.tenant_id, exclusive=True)
        freezing = asyncio.create_task(c.freeze(key="expired-in-wait", intent=intent))
        await c.context.wait_for_blocked_writer(freezing)
        assert times == []
        clock[0] = NOW + timedelta(days=6)
    with pytest.raises(CostFreezeError) as error:
        await freezing
    assert error.value.code == "evidence_expired" and times == [clock[0]]
    assert await c.locked_at() is None and await c.count("operation") == 0


@pytest.mark.parametrize("freeze_first", [True, False])
async def test_actual_add_item_and_freeze_serialize(
    freeze_case: FreezeCase, monkeypatch, freeze_first: bool
) -> None:
    from domains.costing.errors import CostFreezeError, LockedCostSheetError
    from domains.costing.service_impl import CostingServiceImpl
    from infra.db.repositories.costing import CostSheetRepositoryImpl
    from infra.db.repositories.costing_freeze import CostingFreezeRepositoryImpl

    c = freeze_case
    intent = await c.intent()
    service = CostingServiceImpl(
        c.factory, authorizer=Phase1CostingAuthorizer(c.tenant_id), now=lambda: NOW
    )
    command = schemas.CostItemCreate(
        item_type="product_purchase",
        amount=Decimal("1.25"),
        currency="USD",
        price_basis="quoted",
        is_per_unit=True,
        source_ref=c.evidence.evidence_id,
        note=None,
    )

    async def adding():
        return await service.add_item(
            c.tenant_id, c.cost_sheet_id, command, actor=c.actor
        )

    acquired, release = asyncio.Event(), asyncio.Event()
    target, name = (
        (CostingFreezeRepositoryImpl, "add_frozen")
        if freeze_first
        else (CostSheetRepositoryImpl, "update")
    )
    original = getattr(target, name)

    async def guarded(repo, *args, **kwargs):
        result = await original(repo, *args, **kwargs)
        acquired.set()
        await asyncio.wait_for(release.wait(), 2)
        return result

    monkeypatch.setattr(target, name, guarded)
    first = asyncio.create_task(
        c.freeze(key="add-race", intent=intent) if freeze_first else adding()
    )
    await asyncio.wait_for(acquired.wait(), 2)
    second = asyncio.create_task(
        adding() if freeze_first else c.freeze(key="add-race", intent=intent)
    )
    try:
        await c.context.wait_for_blocked_writer(second)
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
    await first
    if freeze_first:
        with pytest.raises(LockedCostSheetError):
            await second
        assert await c.count("operation") == 1
    else:
        with pytest.raises(CostFreezeError) as error:
            await second
        assert error.value.code == "coverage_stale"
        assert await c.count("operation") == 0 and await c.locked_at() is None


async def test_cross_currency_ref_resolves_and_revision_can_use_new_quote_fx(
    freeze_case: FreezeCase,
) -> None:
    from dataclasses import replace

    from domains.costing.errors import CostFreezeError
    from shared.schemas.money import FxRate

    c = freeze_case
    async with c.factory(c.tenant_id) as uow:
        sheet = await uow.sheets.get(c.tenant_id, c.cost_sheet_id)
        await uow.sheets.update(replace(sheet, quote_currency="EUR"))
        changed = await uow.sheets.get(c.tenant_id, c.cost_sheet_id)
        coverage = (
            await uow.coverage.get(c.tenant_id, c.scope_command.coverage_id)
        ).value
    sheet_hash = costing.cost_sheet_content_hash(changed)
    cov = await c.t2.confirm_coverage(
        c.tenant_id,
        c.cost_sheet_id,
        schemas.CostCoverageCreate(
            expected_sheet_hash=sheet_hash,
            acquisition_mode=coverage.acquisition_mode,
            decisions=coverage.decisions,
        ),
        actor=c.actor,
        idempotency_key="coverage-eur",
    )
    c.scope_command = c.scope_command.model_copy(
        update={
            "coverage_id": cov,
            "expected_coverage_hash": cov,
            "expected_sheet_hash": sheet_hash,
        }
    )
    c.options = c.options.model_copy(
        update={"unit_price": Money(Decimal("2.00"), "EUR")}
    )
    fx = await c.t2.confirm_quote_fx(
        c.tenant_id,
        schemas.QuoteFxCreate(
            base_currency="USD",
            quote_currency="EUR",
            source_ref=c.evidence.source_ref,
            rate=Decimal("0.9"),
            observed_at=NOW,
        ),
        actor=c.actor,
        idempotency_key="fx-1",
    )
    intent = await c.intent(quote_fx_ref=fx.fx_id)
    with pytest.raises(CostFreezeError) as error:
        await c.freeze(
            key="missing-fx", intent=intent.model_copy(update={"quote_fx_ref": None})
        )
    assert error.value.code == "fx_missing"
    forged = c.options.model_copy(
        update={
            "quote_fx": FxRate(
                base="USD",
                quote="EUR",
                rate=Decimal("0.8"),
                observed_at=NOW,
                source=c.evidence.source_ref,
            )
        }
    )
    with pytest.raises(CostFreezeError) as error:
        await c.freeze(key="forged-fx", intent=intent, options=forged)
    assert error.value.code == "fx_missing"
    first = await c.freeze(key="fx-first", intent=intent)
    assert first.quote_fx == fx and first.pricing_options.quote_fx.rate == Decimal(
        "0.9"
    )
    c.completion_reader.receipt = c.receipt(first)
    await c.service.complete_creation(c.tenant_id, first.operation_id, actor=c.actor)
    fx2 = await c.t2.confirm_quote_fx(
        c.tenant_id,
        schemas.QuoteFxCreate(
            base_currency="USD",
            quote_currency="EUR",
            source_ref=c.evidence.source_ref,
            rate=Decimal("0.8"),
            observed_at=NOW,
        ),
        actor=c.actor,
        idempotency_key="fx-2",
    )
    scope = await c.scope(key="scope-fx-2")
    revision = await c.intent(
        scope=scope,
        quote_fx_ref=fx2.fx_id,
        replaces_quote_id="quote_controlled",
        expected_quote_version=1,
    )
    second = await c.freeze(key="fx-revision", intent=revision)
    assert (
        second.sheet_hash == first.sheet_hash
        and second.quote_fx.fx_id != first.quote_fx.fx_id
    )
    assert (
        second.calculation.effective_unit_revenue.amount
        > first.calculation.effective_unit_revenue.amount
    )
    assert (
        await c.service.get_frozen(c.tenant_id, first.basis_id, actor=c.actor)
    ).quote_fx == fx
    assert await c.locked_at() == NOW


async def test_cost_currency_fx_is_preserved_separately_in_basis(
    freeze_case: FreezeCase,
) -> None:
    from dataclasses import replace

    from domains.costing.quote_lock import frozen_basis_hash
    from shared.schemas.money import FxRate

    c = freeze_case
    command = schemas.SupplierPriceEvidenceCreate.model_validate(
        c.evidence.model_dump(
            mode="python", include=set(schemas.SupplierPriceEvidenceCreate.model_fields)
        )
    )
    evidence = await c.t2.confirm_price(
        c.tenant_id,
        command.model_copy(update={"currency": "EUR"}),
        actor=c.actor,
        idempotency_key="price-eur",
    )
    rates = (
        FxRate(
            base="EUR",
            quote="USD",
            rate=Decimal("1.1"),
            observed_at=NOW,
            source="controlled-cost-fx",
        ),
    )
    async with c.factory(c.tenant_id) as uow:
        sheet = await uow.sheets.get(c.tenant_id, c.cost_sheet_id)
        updated = replace(
            sheet,
            items=[
                replace(
                    sheet.items[0],
                    amount=Money(Decimal("1.25"), "EUR"),
                    source_ref=evidence.evidence_id,
                )
            ],
            fx_rates=rates,
        )
        await uow.sheets.update(updated)
        coverage = (
            await uow.coverage.get(c.tenant_id, c.scope_command.coverage_id)
        ).value
        persisted = await uow.sheets.get(c.tenant_id, c.cost_sheet_id)
    sheet_hash = costing.cost_sheet_content_hash(persisted)
    decisions = tuple(
        d.model_copy(
            update={
                "item_bindings": tuple(
                    b.model_copy(update={"evidence_id": evidence.evidence_id})
                    for b in d.item_bindings
                )
            }
        )
        for d in coverage.decisions
    )
    cov = await c.t2.confirm_coverage(
        c.tenant_id,
        c.cost_sheet_id,
        schemas.CostCoverageCreate(
            expected_sheet_hash=sheet_hash,
            acquisition_mode=coverage.acquisition_mode,
            decisions=decisions,
        ),
        actor=c.actor,
        idempotency_key="cost-eur-coverage",
    )
    c.scope_command = c.scope_command.model_copy(
        update={
            "coverage_id": cov,
            "expected_coverage_hash": cov,
            "expected_sheet_hash": sheet_hash,
            "evidence_bindings": (
                schemas.CostScopeEvidenceBinding(
                    evidence_id=evidence.evidence_id,
                    evidence_hash=evidence.evidence_hash,
                    applicability_note="完整规格适用，供应商以EUR计价",
                ),
            ),
        }
    )
    basis = await c.freeze(key="cost-fx")
    assert basis.cost_fx_rates == persisted.fx_rates == rates and basis.quote_fx is None
    assert basis.pricing_options.quote_fx is None
    assert (
        await c.service.get_frozen(c.tenant_id, basis.basis_id, actor=c.actor)
    ).cost_fx_rates == rates
    assert basis.basis_hash != frozen_basis_hash(
        basis.model_copy(update={"cost_fx_rates": ()})
    )
    changed = replace(rates[0], rate=Decimal("1.2"))
    assert basis.basis_hash != frozen_basis_hash(
        basis.model_copy(update={"cost_fx_rates": (changed,)})
    )


async def test_corrupt_persisted_receipt_is_rejected_not_guessed(
    freeze_case: FreezeCase,
) -> None:
    from sqlalchemy import update

    from domains.costing.errors import CostFreezeError
    from infra.db.tables import QuoteCreationOperationRow

    c = freeze_case
    basis = await c.freeze(key="corrupt-receipt")
    bad = c.receipt(basis, request_hash="0" * 64)
    async with c.context.unit.sessions.begin() as session:
        await session.execute(
            update(QuoteCreationOperationRow)
            .where(
                QuoteCreationOperationRow.tenant_id == c.tenant_id,
                QuoteCreationOperationRow.operation_id == basis.operation_id,
            )
            .values(
                state="completed",
                completed_at=NOW,
                completion=bad.model_dump(mode="json"),
            )
        )
    with pytest.raises(CostFreezeError) as error:
        async with c.factory(c.tenant_id) as uow:
            await uow.freezes.completed_for_revision(
                c.tenant_id, c.cost_sheet_id, "quote_controlled", 1
            )
    assert error.value.code == "facts_corrupt"


async def test_ambiguous_completed_receipts_are_not_arbitrarily_selected(
    freeze_case: FreezeCase,
) -> None:
    from domains.costing.errors import CostFreezeError
    from domains.costing.quote_lock import frozen_basis_hash

    c = freeze_case
    basis = await c.freeze(key="first")
    c.completion_reader.receipt = c.receipt(basis)
    await c.service.complete_creation(c.tenant_id, basis.operation_id, actor=c.actor)
    original = await c.service.get_creation(c.tenant_id, "first", actor=c.actor)
    duplicate = basis.model_copy(
        update={"basis_id": "qcb_duplicate", "operation_id": "qco_duplicate"}
    )
    duplicate = duplicate.model_copy(
        update={"basis_hash": frozen_basis_hash(duplicate)}
    )
    op = original.model_copy(
        update={
            "operation_id": duplicate.operation_id,
            "basis_id": duplicate.basis_id,
            "idempotency_key": "duplicate",
            "state": "frozen",
            "completion": None,
            "completed_at": None,
        }
    )
    # 0043不能证明quote存在或唯一；受控不一致历史必须在读取时fail closed。
    async with c.factory(c.tenant_id) as uow:
        await uow.freezes.add_frozen(c.tenant_id, duplicate, op)
        await uow.freezes.complete(
            c.tenant_id, op.operation_id, c.receipt(duplicate), NOW
        )
    with pytest.raises(CostFreezeError) as error:
        async with c.factory(c.tenant_id) as uow:
            await uow.freezes.completed_for_revision(
                c.tenant_id, c.cost_sheet_id, "quote_controlled", 1
            )
    assert error.value.code == "facts_corrupt"


async def test_basis_and_operation_are_immutable_and_completion_binding_cannot_change(
    freeze_case: FreezeCase,
) -> None:
    from sqlalchemy.exc import DBAPIError

    c = freeze_case
    basis = await c.freeze(key="immutable")
    for statement in [
        "UPDATE costing_quote_bases SET basis_hash=basis_hash WHERE tenant_id=:tenant",
        "DELETE FROM costing_quote_bases WHERE tenant_id=:tenant",
        "DELETE FROM quote_creation_operations WHERE tenant_id=:tenant",
        "UPDATE quote_creation_operations SET idempotency_key='changed' WHERE tenant_id=:tenant",
    ]:
        with pytest.raises(DBAPIError):
            async with c.context.unit.sessions.begin() as session:
                await session.execute(text(statement), {"tenant": c.tenant_id})
    c.completion_reader.receipt = c.receipt(basis)
    await c.service.complete_creation(c.tenant_id, basis.operation_id, actor=c.actor)
    with pytest.raises(DBAPIError):
        async with c.context.unit.sessions.begin() as session:
            await session.execute(
                text(
                    "UPDATE quote_creation_operations SET completed_at=completed_at WHERE tenant_id=:tenant"
                ),
                {"tenant": c.tenant_id},
            )


async def test_cancelled_freeze_releases_cost_and_context_locks(
    freeze_case: FreezeCase,
) -> None:
    c = freeze_case
    intent = await c.intent()
    async with c.factory(c.tenant_id) as holder:
        await holder.policies.lock_selection(c.tenant_id, exclusive=True)
        task = asyncio.create_task(c.freeze(key="cancel", intent=intent))
        await c.context.wait_for_blocked_writer(task)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    await c.context.update_owner_manager()
    assert await c.count("operation") == 0 and await c.locked_at() is None
    assert (await c.freeze(key="cancel", intent=intent)).basis_id


async def test_changed_actor_same_key_conflicts_and_read_requires_current_role(
    freeze_case: FreezeCase,
) -> None:
    from domains.costing.errors import CostFreezeError, CostFreezePermissionError
    from infra.db.tables import EmployeeRow

    c = freeze_case
    intent = await c.intent()
    basis = await c.freeze(key="actor", intent=intent)
    async with c.context.unit.sessions.begin() as session:
        session.add(
            EmployeeRow(
                tenant_id=c.tenant_id,
                employee_id="emp_product",
                name="产品",
                role="product",
                is_active=True,
                created_at=NOW,
            )
        )
    changed = intent.model_copy(update={"prepared_by": EmployeeId("emp_product")})
    with pytest.raises(CostFreezeError) as error:
        await c.application.freeze(
            c.tenant_id,
            changed,
            c.options,
            actor_id=EmployeeId("emp_product"),
            idempotency_key="actor",
        )
    assert error.value.code == "idempotency_conflict"
    await c.context.update_employee(c.actor_id, role="sales")
    with pytest.raises(CostFreezePermissionError):
        await c.service.get_frozen(c.tenant_id, basis.basis_id, actor=c.actor)


async def test_low_margin_is_retained_without_approval_and_same_currency_ref_is_rejected(
    freeze_case: FreezeCase,
) -> None:
    from domains.costing.errors import CostFreezeError

    c = freeze_case
    options = c.options.model_copy(update={"unit_price": Money(Decimal("1.00"), "USD")})
    intent = await c.intent(unit_price=options.unit_price)
    with pytest.raises(CostFreezeError) as error:
        await c.freeze(
            key="same-currency-fx",
            intent=intent.model_copy(update={"quote_fx_ref": "fx_unrelated"}),
            options=options,
        )
    assert error.value.code == "fx_missing"
    basis = await c.freeze(key="low-margin", intent=intent, options=options)
    assert basis.calculation.effective_unit_revenue.amount == Decimal(1)
    assert basis.calculation.metrics.full_cost_profit < 0


async def test_completion_reader_runs_without_context_or_cost_locks(
    freeze_case: FreezeCase,
) -> None:
    c = freeze_case
    basis = await c.freeze(key="external-completion")

    async def read(tenant_id, operation_id, *, actor_id):
        await c.context.update_owner_manager()
        async with c.factory(c.tenant_id) as uow:
            await uow.sheets.get_for_update(c.tenant_id, c.cost_sheet_id)
        return c.receipt(basis)

    c.completion_reader.read = read
    assert (
        await c.service.complete_creation(
            c.tenant_id, basis.operation_id, actor=c.actor
        )
    ).state == "completed"


async def test_scope_database_rejects_infinite_timestamp(
    freeze_case: FreezeCase,
) -> None:
    from sqlalchemy import insert, literal_column, select
    from sqlalchemy.exc import DBAPIError

    from infra.db.tables import CostScopeConfirmationRow

    c = freeze_case
    await c.scope()
    async with c.context.unit.sessions() as session:
        values = dict(
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
    values.update(
        confirmation_id="csc_infinite",
        idempotency_key="infinite",
        confirmed_at=literal_column("'infinity'::timestamptz"),
    )
    with pytest.raises(DBAPIError):
        async with c.context.unit.sessions.begin() as session:
            await session.execute(insert(CostScopeConfirmationRow).values(**values))


async def test_corrupt_coverage_payload_has_fixed_cost_error(
    freeze_case: FreezeCase,
) -> None:
    from sqlalchemy import insert, select

    from domains.costing.errors import CostFreezeError
    from infra.db.tables import CostingCoverageRow

    c = freeze_case
    async with c.context.unit.sessions() as session:
        values = dict(
            (
                await session.execute(
                    select(CostingCoverageRow.__table__).where(
                        CostingCoverageRow.tenant_id == c.tenant_id
                    )
                )
            )
            .mappings()
            .one()
        )
    values.update(
        coverage_id="corrupt",
        idempotency_key="corrupt",
        confirmed_at=NOW + timedelta(seconds=1),
    )
    async with c.context.unit.sessions.begin() as session:
        await session.execute(insert(CostingCoverageRow).values(**values))
    with pytest.raises(CostFreezeError) as error:
        await c.application.calculate(
            c.tenant_id,
            c.opportunity_id,
            c.cost_sheet_id,
            c.options,
            quote_fx_ref=None,
            actor_id=c.actor_id,
        )
    assert error.value.code == "facts_corrupt"


async def test_reconstructed_basis_cannot_change_intent_rounding(
    freeze_case: FreezeCase,
) -> None:
    from domains.costing.errors import CostFreezeError
    from domains.costing.quote_lock import frozen_basis_hash

    c = freeze_case
    basis = await c.freeze(key="rounding")
    c.completion_reader.receipt = c.receipt(basis)
    original = await c.service.complete_creation(
        c.tenant_id, basis.operation_id, actor=c.actor
    )
    options = basis.pricing_options.model_copy(
        update={
            "rounding": schemas.RoundingPolicy(
                unit_places=2, total_places=1, strategy="ROUND_HALF_UP"
            )
        }
    )
    bad = basis.model_copy(
        update={
            "basis_id": "qcb_bad_rounding",
            "operation_id": "qco_bad_rounding",
            "pricing_options": options,
        }
    )
    bad = bad.model_copy(update={"basis_hash": frozen_basis_hash(bad)})
    op = original.model_copy(
        update={
            "basis_id": bad.basis_id,
            "operation_id": bad.operation_id,
            "state": "frozen",
            "completion": None,
            "completed_at": None,
            "idempotency_key": "bad-rounding",
        }
    )
    with pytest.raises(CostFreezeError) as error:
        async with c.factory(c.tenant_id) as uow:
            await uow.freezes.add_frozen(c.tenant_id, bad, op)
    assert error.value.code == "facts_corrupt"


async def test_application_actor_dependency_error_is_sanitized(
    freeze_case: FreezeCase, monkeypatch
) -> None:
    from domains.costing.errors import CostFreezeUnavailableError

    c = freeze_case

    async def failed(tenant_id, actor_id):
        raise RuntimeError("受控身份读取原始错误")

    monkeypatch.setattr(c.application._actors, "read_current", failed)
    with pytest.raises(CostFreezeUnavailableError) as error:
        await c.application.calculate(
            c.tenant_id,
            c.opportunity_id,
            c.cost_sheet_id,
            c.options,
            quote_fx_ref=None,
            actor_id=c.actor_id,
        )
    assert error.value.code == "dependency_unavailable" and "原始错误" not in str(
        error.value
    )


async def test_costing_close_error_is_sanitized_and_original_key_recovers(
    freeze_case: FreezeCase, monkeypatch
) -> None:
    from sqlalchemy.exc import SQLAlchemyError
    from sqlalchemy.ext.asyncio import AsyncSession

    from domains.costing.errors import CostFreezeUnavailableError
    from infra.db.repositories.costing_freeze import CostingFreezeRepositoryImpl

    c = freeze_case
    intent = await c.intent()
    add, close = CostingFreezeRepositoryImpl.add_frozen, AsyncSession.close

    async def mark(repo, *args, **kwargs):
        await add(repo, *args, **kwargs)
        repo._session.info["controlled_close_error"] = True

    async def failed(session):
        await close(session)
        if session.info.pop("controlled_close_error", False):
            raise SQLAlchemyError("受控关闭底层错误")

    monkeypatch.setattr(CostingFreezeRepositoryImpl, "add_frozen", mark)
    monkeypatch.setattr(AsyncSession, "close", failed)
    with pytest.raises(CostFreezeUnavailableError) as error:
        await c.freeze(key="close-error", intent=intent)
    assert error.value.code == "storage_unknown"
    operation = await c.service.get_creation(c.tenant_id, "close-error", actor=c.actor)
    assert (
        await c.freeze(key="close-error", intent=intent)
    ).basis_id == operation.basis_id
