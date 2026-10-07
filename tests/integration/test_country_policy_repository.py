"""国家政策不可变 PostgreSQL 持久化、租户隔离与原子 outbox 验收。"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from domains.compliance.schemas import (
    DECISION_FIELDS,
    CountryPolicyField,
    CountryPolicyProposalCreate,
)
from shared.errors import TenantIsolationViolation
from shared.events.catalog import CountryPolicyVersionProposed
from shared.schemas.identifiers import (
    ApprovalId,
    CountryPolicyActivationId,
    CountryPolicyVersionId,
    EmployeeId,
    IdempotencyKey,
    TenantId,
)
from shared.schemas.provenance import SourceType

NOW = datetime(2026, 8, 24, 12, tzinfo=UTC)


def _load(module: str, symbol: str) -> Any:
    try:
        return getattr(importlib.import_module(module), symbol)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{symbol} 尚未创建（{exc}）")


CountryPolicyActivation = _load(
    "domains.compliance.models", "CountryPolicyActivation"
)
CountryPolicyVersion = _load("domains.compliance.models", "CountryPolicyVersion")


def _command(country: str = "Synthetic Market") -> CountryPolicyProposalCreate:
    return CountryPolicyProposalCreate.model_validate(
        {
            "country": country,
            "public_research_allowed": True,
            "contact_enrichment_allowed": False,
            "cold_b2b_email_allowed": False,
            "personal_data_basis_required": True,
            "subject_type_affects_judgment": True,
            "contact_type_affects_judgment": True,
            "opt_out_deadline_days": 30,
            "local_representative_required": False,
            "requirements": ["honor_opt_out", "retain.assessment_ref"],
            "notes": "Synthetic policy fixture reviewed by an employee.",
            "field_sources": {
                field: {
                    "source_type": SourceType.EMPLOYEE_INPUT,
                    "source_id": f"assessment:synthetic:{index}",
                }
                for index, field in enumerate(
                    sorted(DECISION_FIELDS, key=lambda item: item.value), 1
                )
            },
        }
    )


def _version(
    tenant: TenantId,
    suffix: str,
    number: int,
    *,
    country: str = "Synthetic Market",
    idempotency_key: str | None = None,
) -> CountryPolicyVersion:
    return CountryPolicyVersion.from_command(
        tenant_id=tenant,
        version_id=CountryPolicyVersionId(f"cpp_{suffix}"),
        version_number=number,
        command=_command(country),
        base_version_id=None,
        base_content_hash=None,
        proposed_by=EmployeeId(f"emp_{suffix}"),
        proposed_at=NOW,
        idempotency_key=IdempotencyKey(idempotency_key or f"policy-{suffix}"),
    )


def _activation(
    version: CountryPolicyVersion,
    suffix: str,
    sequence: int,
    *,
    activated_at: datetime = NOW + timedelta(minutes=2),
) -> CountryPolicyActivation:
    return CountryPolicyActivation(
        tenant_id=version.tenant_id,
        activation_id=CountryPolicyActivationId(f"cpa_{suffix}"),
        activation_sequence=sequence,
        country_key=version.country_key,
        country_policy_version_id=version.country_policy_version_id,
        content_hash=version.content_hash,
        approval_id=ApprovalId(f"apr_{suffix}"),
        change_set_ref=version.change_set_ref,
        approved_by=EmployeeId(f"emp_approver_{suffix}"),
        approved_at=NOW + timedelta(minutes=1),
        activated_by="system:country-policy-workflow",
        activated_at=activated_at,
    )


def _event(version: CountryPolicyVersion) -> CountryPolicyVersionProposed:
    return CountryPolicyVersionProposed(
        tenant_id=version.tenant_id,
        occurred_at=version.proposed_at,
        run_id=None,
        country_policy_version_id=version.country_policy_version_id,
        country_key=version.country_key,
        content_hash=version.content_hash,
        proposed_by=version.proposed_by,
    )


@pytest_asyncio.fixture
async def compliance_uow_factory(
    db_url: str,
) -> AsyncIterator[tuple[Callable[[TenantId], object], AsyncEngine]]:
    from infra.db.session import create_engine_from

    implementation = _load(
        "infra.db.compliance_uow", "SqlAlchemyComplianceUnitOfWork"
    )
    engine = create_engine_from(db_url)
    session_factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield lambda tenant_id: implementation(session_factory, tenant_id), engine
    finally:
        await engine.dispose()


async def _persist_version(factory: Callable[[TenantId], object], version: CountryPolicyVersion) -> None:
    async with factory(version.tenant_id) as uow:
        await uow.versions.add(version.tenant_id, version)
        await uow.provenance.add_for_version(
            version.tenant_id,
            version.country_policy_version_id,
            version.field_provenance,
        )


@pytest.mark.asyncio
async def test_policy_round_trip_keeps_field_provenance_separate(
    compliance_uow_factory,
) -> None:
    factory, engine = compliance_uow_factory
    tenant = TenantId("tenant-policy-roundtrip")
    version = _version(tenant, "roundtrip", 1)
    await _persist_version(factory, version)

    async with factory(tenant) as uow:
        found = await uow.versions.get(tenant, version.country_policy_version_id)
        provenance = await uow.provenance.list_for_version(
            tenant, version.country_policy_version_id
        )

    async with engine.connect() as connection:
        columns = await connection.run_sync(
            lambda sync: {
                str(column["name"])
                for column in inspect(sync).get_columns("country_policy_versions")
            }
        )

    exact_sources = {
        field.value: f"assessment:synthetic:{index}"
        for index, field in enumerate(
            sorted(DECISION_FIELDS, key=lambda item: item.value), 1
        )
    }
    assert found == version
    assert len(provenance) == 9
    assert {field.value: item.source_id for field, item in provenance.items()} == exact_sources
    assert "field_provenance" not in columns
    assert "provenance" not in columns


@pytest.mark.asyncio
async def test_same_country_versions_increment_inside_tenant(
    compliance_uow_factory,
) -> None:
    factory, _ = compliance_uow_factory
    tenant = TenantId("tenant-policy-sequence")
    committed: list[int] = []
    for suffix in ("sequence-one", "sequence-two"):
        async with factory(tenant) as uow:
            number = await uow.versions.next_version_number(
                tenant, "synthetic market"
            )
            version = _version(tenant, suffix, number)
            await uow.versions.add(tenant, version)
            await uow.provenance.add_for_version(
                tenant, version.country_policy_version_id, version.field_provenance
            )
            committed.append(number)

    assert committed == [1, 2]


@pytest.mark.asyncio
async def test_other_country_and_other_tenant_have_independent_sequence(
    compliance_uow_factory,
) -> None:
    factory, _ = compliance_uow_factory
    tenant_a = TenantId("tenant-policy-independent-a")
    tenant_b = TenantId("tenant-policy-independent-b")
    first = _version(tenant_a, "independent-first", 1)
    await _persist_version(factory, first)

    async with factory(tenant_a) as uow:
        other_country = await uow.versions.next_version_number(
            tenant_a, "synthetic republic"
        )
    async with factory(tenant_b) as uow:
        other_tenant = await uow.versions.next_version_number(
            tenant_b, "synthetic market"
        )

    assert other_country == 1
    assert other_tenant == 1


@pytest.mark.asyncio
async def test_same_idempotency_key_different_tenant_is_allowed(
    compliance_uow_factory,
) -> None:
    factory, _ = compliance_uow_factory
    tenant_a = TenantId("tenant-policy-idempotency-a")
    tenant_b = TenantId("tenant-policy-idempotency-b")
    shared_key = "country-policy-shared-idempotency"
    version_a = _version(
        tenant_a, "idempotency-a", 1, idempotency_key=shared_key
    )
    version_b = _version(
        tenant_b, "idempotency-b", 1, idempotency_key=shared_key
    )
    await _persist_version(factory, version_a)
    await _persist_version(factory, version_b)

    async with factory(tenant_a) as uow:
        found_a = await uow.versions.find_by_idempotency_key(
            tenant_a, IdempotencyKey(shared_key)
        )
    async with factory(tenant_b) as uow:
        found_b = await uow.versions.find_by_idempotency_key(
            tenant_b, IdempotencyKey(shared_key)
        )

    assert found_a is not None and found_a.tenant_id == tenant_a
    assert found_b is not None and found_b.tenant_id == tenant_b


@pytest.mark.asyncio
async def test_repository_rejects_cross_tenant_reads(
    compliance_uow_factory,
) -> None:
    factory, engine = compliance_uow_factory
    tenant_a = TenantId("tenant-policy-isolation-a")
    tenant_b = TenantId("tenant-policy-isolation-b")
    version = _version(tenant_a, "isolation", 1)
    await _persist_version(factory, version)
    executed: list[str] = []

    def _record_statement(*_args: object) -> None:
        executed.append("executed")

    event.listen(engine.sync_engine, "before_cursor_execute", _record_statement)
    try:
        async with factory(tenant_a) as uow:
            before = len(executed)
            with pytest.raises(TenantIsolationViolation):
                await uow.versions.get(tenant_b, version.country_policy_version_id)
            assert len(executed) == before
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record_statement)


@pytest.mark.asyncio
async def test_activation_current_is_country_scoped(
    compliance_uow_factory,
) -> None:
    factory, _ = compliance_uow_factory
    tenant = TenantId("tenant-policy-current-country")
    country_a = _version(tenant, "current-country-a", 1)
    country_b = _version(
        tenant, "current-country-b", 1, country="Synthetic Republic"
    )
    await _persist_version(factory, country_a)
    await _persist_version(factory, country_b)
    activation_a = _activation(country_a, "current-country-a", 1)
    activation_b = _activation(
        country_b,
        "current-country-b",
        1,
        activated_at=NOW + timedelta(minutes=20),
    )
    async with factory(tenant) as uow:
        await uow.activations.add(tenant, activation_a)
        await uow.activations.add(tenant, activation_b)

    async with factory(tenant) as uow:
        current = await uow.activations.get_current(
            tenant, country_a.country_key
        )

    assert current == activation_a


@pytest.mark.parametrize("mismatch", ["country_key", "content_hash"])
@pytest.mark.asyncio
async def test_activation_rejects_country_or_hash_mismatch_with_version(
    compliance_uow_factory,
    mismatch: str,
) -> None:
    factory, _ = compliance_uow_factory
    case = {"country_key": "country", "content_hash": "hash"}[mismatch]
    tenant = TenantId(f"tenant-activation-integrity-{case}")
    version = _version(tenant, f"activation-integrity-{case}", 1)
    await _persist_version(factory, version)
    content_hash = "b" * 64 if mismatch == "content_hash" else version.content_hash
    activation = CountryPolicyActivation(
        tenant_id=tenant,
        activation_id=CountryPolicyActivationId(
            f"cpa_activation-integrity-{case}"
        ),
        activation_sequence=1,
        country_key=(
            "synthetic republic"
            if mismatch == "country_key"
            else version.country_key
        ),
        country_policy_version_id=version.country_policy_version_id,
        content_hash=content_hash,
        approval_id=ApprovalId(f"apr_activation-integrity-{case}"),
        change_set_ref=(
            f"country_policy:{version.country_policy_version_id}:{content_hash}"
        ),
        approved_by=EmployeeId("emp_activation-integrity-approver"),
        approved_at=NOW + timedelta(minutes=1),
        activated_by="system:country-policy-workflow",
        activated_at=NOW + timedelta(minutes=2),
    )

    with pytest.raises(IntegrityError):
        async with factory(tenant) as uow:
            await uow.activations.add(tenant, activation)


@pytest.mark.parametrize(
    ("table", "identity_column", "fact_kind", "operation"),
    [
        ("country_policy_versions", "country_policy_version_id", "version", "update"),
        ("country_policy_versions", "country_policy_version_id", "version", "delete"),
        (
            "country_policy_field_provenance",
            "field_name",
            "provenance",
            "update",
        ),
        (
            "country_policy_field_provenance",
            "field_name",
            "provenance",
            "delete",
        ),
        (
            "country_policy_activations",
            "country_policy_activation_id",
            "activation",
            "update",
        ),
        (
            "country_policy_activations",
            "country_policy_activation_id",
            "activation",
            "delete",
        ),
    ],
)
@pytest.mark.asyncio
async def test_policy_facts_reject_update_and_delete(
    compliance_uow_factory,
    table: str,
    identity_column: str,
    fact_kind: str,
    operation: str,
) -> None:
    factory, engine = compliance_uow_factory
    suffix = f"immutable-{fact_kind}-{operation}"
    tenant = TenantId(f"tenant-immutable-{fact_kind[0]}-{operation[0]}")
    version = _version(tenant, suffix, 1)
    activation = _activation(version, suffix, 1)
    async with factory(tenant) as uow:
        await uow.versions.add(tenant, version)
        await uow.provenance.add_for_version(
            tenant, version.country_policy_version_id, version.field_provenance
        )
        await uow.activations.add(tenant, activation)

    identity = {
        "version": str(version.country_policy_version_id),
        "provenance": CountryPolicyField.PUBLIC_RESEARCH_ALLOWED.value,
        "activation": str(activation.activation_id),
    }[fact_kind]
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
                text(statement),
                {"tenant": str(tenant), "identity": identity},
            )

    assert getattr(captured.value.orig, "sqlstate", None) == "23514"


@pytest.mark.asyncio
async def test_version_and_proposal_event_commit_or_rollback_together(
    compliance_uow_factory,
) -> None:
    factory, engine = compliance_uow_factory
    success_tenant = TenantId("tenant-policy-atomic-success")
    success = _version(success_tenant, "atomic-success", 1)
    async with factory(success_tenant) as uow:
        await uow.versions.add(success_tenant, success)
        await uow.provenance.add_for_version(
            success_tenant,
            success.country_policy_version_id,
            success.field_provenance,
        )
        await uow.bus.publish(_event(success))

    async with engine.connect() as connection:
        success_counts = (
            await connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM country_policy_versions WHERE tenant_id=:tenant), "
                    "(SELECT count(*) FROM outbox_events WHERE tenant_id=:tenant "
                    "AND event_type='CountryPolicyVersionProposed')"
                ),
                {"tenant": str(success_tenant)},
            )
        ).one()
        payload = await connection.scalar(
            text(
                "SELECT event_payload FROM outbox_events WHERE tenant_id=:tenant "
                "AND event_type='CountryPolicyVersionProposed'"
            ),
            {"tenant": str(success_tenant)},
        )
    assert tuple(success_counts) == (1, 1)
    assert payload["country_policy_version_id"] == str(
        success.country_policy_version_id
    )
    assert payload["country_key"] == "synthetic market"
    assert payload["content_hash"] == success.content_hash

    failure_tenant = TenantId("tenant-policy-atomic-failure")
    failed = _version(failure_tenant, "atomic-failure", 1)
    with pytest.raises(RuntimeError, match="injected publish failure"):
        async with factory(failure_tenant) as uow:
            await uow.versions.add(failure_tenant, failed)
            await uow.provenance.add_for_version(
                failure_tenant,
                failed.country_policy_version_id,
                failed.field_provenance,
            )

            real_publish = uow.bus.publish

            async def _fail_publish(
                proposal_event: CountryPolicyVersionProposed,
            ) -> None:
                await real_publish(proposal_event)
                raise RuntimeError("injected publish failure")

            uow.bus.publish = _fail_publish
            await uow.bus.publish(_event(failed))

    async with engine.connect() as connection:
        failure_counts = (
            await connection.execute(
                text(
                    "SELECT "
                    "(SELECT count(*) FROM country_policy_versions WHERE tenant_id=:tenant), "
                    "(SELECT count(*) FROM outbox_events WHERE tenant_id=:tenant "
                    "AND event_type='CountryPolicyVersionProposed')"
                ),
                {"tenant": str(failure_tenant)},
            )
        ).one()
    assert tuple(failure_counts) == (0, 0)
