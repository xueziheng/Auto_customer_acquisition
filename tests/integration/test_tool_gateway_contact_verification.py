"""contact.verify PostgreSQL ledger 的邮箱/Key 不落盘最小全链测试。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Self

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from connectors.email_verification.client import (
    EmailVerificationOutcome,
    EmailVerificationResult,
    VerificationCostNote,
)
from infra.db.session import create_engine_from
from infra.db.tables import OutboxEventRow, ToolCallEventRow, ToolCallRow
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.schemas.identifiers import (
    ContactPointId,
    ProspectAccountId,
    ProspectContactId,
    TenantId,
    UserId,
    new_id,
)
from tool_gateway.checks.contact_provider import (
    ContactProviderSuppressionCheck,
    ContactResourceTenantCheck,
    ContactVerificationPreflight,
)
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.contact_verification import (
    MANIFEST,
    ContactVerificationHandler,
    ToolGatewayContactVerifier,
)
from tool_gateway.handlers.single_result_slot import ContextLocalSingleResultSlot
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import ToolCallContext, ToolGateway, ToolInvocationState

NOW = datetime(2026, 8, 21, 16, tzinfo=UTC)
EMAIL = "private-ledger-verify@example.com"
KEY = "private-hunter-key-canary"


class _Reader:
    def __init__(self):
        self.calls = 0

    async def verify(self, tenant_id, email):
        assert email == EMAIL
        self.calls += 1
        return EmailVerificationResult(
            EmailVerificationOutcome.VERIFIED,
            "hunter",
            NOW,
            VerificationCostNote.COUNTED,
        )


class _SuppressionReader:
    async def is_suppressed(self, tenant_id, target):
        del tenant_id, target
        return False


class _Stage:
    def __init__(self, name, preflight):
        self.name, self.preflight = name, preflight

    async def check(self, ctx, state):
        if self.name == "suppression":
            state.preflight = self.preflight


class _FailSuccessfulCompletionCalls:
    def __init__(self, delegate: object) -> None:
        self._delegate = delegate

    def __getattr__(self, name: str):
        return getattr(self._delegate, name)

    async def complete(self, *args, **kwargs) -> None:
        if kwargs.get("status") is ToolCallStatus.SUCCEEDED:
            raise RuntimeError("ledger completion failure canary")
        await self._delegate.complete(*args, **kwargs)


class _FailSuccessfulCompletionUow:
    def __init__(self, delegate: SqlAlchemyToolGatewayUnitOfWork) -> None:
        self._delegate = delegate

    async def __aenter__(self) -> Self:
        entered = await self._delegate.__aenter__()
        self.calls = _FailSuccessfulCompletionCalls(entered.calls)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._delegate.__aexit__(exc_type, exc, tb)


class _StableHasher:
    def fingerprint(self, canonical_value: str) -> str:
        return hashlib.sha256(canonical_value.encode()).hexdigest()


def _gateway(
    factory: async_sessionmaker[AsyncSession],
    tenant: TenantId,
    user: UserId,
    point,
    reader: _Reader,
    slot: ContextLocalSingleResultSlot[EmailVerificationResult],
    *,
    failing_completion: bool = False,
) -> ToolGatewayContactVerifier:
    preflight = ContactVerificationPreflight(tenant, point, False)
    handler = ContactVerificationHandler(
        reader, slot, HmacFingerprintProvider("verify-v1", b"v" * 32), now=lambda: NOW
    )
    registry = ToolRegistry()
    registry.register(MANIFEST, handler)
    stages = {
        "tenant": ContactResourceTenantCheck(),
        "permission": _Stage("permission", preflight),
        "suppression": _Stage("suppression", preflight),
        "rate_limit": _Stage("rate_limit", preflight),
    }

    def uow_factory(requested: TenantId):
        base = SqlAlchemyToolGatewayUnitOfWork(factory, requested, now=lambda: NOW)
        if failing_completion:
            return _FailSuccessfulCompletionUow(base)
        return base

    gateway = ToolGateway(
        registry,
        stages,
        uow_factory,
        lease_duration=timedelta(minutes=1),
        lease_owner="verify",
        now=lambda: NOW,
        id_factory=new_id,
    )
    return ToolGatewayContactVerifier(gateway, slot, user)


@pytest.mark.asyncio
async def test_verify_ledger_contains_only_veb_handle(db_url: str, caplog) -> None:
    from domains.prospecting.schemas import (
        ContactPointKind,
        ContactPointView,
        VerificationStatus,
    )

    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant, user, point_id = (
        TenantId(new_id("tn")),
        UserId(new_id("usr")),
        ContactPointId(new_id("cp")),
    )
    point = ContactPointView(
        point_id,
        tenant,
        ProspectContactId(new_id("con")),
        ProspectAccountId(new_id("acc")),
        ContactPointKind.EMAIL,
        EMAIL,
        VerificationStatus.UNVERIFIED,
        NOW,
    )
    preflight = ContactVerificationPreflight(tenant, point, False)
    reader = _Reader()
    slot = ContextLocalSingleResultSlot("veb", new_id)
    handler = ContactVerificationHandler(
        reader, slot, HmacFingerprintProvider("verify-v1", b"v" * 32), now=lambda: NOW
    )
    registry = ToolRegistry()
    registry.register(MANIFEST, handler)
    stages = {
        "tenant": ContactResourceTenantCheck(),
        "permission": _Stage("permission", preflight),
        "suppression": _Stage("suppression", preflight),
        "rate_limit": _Stage("rate_limit", preflight),
    }
    gateway = ToolGateway(
        registry,
        stages,
        lambda requested: SqlAlchemyToolGatewayUnitOfWork(
            factory, requested, now=lambda: NOW
        ),
        lease_duration=timedelta(minutes=1),
        lease_owner="verify",
        now=lambda: NOW,
        id_factory=new_id,
    )
    try:
        result = await ToolGatewayContactVerifier(gateway, slot, user).verify(
            tenant, point_id
        )
        assert (
            result.outcome is EmailVerificationOutcome.VERIFIED
            and reader.calls == 1
            and slot.is_empty
        )
        async with factory() as session:
            rows = (
                (
                    await session.execute(
                        select(ToolCallRow).where(ToolCallRow.tenant_id == tenant)
                    )
                )
                .scalars()
                .all()
            )
            events = (
                (
                    await session.execute(
                        select(ToolCallEventRow).where(
                            ToolCallEventRow.tenant_id == tenant
                        )
                    )
                )
                .scalars()
                .all()
            )
            outbox = (
                (
                    await session.execute(
                        select(OutboxEventRow).where(OutboxEventRow.tenant_id == tenant)
                    )
                )
                .scalars()
                .all()
            )
        assert rows[0].provider_ref.startswith("veb_")
        values = repr(
            (
                [
                    {
                        c.name: getattr(row, c.name)
                        for c in ToolCallRow.__table__.columns
                    }
                    for row in rows
                ],
                events,
                outbox,
                caplog.records,
            )
        )
        for forbidden in (EMAIL, KEY, "score", "source"):
            assert forbidden not in values
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_failed_ledger_completion_returns_no_result_and_clears_slot(
    db_url: str,
) -> None:
    from domains.prospecting.schemas import (
        ContactPointKind,
        ContactPointView,
        VerificationStatus,
    )

    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    user = UserId(new_id("usr"))
    point_id = ContactPointId(new_id("cp"))
    point = ContactPointView(
        point_id,
        tenant,
        ProspectContactId(new_id("con")),
        ProspectAccountId(new_id("acc")),
        ContactPointKind.EMAIL,
        EMAIL,
        VerificationStatus.UNVERIFIED,
        NOW,
    )
    reader = _Reader()
    slot = ContextLocalSingleResultSlot("veb", new_id)
    adapter = _gateway(
        factory,
        tenant,
        user,
        point,
        reader,
        slot,
        failing_completion=True,
    )
    try:
        with pytest.raises(ToolGatewayError) as captured:
            await adapter.verify(tenant, point_id)
        assert captured.value.category is ToolErrorCategory.RECONCILIATION_REQUIRED
        assert reader.calls == 1
        assert slot.is_empty
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_cross_tenant_contact_point_is_indistinguishable_from_missing(
    db_url: str,
) -> None:
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

    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    owner = TenantId(new_id("tn"))
    outsider = TenantId(new_id("tn"))
    service = ProspectingServiceImpl(
        lambda bound: SqlAlchemyProspectingUnitOfWork(factory, bound, now=lambda: NOW),
        _StableHasher(),
        now=lambda: NOW,
    )
    account_id = await service.resolve_account(
        owner,
        AccountResolveRequest(
            entity_name="Tenant Isolation Canary",
            country="DE",
            website_domain="tenant-isolation.example",
        ),
    )
    contact_id = await service.create_contact(
        owner,
        ContactCreateRequest(account_id, full_name="Isolation Canary"),
    )
    point_id = await service.add_contact_point(
        owner,
        ContactPointCreateRequest(
            contact_id,
            ContactPointKind.EMAIL,
            "tenant-isolation@example.com",
            LegalBasisInput(
                LegalBasisType.LEGITIMATE_INTEREST,
                SubjectType.LEGAL_ENTITY,
                ContactType.PERSONAL_BUSINESS,
                "company_website",
                NOW,
                assessment_ref="lia-isolation",
            ),
        ),
    )
    try:
        check = ContactProviderSuppressionCheck(
            service,
            _SuppressionReader(),
            now=lambda: NOW,
        )
        outcomes = []
        for tenant_id, candidate in (
            (outsider, point_id),
            (outsider, ContactPointId(new_id("cp"))),
        ):
            state = ToolInvocationState(MANIFEST, new_id("tcl"))
            ctx = ToolCallContext(
                tenant_id,
                UserId(new_id("usr")),
                "contact.verify",
                {"contact_point_id": str(candidate)},
            )
            with pytest.raises(ToolGatewayError) as captured:
                await check.check(ctx, state)
            outcomes.append(
                (captured.value.category, captured.value.retry_after_seconds)
            )
        assert outcomes == [
            (ToolErrorCategory.PROVIDER_TRANSIENT, None),
            (ToolErrorCategory.PROVIDER_TRANSIENT, None),
        ]
    finally:
        await engine.dispose()
