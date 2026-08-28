"""实际Linux parser→真实PG/Gateway→两业务服务，仅受控PDF/客户消息。"""

import sys
from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import TypeAdapter
from sqlalchemy import select, update

from artifact_store.store import RawArtifactKind
from connectors.evidence_text.client import LinuxEvidenceTextParser
from domains.costing.permissions import CostingActor, CostingScope
from domains.costing.quote_service import CostingQuoteServiceImpl
from domains.costing.schemas import SupplierPriceEvidenceCreate
from domains.demand.schemas import NeedUnitConfirmationCommand
from domains.demand.service import (
    NeedUnitError,
    NeedUnitPermissionError,
    quantity_fact_hash,
    require_current_unit,
)
from domains.demand.unit_service import NeedUnitServiceImpl
from infra.db.costing_uow import SqlAlchemyCostingUnitOfWork
from infra.db.need_unit_uow import SqlAlchemyNeedUnitUnitOfWork
from infra.db.tables import EmployeeRow, OpportunityRow, ToolCallRow, ValidatedNeedRow
from shared.schemas import evidence_read as e
from shared.schemas.provenance import FactualField
from tests.integration.test_quote_evidence_gateway import NOW, build_case
from tests.unit.test_evidence_parser_client import probe_limits
from tests.unit.test_evidence_text_profiles import parse_limits, pdf_bytes
from workflows.employee_work_intake.schemas import WorkSourceKind
from workflows.quote_approval.source_readers import (
    GatewayNeedUnitEvidenceReader,
    GatewayPricingEvidenceReader,
)


async def locate(case, source, scope, profile, page, start, end):
    preview = await case.reader.read(
        case.tenant,
        e.EvidencePreviewRequest(
            operation="preview",
            source_ref=source,
            scope=scope,
            profile=profile,
            page=page,
        ),
        actor_id=case.actor,
    )
    return await case.reader.read(
        case.tenant,
        e.EvidenceLocateRequest(
            operation="locate",
            source_ref=source,
            scope=scope,
            profile=profile,
            page=page,
            start=start,
            end=end,
            expected_raw_hash=preview.reference.raw.content_hash,
            expected_text_hash=preview.text_hash,
        ),
        actor_id=case.actor,
    )


