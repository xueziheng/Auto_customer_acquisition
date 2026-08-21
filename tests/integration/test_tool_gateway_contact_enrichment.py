"""contact.enrich 经真实 PostgreSQL ledger 的 PII/凭证不落盘证明。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from connectors.contact_enrichment.client import ContactEnrichmentResult
from connectors.hunter.client import HunterConnector
from connectors.hunter.transport import HunterHttpResponse, HunterNetworkError
from infra.db.session import create_engine_from
from infra.db.tables import OutboxEventRow, ToolCallEventRow, ToolCallRow
from infra.db.tool_gateway_uow import SqlAlchemyToolGatewayUnitOfWork
from shared.schemas.identifiers import (
    NeedHypothesisId,
    ProspectAccountId,
    TenantId,
    UserId,
    new_id,
)
from tool_gateway.checks.contact_provider import (
    ContactDiscoveryPreflight,
    ContactResourceTenantCheck,
)
from tool_gateway.errors import ToolCallStatus, ToolErrorCategory, ToolGatewayError
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.contact_enrichment import (
    MANIFEST,
    ContactEnrichmentHandler,
    ToolGatewayContactEnricher,
    _HunterProviderContactEnricher,
)
from tool_gateway.handlers.single_result_slot import ContextLocalSingleResultSlot
from tool_gateway.manifest import ToolRegistry
from tool_gateway.pipeline import CheckRejection, ToolCallContext, ToolGateway

NOW = datetime(2026, 8, 21, 13, tzinfo=UTC)
KEY = "hunter-integration-key-canary"
EMAIL = "private-integration-email@example.com"
NAME = "Private Integration Name"
SOURCE = "https://source.example/private-integration"


class _Secrets:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, secret_ref: str) -> str:
        assert secret_ref == "HUNTER_API_KEY_REF"
        self.calls += 1
        return KEY


class _Transport:
    def __init__(self) -> None:
        self.calls = 0
        self.error: BaseException | None = None

    async def get(self, path, params, *, api_key):
        assert path == "/domain-search"
        assert api_key == KEY
        self.calls += 1
        if self.error is not None:
            raise self.error
        return HunterHttpResponse(
            200,
            {
                "data": {
                    "linked_domains": [],
                    "emails": [
                        {
                            "value": EMAIL,
                            "type": "personal",
                            "first_name": "Private Integration",
                            "last_name": "Name",
                            "position": "Sales",
                            "department": "Sales",
                            "seniority": "senior",
                            "confidence": 99,
                            "sources": [
                                {
                                    "uri": SOURCE,
                                    "extracted_on": "2026-08-01",
                                    "last_seen_on": "2026-08-20",
                                    "still_on_page": True,
                                }
                            ],
                        }
                    ],
                },
                "meta": {"results": 1},
            },
        )


class _Stage:
    def __init__(self, name: str, preflight: ContactDiscoveryPreflight, *, allow: bool = True) -> None:
        self.name = name
        self.preflight = preflight
        self.allow = allow

    async def check(self, _ctx, state):
        if not self.allow:
            return CheckRejection(self.name, f"{self.name}:denied", "当前检查拒绝")
        if self.name == "playbook":
            state.preflight = self.preflight
        return None


@pytest.mark.asyncio
async def test_real_gateway_is_late_configured_and_ledger_contains_only_handle(
    db_url: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    engine = create_engine_from(db_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    tenant = TenantId(new_id("tn"))
    user = UserId(new_id("usr"))
    hypothesis = NeedHypothesisId(new_id("hyp"))
    account = ProspectAccountId(new_id("acc"))
    preflight = ContactDiscoveryPreflight(
        tenant, hypothesis, account, "category", "DE", "example.com"
    )
    secrets = _Secrets()
    transport = _Transport()
    factory_calls: list[TenantId] = []

    def connector_factory(requested_tenant: TenantId) -> HunterConnector:
        factory_calls.append(requested_tenant)
        return HunterConnector(transport, now=lambda: NOW)

    slot = ContextLocalSingleResultSlot[ContactEnrichmentResult]("ceb", new_id)
    handler = ContactEnrichmentHandler(
        _HunterProviderContactEnricher(connector_factory, secrets),
        slot,
        HmacFingerprintProvider("contact-v1", b"c" * 32),
    )
    registry = ToolRegistry()
    registry.register(MANIFEST, handler)

    def gateway(permission: bool) -> ToolGateway:
        stages = {
            "tenant": ContactResourceTenantCheck(),
            "permission": _Stage("permission", preflight, allow=permission),
            "playbook": _Stage("playbook", preflight),
            "country_policy": _Stage("country_policy", preflight),
            "suppression": _Stage("suppression", preflight),
            "rate_limit": _Stage("rate_limit", preflight),
        }
        return ToolGateway(
            registry,
            stages,
            lambda requested: SqlAlchemyToolGatewayUnitOfWork(factory, requested, now=lambda: NOW),
            lease_duration=timedelta(minutes=1),
            lease_owner="contact-enrichment",
            now=lambda: NOW,
            id_factory=new_id,
        )

    params = {"hypothesis_id": str(hypothesis), "account_id": str(account), "role_hints": ()}
    try:
        denied = await gateway(False).invoke(ToolCallContext(tenant, user, "contact.enrich", params))
        assert denied.status is ToolCallStatus.REJECTED
        assert factory_calls == []
        assert secrets.calls == 0
        assert transport.calls == 0

        trusted = ToolGatewayContactEnricher(gateway(True), slot, user)
        result = await trusted.find_contacts(tenant, hypothesis, account, ())
        assert result.candidates[0].email == EMAIL
        assert slot.is_empty
        assert factory_calls == [tenant]
        assert secrets.calls == 1
        assert transport.calls == 1

        transport.error = HunterNetworkError(True)
        with pytest.raises(ToolGatewayError) as uncertain:
            await trusted.find_contacts(tenant, hypothesis, account, ())
        assert uncertain.value.category is ToolErrorCategory.RECONCILIATION_REQUIRED
        assert transport.calls == 2
        assert slot.is_empty

        async with factory() as session:
            rows = (await session.execute(select(ToolCallRow).where(ToolCallRow.tenant_id == tenant))).scalars().all()
            events = (await session.execute(select(ToolCallEventRow).where(ToolCallEventRow.tenant_id == tenant))).scalars().all()
            outbox = (await session.execute(select(OutboxEventRow).where(OutboxEventRow.tenant_id == tenant))).scalars().all()
        assert {row.status for row in rows} == {
            "rejected",
            "succeeded",
            "failed_transient",
        }
        succeeded = next(row for row in rows if row.status == "succeeded")
        assert succeeded.provider_ref.startswith("ceb_")
        row_values = [
            {column.name: getattr(row, column.name) for column in ToolCallRow.__table__.columns}
            for row in rows
        ]
        event_values = [
            {column.name: getattr(event, column.name) for column in ToolCallEventRow.__table__.columns}
            for event in events
        ]
        outbox_values = [
            {column.name: getattr(event, column.name) for column in OutboxEventRow.__table__.columns}
            for event in outbox
        ]
        log_values = [(record.getMessage(), record.args) for record in caplog.records]
        rendered = repr((row_values, event_values, outbox_values, log_values))
        for forbidden in (KEY, EMAIL, NAME, SOURCE, "confidence"):
            assert forbidden not in rendered
    finally:
        await engine.dispose()
