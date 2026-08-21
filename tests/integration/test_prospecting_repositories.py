"""prospecting PostgreSQL repositories/UoW 集成契约。"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from domains.prospecting.schemas import (
    ContactPointCreateRequest,
    ContactPointKind,
    VerificationRecordRequest,
)
from infra.db.tables import OutboxEventRow
from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import (
    ContactPointId,
    ProspectAccountId,
    ProspectContactId,
    TenantId,
    new_id,
)

_models = importlib.import_module("domains.prospecting.models")
NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


@pytest_asyncio.fixture
async def prospect_engine(db_url: str) -> AsyncIterator[AsyncEngine]:
    from infra.db.session import create_engine_from

    engine = create_engine_from(db_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def prospect_session(
    prospect_engine: AsyncEngine,
) -> AsyncIterator[AsyncSession]:
    session = AsyncSession(bind=prospect_engine, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()


def _account(tenant: TenantId, suffix: str = "1"):
    return _models.ProspectAccount(
        account_id=ProspectAccountId(f"acc-{suffix}"),
        tenant_id=tenant,
        name="Acme Manufacturing",
        country="DE",
        website_domain=f"acme-{suffix}.example",
        created_at=NOW,
        source_signal_refs=[f"sig-{suffix}"],
    )


def _contact(tenant: TenantId, account_id: ProspectAccountId, suffix: str = "1"):
    return _models.ProspectContact(
        contact_id=ProspectContactId(f"pc-{suffix}"),
        tenant_id=tenant,
        account_id=account_id,
        created_at=NOW,
        full_name="Alex Buyer",
        role_title="Procurement Manager",
        language="en",
    )


def _point(tenant: TenantId, contact_id: ProspectContactId, suffix: str = "1"):
    return _models.ContactPoint(
        contact_point_id=ContactPointId(f"cp-{suffix}"),
        tenant_id=tenant,
        contact_id=contact_id,
        kind=_models.ContactPointKind.EMAIL,
        value=f"buyer-{suffix}@example.com",
        value_hash=(suffix[-1] if suffix[-1] in "abcdef" else "a") * 64,
        legal_basis=_models.LegalBasisRecord(
            basis=_models.LegalBasisType.LEGITIMATE_INTEREST,
            subject_type=_models.SubjectType.LEGAL_ENTITY,
            contact_type=_models.ContactType.PERSONAL_BUSINESS,
            source="company_website",
            collected_at=NOW,
            source_url="https://example.com/contact",
            assessment_ref="lia-1",
        ),
        created_at=NOW,
        enrichment_cost_note="provider tier one",
    )


async def test_account_repository_roundtrip_dedup_merge_and_tenant_guard(
    prospect_session: AsyncSession,
) -> None:
    from infra.db.repositories.prospecting import ProspectAccountRepositoryImpl

    tenant = TenantId("tenant-prospect-account")
    repo = ProspectAccountRepositoryImpl(prospect_session, tenant)
    account = _account(tenant)
    assert await repo.add(account) is True
    assert await repo.add(_account(tenant, "duplicate")) is True
    domain_duplicate = _account(tenant, "winner")
    domain_duplicate.website_domain = account.website_domain
    assert await repo.add(domain_duplicate) is False
    assert await repo.get(tenant, account.account_id) == account
    assert await repo.find_by_domain(tenant, account.website_domain or "") == account
    candidates = await repo.search_by_name(tenant, "Acme", "DE")
    assert [item.account_id for item in candidates] == [account.account_id, ProspectAccountId("acc-duplicate")]
    merged = await repo.merge_source_signal_refs(
        tenant, account.account_id, ("sig-1", "sig-2", "sig-1")
    )
    assert merged is not None
    assert merged.source_signal_refs == ["sig-1", "sig-2"]
    other_tenant = TenantId("tenant-prospect-account-other")
    other_repo = ProspectAccountRepositoryImpl(prospect_session, other_tenant)
    other_account = _account(other_tenant)
    other_account.website_domain = account.website_domain
    assert await other_repo.add(other_account) is True
    assert await other_repo.find_by_domain(
        other_tenant, account.website_domain or ""
    ) == other_account
    with pytest.raises(TenantIsolationViolation, match="跨租户数据隔离违规"):
        await repo.get(TenantId("tenant-other"), account.account_id)


async def test_contact_repository_roundtrip_and_verified_filter(
    prospect_session: AsyncSession,
) -> None:
    from infra.db.repositories.prospecting import (
        ProspectAccountRepositoryImpl,
        ProspectContactRepositoryImpl,
    )

    tenant = TenantId("tenant-prospect-contact")
    accounts = ProspectAccountRepositoryImpl(prospect_session, tenant)
    contacts = ProspectContactRepositoryImpl(prospect_session, tenant, now=lambda: NOW)
    account = _account(tenant, "contact")
    contact = _contact(tenant, account.account_id, "contact")
    point = _point(tenant, contact.contact_id, "contact")
    assert await accounts.add(account) is True
    await contacts.add_contact(contact)
    assert await contacts.add_contact_point(point) is True
    assert await contacts.get_contact(tenant, contact.contact_id) == contact
    assert await contacts.get_contact_point(tenant, point.contact_point_id) == point
    assert await contacts.find_by_value_hash(tenant, point.kind, point.value_hash) == point
    assert await contacts.list_verified_for_account(tenant, account.account_id) == []
    assert await contacts.add_contact_point(point) is False


async def test_erasure_repository_is_append_only_and_removes_orphan_contact(
    prospect_session: AsyncSession,
) -> None:
    from infra.db.repositories.prospecting import (
        ProspectAccountRepositoryImpl,
        ProspectContactRepositoryImpl,
    )

    tenant = TenantId("tenant-prospect-erasure")
    accounts = ProspectAccountRepositoryImpl(prospect_session, tenant)
    contacts = ProspectContactRepositoryImpl(prospect_session, tenant, now=lambda: NOW)
    account = _account(tenant, "erase")
    contact = _contact(tenant, account.account_id, "erase")
    point = _point(tenant, contact.contact_id, "erase")
    assert await accounts.add(account) is True
    await contacts.add_contact(contact)
    assert await contacts.add_contact_point(point) is True
    assert await contacts.erase_personal_data(tenant, point.value_hash) == 1
    assert await contacts.is_erasure_suppressed(tenant, point.value_hash) is True
    assert await contacts.get_contact_point(tenant, point.contact_point_id) is None
    assert await contacts.get_contact(tenant, contact.contact_id) is None
    assert await accounts.get(tenant, account.account_id) is not None


async def test_prospecting_uow_commits_and_rolls_back(prospect_engine: AsyncEngine) -> None:
    from infra.db.prospecting_uow import SqlAlchemyProspectingUnitOfWork
    from infra.db.repositories.prospecting import ProspectAccountRepositoryImpl

    factory = async_sessionmaker(prospect_engine, expire_on_commit=False)
    tenant = TenantId("tenant-prospect-uow")
    committed = _account(tenant, "committed")
    async with SqlAlchemyProspectingUnitOfWork(factory, tenant, now=lambda: NOW) as uow:
        assert await uow.accounts.add(committed) is True
    async with factory() as session:
        repo = ProspectAccountRepositoryImpl(session, tenant)
        assert await repo.get(tenant, committed.account_id) == committed

    rolled_back = _account(tenant, "rolled-back")
    with pytest.raises(RuntimeError, match="rollback marker"):
        async with SqlAlchemyProspectingUnitOfWork(factory, tenant, now=lambda: NOW) as uow:
            assert await uow.accounts.add(rolled_back) is True
            raise RuntimeError("rollback marker")
    async with factory() as session:
        repo = ProspectAccountRepositoryImpl(session, tenant)
        assert await repo.get(tenant, rolled_back.account_id) is None


class _StableTestHasher:
    def fingerprint(self, canonical_value: str) -> str:
        return hashlib.sha256(canonical_value.encode()).hexdigest()


async def _seed_verification_points(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    suffixes: tuple[str, ...],
) -> tuple[ProspectAccountId, list[ContactPointId]]:
    from infra.db.prospecting_uow import SqlAlchemyProspectingUnitOfWork

    account = _account(tenant, "verification")
    points: list[ContactPointId] = []
    async with SqlAlchemyProspectingUnitOfWork(
        factory, tenant, now=lambda: NOW
    ) as uow:
        assert await uow.accounts.add(account) is True
        for suffix in suffixes:
            contact = _contact(tenant, account.account_id, suffix)
            point = _point(tenant, contact.contact_id, suffix)
            await uow.contacts.add_contact(contact)
            assert await uow.contacts.add_contact_point(point) is True
            points.append(point.contact_point_id)
    return account.account_id, points


def _verification_service(
    factory: async_sessionmaker[AsyncSession],
    *,
    now: datetime = NOW,
):
    from domains.prospecting.service_impl import ProspectingServiceImpl
    from infra.db.prospecting_uow import SqlAlchemyProspectingUnitOfWork

    return ProspectingServiceImpl(
        lambda bound: SqlAlchemyProspectingUnitOfWork(
            factory, bound, now=lambda: now
        ),
        _StableTestHasher(),
        now=lambda: now,
    )


def _verification_request(
    contact_point_id: ContactPointId,
    result: object,
    *,
    checked_at: datetime = NOW,
    provider: str = "provider-v1",
    cost_note: str = "hunter.email_verifier.counted",
) -> VerificationRecordRequest:
    return VerificationRecordRequest(
        contact_point_id,
        result,  # type: ignore[arg-type]
        provider,
        checked_at,
        cost_note,
    )


async def _seed_service_contact_points(
    service: object,
    tenant: TenantId,
    *,
    domain: str,
    values: tuple[tuple[ContactPointKind, str], ...],
) -> tuple[
    ProspectAccountId,
    ProspectContactId,
    list[ContactPointId],
    ContactPointCreateRequest,
]:
    from domains.prospecting.schemas import (
        AccountResolveRequest,
        ContactCreateRequest,
        ContactType,
        LegalBasisInput,
        LegalBasisType,
        SubjectType,
    )

    account_id = await service.resolve_account(  # type: ignore[attr-defined]
        tenant,
        AccountResolveRequest(
            entity_name="Privacy Test Account",
            country="DE",
            website_domain=domain,
        ),
    )
    contact_id = await service.create_contact(  # type: ignore[attr-defined]
        tenant,
        ContactCreateRequest(
            account_id=account_id,
            full_name="Privacy Test Contact",
            role_title="Procurement Manager",
            language="en",
        ),
    )
    basis = LegalBasisInput(
        basis=LegalBasisType.LEGITIMATE_INTEREST,
        subject_type=SubjectType.LEGAL_ENTITY,
        contact_type=ContactType.PERSONAL_BUSINESS,
        source="company_website",
        source_url=f"https://{domain}/contact",
        collected_at=NOW,
        assessment_ref="lia-privacy-test",
    )
    point_ids = []
    first_request: ContactPointCreateRequest | None = None
    for kind, value in values:
        request = ContactPointCreateRequest(
            contact_id=contact_id,
            kind=kind,
            value=value,
            legal_basis=basis,
            enrichment_cost_note="privacy-provider-tier",
        )
        if first_request is None:
            first_request = request
        point_ids.append(
            await service.add_contact_point(tenant, request)  # type: ignore[attr-defined]
        )
    assert first_request is not None
    return account_id, contact_id, point_ids, first_request


async def test_service_resolves_and_records_contacts_with_compliance_gates(
    prospect_engine: AsyncEngine,
) -> None:
    from domains.prospecting.errors import (
        ErasedContactPointError,
        ProspectingConflictError,
    )
    from domains.prospecting.schemas import (
        AccountResolveRequest,
        ContactCreateRequest,
        ContactPointCreateRequest,
        ContactPointKind,
        ContactType,
        LegalBasisInput,
        LegalBasisType,
        SubjectType,
    )
    from domains.prospecting.service_impl import ProspectingServiceImpl
    from infra.db.prospecting_uow import SqlAlchemyProspectingUnitOfWork

    factory = async_sessionmaker(prospect_engine, expire_on_commit=False)
    tenant = TenantId("tenant-prospect-service")
    service = ProspectingServiceImpl(
        lambda bound: SqlAlchemyProspectingUnitOfWork(factory, bound, now=lambda: NOW),
        _StableTestHasher(),
        now=lambda: NOW,
    )
    first = await service.resolve_account(
        tenant,
        AccountResolveRequest(
            entity_name="Acme",
            country="DE",
            website_domain="Acme.Example.",
            source_signal_refs=("sig-1",),
        ),
    )
    repeated = await service.resolve_account(
        tenant,
        AccountResolveRequest(
            entity_name="Acme GmbH",
            country="DE",
            website_domain="acme.example",
            source_signal_refs=("sig-2",),
        ),
    )
    assert repeated == first
    view = await service.get_account(tenant, first)
    assert view.website_domain == "acme.example"
    assert view.source_signal_refs == ("sig-1", "sig-2")
    without_domain_a = await service.resolve_account(
        tenant, AccountResolveRequest(entity_name="Same Name", country="DE")
    )
    without_domain_b = await service.resolve_account(
        tenant, AccountResolveRequest(entity_name="Same Name", country="DE")
    )
    assert without_domain_a != without_domain_b

    contact_id = await service.create_contact(
        tenant,
        ContactCreateRequest(
            account_id=first,
            full_name="Alex Buyer",
            role_title="Procurement Manager",
            language="en",
        ),
    )
    basis = LegalBasisInput(
        basis=LegalBasisType.LEGITIMATE_INTEREST,
        subject_type=SubjectType.LEGAL_ENTITY,
        contact_type=ContactType.PERSONAL_BUSINESS,
        source="company_website",
        source_url="https://acme.example/contact",
        collected_at=NOW,
        assessment_ref="lia-1",
    )
    request = ContactPointCreateRequest(
        contact_id=contact_id,
        kind=ContactPointKind.EMAIL,
        value="Buyer.Name@Acme.Example",
        legal_basis=basis,
        enrichment_cost_note="provider tier one",
    )
    point_id = await service.add_contact_point(tenant, request)
    assert await service.add_contact_point(tenant, request) == point_id
    with pytest.raises(ProspectingConflictError, match="联系方式唯一身份冲突"):
        await service.add_contact_point(
            tenant,
            ContactPointCreateRequest(
                contact_id=contact_id,
                kind=request.kind,
                value=request.value,
                legal_basis=LegalBasisInput(
                    basis=basis.basis,
                    subject_type=basis.subject_type,
                    contact_type=basis.contact_type,
                    source="different_source",
                    collected_at=NOW,
                    assessment_ref="lia-1",
                ),
                enrichment_cost_note=request.enrichment_cost_note,
            ),
        )

    erased_value = "erased@acme.example"
    erased_hash = _StableTestHasher().fingerprint(erased_value)
    async with SqlAlchemyProspectingUnitOfWork(factory, tenant, now=lambda: NOW) as uow:
        assert await uow.contacts.erase_personal_data(tenant, erased_hash) == 0
    with pytest.raises(ErasedContactPointError, match="联系方式已被删除"):
        await service.add_contact_point(
            tenant,
            ContactPointCreateRequest(
                contact_id=contact_id,
                kind=ContactPointKind.EMAIL,
                value=erased_value,
                legal_basis=basis,
            ),
        )


async def test_verification_state_machine_filters_and_publishes_metadata_only(
    prospect_engine: AsyncEngine,
) -> None:
    from domains.prospecting.errors import ContactPointNotFoundError
    from infra.db.prospecting_uow import SqlAlchemyProspectingUnitOfWork

    factory = async_sessionmaker(prospect_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    account_id, point_ids = await _seed_verification_points(
        factory, tenant, ("verify-a", "verify-b", "verify-c", "verify-d")
    )
    service = _verification_service(factory)
    results = (
        _models.VerificationStatus.UNVERIFIED,
        _models.VerificationStatus.VERIFIED,
        _models.VerificationStatus.RISKY,
        _models.VerificationStatus.INVALID,
    )
    for point_id, result in zip(point_ids, results, strict=True):
        await service.record_verification(
            tenant, _verification_request(point_id, result)
        )

    async with SqlAlchemyProspectingUnitOfWork(
        factory, tenant, now=lambda: NOW
    ) as uow:
        stored = [
            await uow.contacts.get_contact_point(tenant, point_id)
            for point_id in point_ids
        ]
    assert [point.verification for point in stored if point is not None] == list(
        results
    )
    assert stored[0] is not None and stored[0].verification_provider == "provider-v1"
    assert stored[0].verification_checked_at == NOW
    assert stored[0].verification_cost_note == "hunter.email_verifier.counted"
    assert stored[1] is not None and stored[1].verified_at == NOW
    assert stored[1].verification_provider == "provider-v1"
    assert stored[2] is not None and stored[2].verified_at is None
    assert stored[3] is not None and stored[3].verified_at is None
    visible = await service.list_verified_contact_points(tenant, account_id)
    assert [item.contact_point_id for item in visible] == [point_ids[1]]

    async with factory() as session:
        events = (
            await session.execute(
                select(OutboxEventRow).where(
                    OutboxEventRow.tenant_id == str(tenant),
                    OutboxEventRow.event_type == "ContactPointVerified",
                )
            )
        ).scalars().all()
    assert len(events) == 1
    assert set(events[0].event_payload) == {
        "tenant_id",
        "occurred_at",
        "run_id",
        "contact_point_id",
        "verification_result",
    }
    assert events[0].event_payload["contact_point_id"] == str(point_ids[1])
    assert events[0].event_payload["verification_result"] == "verified"

    await service.record_verification(
        tenant,
        _verification_request(
            point_ids[1],
            _models.VerificationStatus.VERIFIED,
            checked_at=NOW + timedelta(seconds=1),
            provider="provider-v2",
        ),
    )
    async with factory() as session:
        repeated = (
            await session.execute(
                select(OutboxEventRow).where(
                    OutboxEventRow.tenant_id == str(tenant),
                    OutboxEventRow.event_type == "ContactPointVerified",
                )
            )
        ).scalars().all()
    assert len(repeated) == 1

    await service.record_verification(
        tenant,
        _verification_request(
            point_ids[1],
            _models.VerificationStatus.RISKY,
            checked_at=NOW + timedelta(seconds=2),
            provider="provider-v3",
        ),
    )
    assert await service.list_verified_contact_points(tenant, account_id) == []
    async with SqlAlchemyProspectingUnitOfWork(
        factory, tenant, now=lambda: NOW
    ) as uow:
        risky = await uow.contacts.get_contact_point(tenant, point_ids[1])
    assert risky is not None
    assert risky.verification is _models.VerificationStatus.RISKY
    assert risky.verified_at is None
    assert risky.verification_provider == "provider-v3"
    await service.record_verification(
        tenant,
        _verification_request(
            point_ids[1],
            _models.VerificationStatus.VERIFIED,
            checked_at=NOW + timedelta(seconds=3),
            provider="provider-v4",
        ),
    )
    async with factory() as session:
        reverified = (
            await session.execute(
                select(OutboxEventRow).where(
                    OutboxEventRow.tenant_id == str(tenant),
                    OutboxEventRow.event_type == "ContactPointVerified",
                )
            )
        ).scalars().all()
    assert len(reverified) == 2

    with pytest.raises(ContactPointNotFoundError, match="潜在联系方式不存在"):
        await service.record_verification(
            TenantId(new_id("tn")),
            _verification_request(point_ids[1], _models.VerificationStatus.VERIFIED),
        )
    with pytest.raises(ContactPointNotFoundError, match="潜在联系方式不存在"):
        await service.record_verification(
            tenant,
            _verification_request(
                ContactPointId(new_id("cp")), _models.VerificationStatus.VERIFIED
            ),
        )

    with pytest.raises(ContactPointNotFoundError, match="潜在联系方式不存在"):
        await service.get_contact_point(TenantId(new_id("tn")), point_ids[1])
    point_view = await service.get_contact_point(tenant, point_ids[1])
    assert point_view.account_id == account_id
    assert point_view.verification_checked_at == NOW + timedelta(seconds=3)


async def test_concurrent_verification_emits_exactly_one_event(
    prospect_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(prospect_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    _account_id, point_ids = await _seed_verification_points(
        factory, tenant, ("concurrent-a",)
    )
    point_id = point_ids[0]
    service = _verification_service(factory)
    await asyncio.gather(
        *(
            service.record_verification(
                tenant,
                _verification_request(point_id, _models.VerificationStatus.VERIFIED),
            )
            for _ in range(20)
        )
    )
    async with factory() as session:
        events = (
            await session.execute(
                select(OutboxEventRow).where(
                    OutboxEventRow.tenant_id == str(tenant),
                    OutboxEventRow.event_type == "ContactPointVerified",
                )
            )
        ).scalars().all()
    assert len(events) == 1


async def test_verification_bus_failure_rolls_back_and_retry_succeeds(
    prospect_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from infra.db.outbox import PostgresEventBus
    from infra.db.prospecting_uow import SqlAlchemyProspectingUnitOfWork

    factory = async_sessionmaker(prospect_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    _account_id, point_ids = await _seed_verification_points(
        factory, tenant, ("rollback-a",)
    )
    point_id = point_ids[0]
    service = _verification_service(factory)

    async def _fail_publish(_bus: PostgresEventBus, _event: object) -> None:
        raise RuntimeError("outbox failure marker")

    with monkeypatch.context() as scoped:
        scoped.setattr(PostgresEventBus, "publish", _fail_publish)
        with pytest.raises(RuntimeError, match="outbox failure marker"):
            await service.record_verification(
                tenant,
                _verification_request(point_id, _models.VerificationStatus.VERIFIED),
            )

    async with SqlAlchemyProspectingUnitOfWork(
        factory, tenant, now=lambda: NOW
    ) as uow:
        rolled_back = await uow.contacts.get_contact_point(tenant, point_id)
    assert rolled_back is not None
    assert rolled_back.verification is _models.VerificationStatus.UNVERIFIED
    assert rolled_back.verified_at is None
    assert rolled_back.verification_provider is None
    async with factory() as session:
        events_before_retry = (
            await session.execute(
                select(OutboxEventRow).where(
                    OutboxEventRow.tenant_id == str(tenant),
                    OutboxEventRow.event_type == "ContactPointVerified",
                )
            )
        ).scalars().all()
    assert events_before_retry == []

    await service.record_verification(
        tenant,
        _verification_request(point_id, _models.VerificationStatus.VERIFIED),
    )
    async with factory() as session:
        events_after_retry = (
            await session.execute(
                select(OutboxEventRow).where(
                    OutboxEventRow.tenant_id == str(tenant),
                    OutboxEventRow.event_type == "ContactPointVerified",
                )
            )
        ).scalars().all()
    assert len(events_after_retry) == 1


async def test_erasure_service_removes_personal_data_and_blocks_recollection(
    prospect_engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    from domains.prospecting.errors import ErasedContactPointError
    from infra.db.prospecting_uow import SqlAlchemyProspectingUnitOfWork
    from infra.db.tables import (
        ContactLegalBasisRow,
        ContactPointRow,
        ProspectAccountRow,
        ProspectContactRow,
        ProspectingErasureSuppressionRow,
    )

    factory = async_sessionmaker(prospect_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    other_tenant = TenantId(new_id("tn"))
    service = _verification_service(factory)
    sensitive_value = "erase.marker.9271@Privacy.Example"
    canonical_sensitive = "erase.marker.9271@privacy.example"
    phone_value = "+491234567890"
    unknown_value = "unknown.marker.9271@privacy.example"
    account_id, contact_id, point_ids, email_request = (
        await _seed_service_contact_points(
            service,
            tenant,
            domain="privacy-a.example",
            values=(
                (ContactPointKind.EMAIL, sensitive_value),
                (ContactPointKind.PHONE, phone_value),
            ),
        )
    )
    other_account, other_contact, other_points, _other_request = (
        await _seed_service_contact_points(
            service,
            other_tenant,
            domain="privacy-b.example",
            values=((ContactPointKind.EMAIL, sensitive_value),),
        )
    )
    del other_account, other_contact

    caplog.clear()
    assert await service.handle_erasure_request(tenant, sensitive_value) == 1
    assert await service.handle_erasure_request(tenant, sensitive_value) == 0
    assert await service.handle_erasure_request(tenant, unknown_value) == 0
    with pytest.raises(ErasedContactPointError, match="联系方式已被删除") as error:
        await service.add_contact_point(tenant, email_request)  # type: ignore[attr-defined]
    assert canonical_sensitive not in str(error.value)
    assert sensitive_value not in str(error.value)

    async with SqlAlchemyProspectingUnitOfWork(
        factory, tenant, now=lambda: NOW
    ) as uow:
        assert await uow.contacts.get_contact_point(tenant, point_ids[0]) is None
        assert await uow.contacts.get_contact_point(tenant, point_ids[1]) is not None
        assert await uow.contacts.get_contact(tenant, contact_id) is not None
        assert await uow.accounts.get(tenant, account_id) is not None
    async with SqlAlchemyProspectingUnitOfWork(
        factory, other_tenant, now=lambda: NOW
    ) as uow:
        assert (
            await uow.contacts.get_contact_point(other_tenant, other_points[0])
            is not None
        )

    assert await service.handle_erasure_request(tenant, phone_value) == 1
    async with SqlAlchemyProspectingUnitOfWork(
        factory, tenant, now=lambda: NOW
    ) as uow:
        assert await uow.contacts.get_contact(tenant, contact_id) is None
        assert await uow.accounts.get(tenant, account_id) is not None

    async with factory() as session:
        snapshot = {
            "points": (
                await session.execute(
                    select(ContactPointRow.__table__).where(
                        ContactPointRow.tenant_id == str(tenant)
                    )
                )
            ).mappings().all(),
            "basis": (
                await session.execute(
                    select(ContactLegalBasisRow.__table__).where(
                        ContactLegalBasisRow.tenant_id == str(tenant)
                    )
                )
            ).mappings().all(),
            "contacts": (
                await session.execute(
                    select(ProspectContactRow.__table__).where(
                        ProspectContactRow.tenant_id == str(tenant)
                    )
                )
            ).mappings().all(),
            "accounts": (
                await session.execute(
                    select(ProspectAccountRow.__table__).where(
                        ProspectAccountRow.tenant_id == str(tenant)
                    )
                )
            ).mappings().all(),
            "outbox": (
                await session.execute(
                    select(OutboxEventRow.__table__).where(
                        OutboxEventRow.tenant_id == str(tenant)
                    )
                )
            ).mappings().all(),
            "suppressions": (
                await session.execute(
                    select(ProspectingErasureSuppressionRow.__table__).where(
                        ProspectingErasureSuppressionRow.tenant_id == str(tenant)
                    )
                )
            ).mappings().all(),
        }
    persisted = repr(snapshot)
    for marker in (sensitive_value, canonical_sensitive, phone_value, unknown_value):
        assert marker not in persisted
        assert marker not in caplog.text


async def test_concurrent_erasure_deletes_once_and_keeps_one_suppression(
    prospect_engine: AsyncEngine,
) -> None:
    from infra.db.tables import ProspectingErasureSuppressionRow

    factory = async_sessionmaker(prospect_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    service = _verification_service(factory)
    raw_value = "concurrent.erase@privacy.example"
    _account_id, _contact_id, _point_ids, _request = (
        await _seed_service_contact_points(
            service,
            tenant,
            domain="privacy-concurrent.example",
            values=((ContactPointKind.EMAIL, raw_value),),
        )
    )
    results = await asyncio.gather(
        *(service.handle_erasure_request(tenant, raw_value) for _ in range(20))  # type: ignore[attr-defined]
    )
    assert sum(results) == 1
    async with factory() as session:
        suppression_count = await session.scalar(
            select(func.count())
            .select_from(ProspectingErasureSuppressionRow)
            .where(ProspectingErasureSuppressionRow.tenant_id == str(tenant))
        )
    assert suppression_count == 1


async def test_erasure_repository_failure_rolls_back_then_retry_succeeds(
    prospect_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from infra.db.prospecting_uow import SqlAlchemyProspectingUnitOfWork
    from infra.db.repositories.prospecting import ProspectContactRepositoryImpl

    factory = async_sessionmaker(prospect_engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    service = _verification_service(factory)
    raw_value = "rollback.erase@privacy.example"
    _account_id, _contact_id, point_ids, _request = (
        await _seed_service_contact_points(
            service,
            tenant,
            domain="privacy-rollback.example",
            values=((ContactPointKind.EMAIL, raw_value),),
        )
    )
    original = ProspectContactRepositoryImpl.erase_personal_data

    async def _fail_after_erasure(
        repository: ProspectContactRepositoryImpl,
        bound_tenant: TenantId,
        value_hash: str,
    ) -> int:
        await original(repository, bound_tenant, value_hash)
        raise RuntimeError("erasure rollback marker")

    with monkeypatch.context() as scoped:
        scoped.setattr(
            ProspectContactRepositoryImpl,
            "erase_personal_data",
            _fail_after_erasure,
        )
        with pytest.raises(RuntimeError, match="erasure rollback marker"):
            await service.handle_erasure_request(tenant, raw_value)  # type: ignore[attr-defined]

    value_hash = _StableTestHasher().fingerprint(raw_value)
    async with SqlAlchemyProspectingUnitOfWork(
        factory, tenant, now=lambda: NOW
    ) as uow:
        assert await uow.contacts.get_contact_point(tenant, point_ids[0]) is not None
        assert await uow.contacts.is_erasure_suppressed(tenant, value_hash) is False
    assert await service.handle_erasure_request(tenant, raw_value) == 1  # type: ignore[attr-defined]