async def test_actual_parser_prices_and_unit_receipt_history(integration_engine):
    assert sys.platform == "linux", "not_run：同链必须在固定Linux runner执行"
    parser = LinuxEvidenceTextParser(limits=parse_limits(), probe_limits=probe_limits())
    assert (await parser.probe()).status == "available"
    case = await build_case(integration_engine, parser=parser)

    async def no_business_locks():
        assert case.need_access.depth == 0
        async with case.factory() as session, session.begin():
            assert (
                "executing"
                in (
                    await session.scalars(
                        select(ToolCallRow.status).where(
                            ToolCallRow.tenant_id == case.tenant
                        )
                    )
                ).all()
            )
            for table, key, identity in [
                (EmployeeRow, "employee_id", case.actor),
                (ValidatedNeedRow, "need_id", case.need),
                (OpportunityRow, "opportunity_id", case.opportunity),
            ]:
                await session.execute(
                    select(getattr(table, key))
                    .where(
                        table.tenant_id == case.tenant, getattr(table, key) == identity
                    )
                    .with_for_update(nowait=True)
                )

    case.transport.before = no_business_locks
    statement = "Quoted unit price: USD 2.00 for 50 pieces."
    raw = await case.store.put(
        case.tenant, RawArtifactKind.PDF, pdf_bytes(statement), "application/pdf"
    )
    upload = await case.uploads.register_upload(
        case.tenant,
        raw.artifact_id,
        case.actor,
        WorkSourceKind.PDF_TEXT,
        occurred_at=NOW,
        customer_timezone="UTC",
    )
    located = await locate(
        case,
        "upload:" + upload.upload_id,
        e.PricingEvidenceScope(purpose="pricing"),
        "pdf-text-v1",
        1,
        0,
        len(statement),
    )
    assert located.excerpt == statement

    class CurrentActor:
        async def read_current(self, tenant, actor):
            fact = await case.contexts.read_actor(tenant, actor)
            if fact is None or not fact.is_active:
                return None
            return CostingActor(fact.employee_id, fact.role, CostingScope.TENANT)

    costing = CostingQuoteServiceImpl(
        lambda tn: SqlAlchemyCostingUnitOfWork(case.factory, tn, now=lambda: NOW),
        GatewayPricingEvidenceReader(case.reader),
        actor_reader=CurrentActor(),
        now=lambda: NOW,
    )
    price = await costing.confirm_price(
        case.tenant,
        SupplierPriceEvidenceCreate(
            kind="supplier_price",
            opportunity_id=case.opportunity,
            currency="USD",
            source_ref="upload:" + upload.upload_id,
            locator=located.locator,
            amount=Decimal("2.00"),
            need_id=case.need,
            supplier_ref="controlled:supplier",
            specification="controlled hardware",
            unit="pieces",
            destination="DE",
            basis="quoted",
            quantity_min=50,
            quantity_max=50,
            moq=1,
            quoted_at=NOW,
            valid_until=NOW + timedelta(days=1),
        ),
        actor=CostingActor(case.actor, "boss", CostingScope.TENANT),
        idempotency_key="controlled-price",
    )
    assert (
        price.source.artifact_id == raw.artifact_id
        and price.source.content_hash == raw.content_hash
    )
    assert price.amount == Decimal("2.00") and price.confirmed_by == case.actor
    assert price.field_provenance["amount"].is_human_confirmed

    located = await locate(
        case,
        "message:" + case.message,
        e.NeedUnitEvidenceScope(
            purpose="need_unit", need_id=case.need, action="confirm"
        ),
        "rfc822-plain-v1",
        None,
        8,
        17,
    )
    assert located.excerpt == "50 pieces"
    reader = GatewayNeedUnitEvidenceReader(
        case.reader, case.access, case.contexts, case.need_access
    )
    service = NeedUnitServiceImpl(
        lambda tn: SqlAlchemyNeedUnitUnitOfWork(
            case.factory, tn, lock_timeout_ms=1000, statement_timeout_ms=1000
        ),
        case.need_access,
        reader,
        now=lambda: NOW,
    )
    command = NeedUnitConfirmationCommand(
        unit="pieces",
        source_message_id=case.message,
        locator=located.locator,
        source_quote="50 pieces",
        expected_quantity_fact_hash=quantity_fact_hash(
            case.tenant, case.need, case.quantity
        ),
        expected_unit_confirmation_id=None,
    )
    receipt = await service.confirm(
        case.tenant,
        case.need,
        command,
        actor_id=case.actor,
        idempotency_key="controlled-unit",
    )
    assert receipt.source.artifact_id == case.email.artifact_id
    assert (
        require_current_unit(
            await service.get_facts(case.tenant, case.need, actor_id=case.actor)
        ).value
        == "pieces"
    )
    reads = case.transport.reads
    assert (
        await service.confirm(
            case.tenant,
            case.need,
            command,
            actor_id=case.actor,
            idempotency_key="controlled-unit",
        )
        == receipt
    )
    changed = FactualField(51, case.quantity.provenance)
    async with case.factory() as session, session.begin():
        await session.execute(
            update(ValidatedNeedRow)
            .where(
                ValidatedNeedRow.tenant_id == case.tenant,
                ValidatedNeedRow.need_id == case.need,
            )
            .values(
                quantity=TypeAdapter(FactualField[int]).dump_python(
                    changed, mode="json"
                )
            )
        )
    assert (
        await service.get_confirmation(
            case.tenant, case.need, receipt.confirmation_id, actor_id=case.actor
        )
        == receipt
    )
    assert (
        await service.confirm(
            case.tenant,
            case.need,
            command,
            actor_id=case.actor,
            idempotency_key="controlled-unit",
        )
        == receipt
    )
    with pytest.raises(NeedUnitError) as caught:
        require_current_unit(
            await service.get_facts(case.tenant, case.need, actor_id=case.actor)
        )
    assert caught.value.code == "unit_stale"
    assert case.transport.reads == reads
    async with case.factory() as session, session.begin():
        await session.execute(
            update(EmployeeRow)
            .where(
                EmployeeRow.tenant_id == case.tenant,
                EmployeeRow.employee_id == case.actor,
            )
            .values(is_active=False)
        )
    with pytest.raises(NeedUnitPermissionError):
        await service.get_confirmation(
            case.tenant, case.need, receipt.confirmation_id, actor_id=case.actor
        )
    with pytest.raises(NeedUnitPermissionError):
        await service.confirm(
            case.tenant,
            case.need,
            command,
            actor_id=case.actor,
            idempotency_key="controlled-unit",
        )
    assert case.transport.reads == reads
