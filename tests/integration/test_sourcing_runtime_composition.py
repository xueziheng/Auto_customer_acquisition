"""Task 13 production composition 的受控 PostgreSQL 与零网络验证。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from apps.scheduler_worker.config import SourcingSettings
from apps.scheduler_worker.sourcing_runtime import (
    PostgresSourcingNeedReader,
    SourcingResearchChain,
    SourcingResearchComposition,
    build_sourcing_case_composition,
    build_sourcing_research_chain,
)
from domains.costing.permissions import CostingScope
from domains.costing.service_impl import CostingServiceImpl
from domains.products.permissions import ProductRole
from domains.products.service_impl import ProductServiceImpl
from domains.sourcing.permissions import SourcingScope
from domains.sourcing.service_impl import SourcingServiceImpl
from domains.suppliers.service import SupplierRole
from domains.suppliers.service_impl import SupplierServiceImpl
from infra.db.run_audit import PostgresRunAuditRepository
from infra.db.tables import SourcingCaseRow, WorkflowRunRow
from shared.errors import ValidationError
from shared.events.catalog import (
    NeedBecameSourcingReady,
    NeedValidated,
    SourcingCandidatesReady,
    SourcingCandidatesVerified,
    SourcingCaseHandedToCosting,
)
from shared.schemas.identifiers import (
    RunId,
    SourcingCaseId,
    TenantId,
    UserId,
    ValidatedNeedId,
    new_id,
)
from tool_gateway.fingerprint import HmacFingerprintProvider
from tool_gateway.handlers.free_search import FreeSearchGatewaySearcher
from tool_gateway.handlers.web_read_page import ToolGatewayWebPageReader
from tool_gateway.handlers.web_search import ToolGatewayWebSearcher
from tool_gateway.handlers.web_slots import WebPageSnapshotSlot, WebSearchResultSlot

NOW = datetime(2026, 8, 31, 12, tzinfo=UTC)


class _NoNetworkGateway:
    calls = 0

    async def invoke(self, ctx: object) -> object:
        del ctx
        self.calls += 1
        raise AssertionError("composition 不得触发网络/Gateway 调用")


class _Quota:
    async def check_available(self, *args: object, **kwargs: object) -> None: ...
    async def reserve(self, *args: object, **kwargs: object) -> object: ...
    async def record_unavailable(self, *args: object, **kwargs: object) -> None: ...
    async def mark_dispatched(self, *args: object, **kwargs: object) -> None: ...
    async def consume(self, *args: object, **kwargs: object) -> None: ...
    async def acknowledge_uncertain_as_consumed(
        self, *args: object, **kwargs: object
    ) -> None: ...
    async def snapshot(self) -> object: ...
    async def get(self, *args: object, **kwargs: object) -> object: ...
    async def run_state(self, *args: object, **kwargs: object) -> object: ...


class _Model:
    model_identifier = "sourcing-model-v1"
    calls = 0

    async def extract_candidate(self, **kwargs: object) -> str:
        del kwargs
        self.calls += 1
        raise AssertionError("composition 不得调用模型")


class _SearchTransport:
    calls = 0

    async def search(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        raise AssertionError((args, kwargs))

    async def usage(self, **kwargs: object) -> object:
        self.calls += 1
        raise AssertionError(kwargs)


class _PageTransport:
    calls = 0

    async def validate_url(self, url: str) -> str:
        self.calls += 1
        raise AssertionError(url)

    async def fetch(self, url: str) -> object:
        self.calls += 1
        raise AssertionError(url)


class _Artifacts:
    calls = 0

    async def put(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        raise AssertionError((args, kwargs))

    async def get(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        raise AssertionError((args, kwargs))

    async def get_meta(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        raise AssertionError((args, kwargs))


class _Playbook:
    calls = 0

    async def allows_research(self, *args: object) -> bool:
        self.calls += 1
        raise AssertionError(args)


class _Secrets:
    calls = 0

    def resolve(self, secret_ref: str) -> str:
        self.calls += 1
        raise AssertionError(secret_ref)


class _CountryPolicy:
    calls = 0

    async def decision(self, *args: object) -> object:
        self.calls += 1
        raise AssertionError(args)


class _PlanReader:
    async def load_authorized(self, **kwargs: object) -> object:
        raise AssertionError(kwargs)


class _Receipts:
    async def restore(self, **kwargs: object) -> object:
        raise AssertionError(kwargs)

    async def commit_locator_receipt(self, **kwargs: object) -> None:
        raise AssertionError(kwargs)

    async def record_uncertain(self, **kwargs: object) -> None:
        raise AssertionError(kwargs)

    async def restore_page_attempts(self, **kwargs: object) -> tuple[object, ...]:
        raise AssertionError(kwargs)

    async def claim_page_attempt(self, **kwargs: object) -> object:
        raise AssertionError(kwargs)

    async def complete_page_attempt(self, **kwargs: object) -> object:
        raise AssertionError(kwargs)


class _Drafts:
    async def save(self, **kwargs: object) -> str:
        raise AssertionError(kwargs)


class _Opportunities:
    async def find_for_need(self, *args: object, **kwargs: object) -> None:
        return None


class _Engine:
    def __init__(self) -> None:
        self.definitions: list[object] = []

    def register(self, definition: object) -> None:
        self.definitions.append(definition)


class _Outbox:
    def __init__(self) -> None:
        self.events: list[type[object]] = []

    def register_handler(
        self, event_type: type[object], name: str, handler: object
    ) -> None:
        assert name and handler is not None
        self.events.append(event_type)


def _research(
    tenant: TenantId,
) -> tuple[SourcingResearchChain, _NoNetworkGateway, _Model]:
    gateway = _NoNetworkGateway()
    user = UserId(new_id("usr"))
    search_delegate = ToolGatewayWebSearcher(
        gateway,  # type: ignore[arg-type]
        WebSearchResultSlot(new_id, maximum_batches=4),
        user,
    )
    quota = _Quota()
    searcher = FreeSearchGatewaySearcher(search_delegate, quota, tenant)  # type: ignore[arg-type]
    page_reader = ToolGatewayWebPageReader(
        gateway,  # type: ignore[arg-type]
        WebPageSnapshotSlot(new_id),
        user,
    )
    model = _Model()
    return (
        SourcingResearchChain(
            tavily_secret_ref="TAVILY_DEPLOYMENT_REFERENCE",
            model_port=model,
            plan_reader=_PlanReader(),  # type: ignore[arg-type]
            quota=quota,  # type: ignore[arg-type]
            searcher=searcher,
            page_reader=page_reader,
            receipts=_Receipts(),  # type: ignore[arg-type]
            drafts=_Drafts(),
        ),
        gateway,
        model,
    )


@pytest.mark.asyncio
async def test_need_reader_preserves_exact_facts_and_provenance(
    integration_engine: AsyncEngine,
) -> None:
    tenant = TenantId(new_id("tn"))
    other = TenantId(new_id("tn"))
    need_id = ValidatedNeedId(new_id("need"))
    provenance = {
        "source_type": "conversation",
        "source_id": "msg-confirmed",
        "extracted_by": "human",
        "extracted_at": NOW.isoformat(),
        "confirmed_by": None,
        "confirmed_at": None,
        "source_url": None,
        "page_hash": None,
        "source_quote": None,
    }
    category = json.dumps({"value": "hinges", "provenance": provenance})
    application = json.dumps({"value": "marine door", "provenance": provenance})
    quantity = json.dumps({"value": 5000, "provenance": provenance})
    async with integration_engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO validated_needs "
                "(tenant_id, need_id, account_id, product_category, source_message_id, "
                "status, application, quantity, created_at) VALUES "
                "(:tenant, :need, 'account-runtime', CAST(:category AS jsonb), "
                "'message-runtime', 'sourcing_ready', CAST(:application AS jsonb), "
                "CAST(:quantity AS jsonb), :created_at)"
            ),
            {
                "tenant": tenant,
                "need": need_id,
                "category": category,
                "application": application,
                "quantity": quantity,
                "created_at": NOW,
            },
        )
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    reader = PostgresSourcingNeedReader(factory, tenant)

    snapshot = await reader.read(tenant, need_id)

    assert snapshot.product_category.value == "hinges"
    assert snapshot.quantity.value == 5000
    assert snapshot.application is not None
    assert snapshot.application.value == "marine door"
    assert snapshot.model is None
    assert snapshot.destination is None
    assert snapshot.product_category.provenance.source_id == "msg-confirmed"
    with pytest.raises(ValidationError, match="租户绑定"):
        await reader.read(other, need_id)


@pytest.mark.asyncio
async def test_composition_uses_real_services_and_registers_complete_events(
    integration_engine: AsyncEngine,
) -> None:
    tenant = TenantId(new_id("tn"))
    factory = async_sessionmaker(integration_engine, expire_on_commit=False)
    research, gateway, model = _research(tenant)
    settings = SourcingSettings(
        "sourcing-model-v1",
        "TAVILY_DEPLOYMENT_REFERENCE",
        4,
        12,
        "system:sourcing-runtime",
    )
    transport = _SearchTransport()
    pages = _PageTransport()
    artifacts = _Artifacts()
    playbook = _Playbook()
    secrets = _Secrets()
    production_chain = build_sourcing_research_chain(
        factory=factory,
        tenant_id=tenant,
        settings=settings,
        composition=SourcingResearchComposition(
            playbook=playbook,  # type: ignore[arg-type]
            search_transport=transport,  # type: ignore[arg-type]
            page_transport=pages,  # type: ignore[arg-type]
            artifacts=artifacts,  # type: ignore[arg-type]
            model_port=model,
        ),
        tool_user=UserId(new_id("usr")),
        fingerprints=HmacFingerprintProvider("v1", b"x" * 32),
        secret_resolver=secrets,
        country_policy=_CountryPolicy(),  # type: ignore[arg-type]
        lease_duration=timedelta(seconds=30),
        now=lambda: NOW,
    )
    composition = build_sourcing_case_composition(
        factory=factory,
        tenant_id=tenant,
        settings=settings,
        research=production_chain,
        opportunities=_Opportunities(),  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    assert isinstance(composition.sourcing, SourcingServiceImpl)
    assert isinstance(composition.products, ProductServiceImpl)
    assert isinstance(composition.suppliers, SupplierServiceImpl)
    assert isinstance(composition.costing, CostingServiceImpl)
    assert composition.sourcing_actor.scope is SourcingScope.SYSTEM
    assert composition.product_actor.role is ProductRole.SYSTEM
    assert composition.supplier_actor.role is SupplierRole.SYSTEM
    assert composition.costing_actor.scope is CostingScope.SYSTEM
    assert set(composition.handlers) == {
        "sourcing_case.v2.check_ladder",
        "sourcing_case.v2.await_public_plan",
        "sourcing_case.v2.public_search",
        "sourcing_case.v2.verify_candidates",
        "sourcing_case.v2.prepare_candidates",
        "sourcing_case.v2.await_product_cards",
        "sourcing_case.v2.await_review",
        "sourcing_case.v2.handoff_costing",
    }
    engine = _Engine()
    outbox = _Outbox()
    composition.register(engine, outbox)  # type: ignore[arg-type]
    assert [type(item).__name__ for item in engine.definitions] == [
        "WorkflowDefinition"
    ]
    assert outbox.events == [
        NeedValidated,
        NeedBecameSourcingReady,
        SourcingCandidatesVerified,
        SourcingCandidatesReady,
        SourcingCaseHandedToCosting,
    ]
    assert gateway.calls == 0
    assert model.calls == 0
    assert transport.calls == pages.calls == artifacts.calls == 0
    assert playbook.calls == secrets.calls == 0

    wrong_model = SimpleNamespace(**{**research.__dict__, "model_port": _Model()})
    wrong_model.model_port.model_identifier = "other-model"
    with pytest.raises(ValidationError, match="research-only"):
        build_sourcing_case_composition(
            factory=factory,
            tenant_id=tenant,
            settings=settings,
            research=wrong_model,  # type: ignore[arg-type]
            opportunities=_Opportunities(),  # type: ignore[arg-type]
            now=lambda: NOW,
        )


@pytest.mark.asyncio
async def test_run_sourcing_projection_requires_same_tenant_case_v2_binding(
    integration_engine: AsyncEngine,
) -> None:
    tenant = TenantId(new_id("tn"))
    other = TenantId(new_id("tn"))
    need_id = ValidatedNeedId(new_id("need"))
    case_id = SourcingCaseId(new_id("src"))
    run_id = RunId(new_id("run"))
    async with integration_engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(
            connection,
            expire_on_commit=False,
            join_transaction_mode="create_savepoint",
        )
        async with factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO validated_needs "
                    "(tenant_id, need_id, account_id, product_category, source_message_id, status, created_at) "
                    "VALUES (:tenant, :need, 'account-audit', CAST(:category AS jsonb), "
                    "'message-audit', 'sourcing_ready', :created_at)"
                ),
                {
                    "tenant": tenant,
                    "need": need_id,
                    "category": '{"value":"hinges"}',
                    "created_at": NOW,
                },
            )
            session.add(
                SourcingCaseRow(
                    tenant_id=tenant,
                    case_id=case_id,
                    need_id=need_id,
                    workflow_version=2,
                    trigger_key=f"sourcing-case:v2:{tenant}:{need_id}",
                    need_snapshot={"safe": True},
                    need_snapshot_hash="a" * 64,
                    state="opened",
                    sealed_candidate_ids=[],
                    stop_code="manual_stop",
                    stop_detail={"stage": "review", "observed_count": 0},
                    version=1,
                    opened_at=NOW,
                    state_changed_at=NOW,
                )
            )
            session.add(
                WorkflowRunRow(
                    tenant_id=tenant,
                    run_id=run_id,
                    workflow_type="sourcing_case",
                    workflow_version=2,
                    subject_ref=case_id,
                    current_step="await_review",
                    status="waiting_event",
                    context={"query_text": "must never be projected"},
                    idempotency_key=f"sourcing-runtime:{case_id}",
                )
            )
        repository = PostgresRunAuditRepository(factory)

        detail = await repository.get_run(tenant, run_id)

        assert detail is not None
        assert detail.summary.sourcing is not None
        assert detail.summary.sourcing.case_id == case_id
        assert detail.summary.sourcing.search_attempt_count == 0
        assert detail.summary.sourcing.page_attempt_count == 0
        assert detail.summary.sourcing.stop_reason is not None
        assert detail.summary.sourcing.stop_reason.code == "manual_stop"
        assert "must never be projected" not in detail.model_dump_json()
        assert await repository.get_run(other, run_id) is None
        await transaction.rollback()
