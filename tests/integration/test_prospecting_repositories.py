"""prospecting PostgreSQL repositories/UoW 集成契约。"""

from __future__ import annotations

import hashlib
import importlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from shared.errors import TenantIsolationViolation
from shared.schemas.identifiers import (
    ContactPointId,
    ProspectAccountId,
    ProspectContactId,
    TenantId,
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
