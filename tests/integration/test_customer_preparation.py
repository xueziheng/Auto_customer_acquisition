"""真实PG资料准备；只保存事实/推断/未验证邮箱，不触达外部系统。"""

import hashlib
from dataclasses import replace

import pytest
from sqlalchemy import select

from apps.api.composition.customer_preparation import (
    CustomerPreparationApplication,
    PostgresCustomerPreparationTransactions,
)
from apps.api.dependencies import get_request_identity
from domains.prospecting.customer_preparation import CustomerPreparationCommand
from domains.prospecting.schemas import (
    ContactType,
    LegalBasisInput,
    LegalBasisType,
    SubjectType,
)
from infra.db.customer_followup import PostgresCustomerContextReader
from infra.db.tables import (
    ContactPointRow,
    DemandSignalRow,
    NeedHypothesisRow,
    ProspectAccountRow,
    RawArtifactRow,
)
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import new_id
from tests.integration.test_customer_assignment_bridge import NOW
from tests.integration.test_customer_assignment_bridge import (
    bridge_inventory as bridge_inventory,  # noqa: PLC0414 -- 真实PG共享夹具
)
from tests.integration.test_customer_followup_api import app_fixture


class TestHasher:
    def fingerprint(self, value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()


async def setup(bridge_inventory):
    app, allocation, _, _ = await app_fixture(bridge_inventory)
    identity = app.dependency_overrides[get_request_identity]()
    _, _, factory, tenant, _, _, _ = bridge_inventory
    artifact_id = new_id("art")
    async with factory() as session, session.begin():
        session.add(
            RawArtifactRow(
                tenant_id=tenant,
                artifact_id=artifact_id,
                kind="web_snapshot",
                mime_type="text/html",
                size_bytes=10,
                content_hash="b" * 64,
                object_key=f"raw/{tenant}/{artifact_id}",
                uploaded_at=NOW,
            )
        )
    application = CustomerPreparationApplication(
        PostgresCustomerContextReader(factory, tenant),
        PostgresCustomerPreparationTransactions(factory, TestHasher(), now=lambda: NOW),
        now=lambda: NOW,
    )
    command = CustomerPreparationCommand(
        entity_type="distributor",
        industry="Motor vehicles",
        source_url="https://company.test/tricycles",
        page_hash="b" * 64,
        snapshot_artifact_ref=artifact_id,
        observed_at=NOW,
        public_observation="Company distributes tricycles.",
        product_category="electric_tricycle",
        fit_reasoning="员工推断：已有三轮车销售渠道，可能适配电动三轮车，需沟通验证。",
    )
    return application, identity, allocation, command


async def rows(factory, tenant, model):
    async with factory() as session:
        return (
            await session.scalars(select(model).where(model.tenant_id == tenant))
        ).all()


async def test_public_profile_and_inference_are_atomic_idempotent(bridge_inventory):
    service, identity, allocation, command = await setup(bridge_inventory)
    factory, tenant = bridge_inventory[2:4]
    result = await service.prepare(identity, allocation.id, command)
    again = await service.prepare(identity, allocation.id, command)
    assert result.account.entity_type == "distributor"
    assert again.hypotheses == result.hypotheses
    assert (
        result.hypotheses[0].is_inference and result.hypotheses[0].status == "inferred"
    )
    assert len(await rows(factory, tenant, DemandSignalRow)) == 1
    assert len(await rows(factory, tenant, NeedHypothesisRow)) == 1
    assert not await rows(factory, tenant, ContactPointRow)
    provenance = result.account.field_provenance["entity_type"]
    assert provenance.source_url == command.source_url
    assert provenance.confirmed_by == identity.employee.employee_id
    assert provenance.confirmed_at == NOW
    assert await service.read(identity, allocation.id) == again


async def test_contact_stays_unverified_and_replays_do_not_duplicate(bridge_inventory):
    service, identity, allocation, command = await setup(bridge_inventory)
    command = command.model_copy(
        update={
            "public_email": "info@company.test",
            "legal_basis_confirmed": True,
            "legal_basis": LegalBasisInput(
                LegalBasisType.LEGITIMATE_INTEREST,
                SubjectType.LEGAL_ENTITY,
                ContactType.ROLE_BASED,
                "employee_review",
                NOW,
                source_url=command.source_url,
                assessment_ref="assessment-fixture-reviewed-001",
            ),
        }
    )
    result = await service.prepare(identity, allocation.id, command)
    await service.prepare(identity, allocation.id, command)
    points = await rows(bridge_inventory[2], bridge_inventory[3], ContactPointRow)
    assert len(points) == 1 and points[0].verification_status == "unverified"
    assert result.contacts[0].contact_points[0].contact_point.verified_at is None


async def test_missing_legal_evidence_rejects_without_profile_or_signal_changes(
    bridge_inventory,
):
    service, identity, allocation, command = await setup(bridge_inventory)
    command = command.model_copy(update={"public_email": "info@company.test"})
    with pytest.raises(ValidationError):
        await service.prepare(identity, allocation.id, command)
    factory, tenant = bridge_inventory[2:4]
    assert (await rows(factory, tenant, ProspectAccountRow))[0].entity_type is None
    assert not await rows(factory, tenant, DemandSignalRow)


async def test_bad_snapshot_rolls_back_profile_and_missing_fields_conflict(
    bridge_inventory,
):
    service, identity, allocation, command = await setup(bridge_inventory)
    with pytest.raises(ValidationError):
        await service.prepare(
            identity, allocation.id, command.model_copy(update={"page_hash": "a" * 64})
        )
    factory, tenant = bridge_inventory[2:4]
    assert (await rows(factory, tenant, ProspectAccountRow))[0].entity_type is None
    await service.prepare(identity, allocation.id, command)
    with pytest.raises(ValidationError):
        await service.prepare(
            identity,
            allocation.id,
            command.model_copy(update={"entity_type": "manufacturer"}),
        )
    assert (await rows(factory, tenant, ProspectAccountRow))[
        0
    ].entity_type == "distributor"


async def test_other_owner_and_cross_tenant_reject_before_changes(bridge_inventory):
    service, identity, allocation, command = await setup(bridge_inventory)
    from shared.schemas.identifiers import TenantId

    for denied in (
        replace(
            identity,
            employee=replace(identity.employee, employee_id=bridge_inventory[6]),
        ),
        replace(identity, tenant_id=TenantId(new_id("tn"))),
    ):
        with pytest.raises(PermissionDenied):
            await service.prepare(denied, allocation.id, command)
    assert not await rows(bridge_inventory[2], bridge_inventory[3], DemandSignalRow)


async def test_late_contact_validation_failure_rolls_back_all_new_records(
    bridge_inventory,
):
    service, identity, allocation, command = await setup(bridge_inventory)
    command = command.model_copy(
        update={
            "public_email": "not-an-email",
            "legal_basis_confirmed": True,
            "legal_basis": LegalBasisInput(
                LegalBasisType.LEGITIMATE_INTEREST,
                SubjectType.LEGAL_ENTITY,
                ContactType.ROLE_BASED,
                "employee_review",
                NOW,
                source_url=command.source_url,
                assessment_ref="assessment-fixture-reviewed-001",
            ),
        }
    )
    with pytest.raises(ValidationError):
        await service.prepare(identity, allocation.id, command)
    factory, tenant = bridge_inventory[2:4]
    assert (await rows(factory, tenant, ProspectAccountRow))[0].entity_type is None
    assert not await rows(factory, tenant, DemandSignalRow)
    assert not await rows(factory, tenant, NeedHypothesisRow)


async def test_concurrent_replay_has_single_signal_hypothesis_and_contact(
    bridge_inventory,
):
    import asyncio

    service, identity, allocation, command = await setup(bridge_inventory)
    values = await asyncio.gather(
        *(service.prepare(identity, allocation.id, command) for _ in range(2))
    )
    assert values[0].hypotheses == values[1].hypotheses
    factory, tenant = bridge_inventory[2:4]
    assert len(await rows(factory, tenant, DemandSignalRow)) == 1
    assert len(await rows(factory, tenant, NeedHypothesisRow)) == 1


async def test_http_preparation_rejects_spoofed_identity_and_returns_provenance(
    bridge_inventory,
):
    from types import SimpleNamespace

    from httpx import ASGITransport, AsyncClient

    from apps.api.dependencies import get_api_dependencies
    from apps.api.routers.customer_preparation import router

    service, identity, allocation, command = await setup(bridge_inventory)
    app, _, _, _ = await app_fixture(bridge_inventory)
    app.include_router(router, prefix="/prospects")
    app.dependency_overrides[get_request_identity] = lambda: identity
    app.dependency_overrides[get_api_dependencies] = lambda: SimpleNamespace(
        customer_preparation=service
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Tenant-Id": identity.tenant_id},
    ) as client:
        path = f"/prospects/customers/{allocation.id}/preparation"
        body = command.model_dump(mode="json")
        response = await client.post(path, json={**body, "employee_id": "intruder"})
        assert response.status_code == 400
        response = await client.post(path, json=body)
        assert response.status_code == 200
        assert (
            response.json()["account"]["field_provenance"]["entity_type"][
                "confirmed_by"
            ]
            == identity.employee.employee_id
        )
        assert (await client.get(path)).json()["can_prepare"] is True


async def test_candidate_artifact_from_another_tenant_cannot_prepare_customer(
    bridge_inventory,
):
    service, identity, allocation, command = await setup(bridge_inventory)
    factory, tenant = bridge_inventory[2:4]
    foreign_tenant, foreign_artifact = new_id("tn"), new_id("art")
    async with factory() as session, session.begin():
        session.add(
            RawArtifactRow(
                tenant_id=foreign_tenant,
                artifact_id=foreign_artifact,
                kind="web_snapshot",
                mime_type="text/html",
                size_bytes=10,
                content_hash="b" * 64,
                object_key=f"raw/{foreign_tenant}/{foreign_artifact}",
                uploaded_at=NOW,
            )
        )
    with pytest.raises(ValidationError):
        await service.prepare(
            identity,
            allocation.id,
            command.model_copy(
                update={
                    "snapshot_artifact_ref": foreign_artifact,
                }
            ),
        )
    assert (await rows(factory, tenant, ProspectAccountRow))[0].entity_type is None
    assert not await rows(factory, tenant, DemandSignalRow)
    assert not await rows(factory, tenant, NeedHypothesisRow)
