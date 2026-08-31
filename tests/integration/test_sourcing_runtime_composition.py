"""Task 13 production composition 的受控 PostgreSQL 与零网络验证。"""

from __future__ import annotations

import importlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from apps.scheduler_worker.config import SourcingSettings
from apps.scheduler_worker.sourcing_runtime import (
    PostgresCandidateEvidenceSnapshotReader,
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
from infra.db.tables import (
    ProductRow,
    SearchQuotaAccountRow,
    SearchQuotaReservationRow,
    SourcingCandidateRow,
    SourcingCaseRow,
    SourcingPageAttemptRow,
    SourcingPublicPlanRow,
    SourcingReviewRow,
    SourcingSupplyOptionRow,
    ToolCallRow,
    WorkflowRunRow,
)
from shared.errors import ValidationError
from shared.events.catalog import (
    EvidenceLevel,
    NeedBecameSourcingReady,
    NeedValidated,
    SourcingCandidatesReady,
    SourcingCandidatesVerified,
    SourcingCaseHandedToCosting,
)
from shared.schemas.identifiers import (
    ArtifactId,
    OpportunityId,
    RunId,
    SourcingCaseId,
    SourcingPlanId,
    SourcingReviewId,
    SourcingSupplyOptionId,
    SupplierCandidateId,
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


async def _seed_candidate_evidence_bindings(
    connection: object,
    *,
    tenant: TenantId,
    index: int,
    artifact_kind: str = "web_snapshot",
    artifact_mime_type: str = "text/html",
    bindings: tuple[tuple[str, str, datetime], ...] | None = None,
) -> ArtifactId:
    """以真实 PG 约束写入候选证据；每个 binding 都是独立候选来源。"""

    artifact_id = ArtifactId(new_id("art"))
    case_id = SourcingCaseId(new_id("src"))
    need_id = ValidatedNeedId(new_id("need"))
    content_hash = f"{index:064x}"
    sources = bindings or (("https://evidence.example/items/hinge", content_hash, NOW),)
    execute = connection.execute  # type: ignore[attr-defined]
    await execute(
        text(
            "INSERT INTO validated_needs "
            "(tenant_id, need_id, account_id, product_category, source_message_id, status, created_at) "
            "VALUES (:tenant, :need, 'account-evidence', CAST(:category AS jsonb), "
            "'message-evidence', 'sourcing_ready', :created_at)"
        ),
        {
            "tenant": str(tenant),
            "need": str(need_id),
            "category": '{"value":"hinges"}',
            "created_at": NOW,
        },
    )
    await execute(
        text(
            "INSERT INTO sourcing_cases "
            "(tenant_id, case_id, need_id, workflow_version, trigger_key, need_snapshot, "
            "need_snapshot_hash, state, sealed_candidate_ids, version, opened_at, state_changed_at) "
            "VALUES (:tenant, :case, :need, 2, :trigger, CAST(:snapshot AS jsonb), "
            ":snapshot_hash, 'opened', CAST('[]' AS jsonb), 1, :now, :now)"
        ),
        {
            "tenant": str(tenant),
            "case": str(case_id),
            "need": str(need_id),
            "trigger": f"candidate-evidence:{case_id}",
            "snapshot": "{}",
            "snapshot_hash": "a" * 64,
            "now": NOW,
        },
    )
    await execute(
        text(
            "INSERT INTO raw_artifacts "
            "(tenant_id, artifact_id, kind, content_hash, size_bytes, mime_type, object_key, uploaded_at) "
            "VALUES (:tenant, :artifact, :kind, :content_hash, 1, :mime_type, :object_key, :now)"
        ),
        {
            "tenant": str(tenant),
            "artifact": str(artifact_id),
            "kind": artifact_kind,
            "content_hash": content_hash,
            "mime_type": artifact_mime_type,
            "object_key": f"raw/{tenant}/{artifact_id}",
            "now": NOW,
        },
    )
    tiers = json.dumps(
        [
            {
                "minimum_quantity": 1,
                "amount": "1.00",
                "currency": "USD",
                "unit": "piece",
                "provenance": {},
                "evidence_ref": str(artifact_id),
            }
        ]
    )
    for source_index, (url, bound_hash, observed_at) in enumerate(sources):
        candidate_id = new_id("sc")
        await execute(
            text(
                "INSERT INTO sourcing_candidates "
                "(tenant_id, candidate_id, case_id, supplier_name, product_title, observed_facts, "
                "supplier_claims, match_inferences, verified_specs, indicative_price_tiers, "
                "rejected, rejection_reasons, created_at) "
                "VALUES (:tenant, :candidate, :case, :supplier, 'hinge', CAST('{}' AS jsonb), "
                "CAST('{}' AS jsonb), CAST('{}' AS jsonb), CAST('[]' AS jsonb), "
                "CAST(:tiers AS jsonb), false, CAST('[]' AS jsonb), :now)"
            ),
            {
                "tenant": str(tenant),
                "candidate": candidate_id,
                "case": str(case_id),
                "supplier": f"supplier-{index}-{source_index}",
                "tiers": tiers,
                "now": NOW,
            },
        )
        await execute(
            text(
                "INSERT INTO sourcing_candidate_evidence "
                "(tenant_id, candidate_id, artifact_id, url, observed_at, content_hash) "
                "VALUES (:tenant, :candidate, :artifact, :url, :observed_at, :content_hash)"
            ),
            {
                "tenant": str(tenant),
                "candidate": candidate_id,
                "artifact": str(artifact_id),
                "url": url,
                "observed_at": observed_at,
                "content_hash": bound_hash,
            },
        )
    return artifact_id


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
async def test_candidate_evidence_reader_uses_canonical_gateway_url_and_rejects_real_pg_binding_drift(
    integration_engine: AsyncEngine,
) -> None:
    """P63/P66：URL、tenant、kind、hash、time 与来源均由真实持久化绑定复核。"""

    tenant = TenantId(new_id("tn"))
    other = TenantId(new_id("tn"))
    canonical_hash = f"{1:064x}"
    async with integration_engine.begin() as connection:
        valid = await _seed_candidate_evidence_bindings(
            connection,
            tenant=tenant,
            index=1,
        )
        wrong_kind = await _seed_candidate_evidence_bindings(
            connection,
            tenant=tenant,
            index=2,
            artifact_kind="pdf",
            artifact_mime_type="application/pdf",
        )
        wrong_hash = await _seed_candidate_evidence_bindings(
            connection,
            tenant=tenant,
            index=3,
            bindings=(("https://evidence.example/items/hinge", "f" * 64, NOW),),
        )
        default_port = await _seed_candidate_evidence_bindings(
            connection,
            tenant=tenant,
            index=4,
            bindings=(("https://evidence.example:443/items/hinge", f"{4:064x}", NOW),),
        )
        time_drift = await _seed_candidate_evidence_bindings(
            connection,
            tenant=tenant,
            index=5,
            bindings=(
                ("https://evidence.example/items/hinge", f"{5:064x}", NOW),
                (
                    "https://evidence.example/items/hinge",
                    f"{5:064x}",
                    NOW + timedelta(seconds=1),
                ),
            ),
        )
        source_drift = await _seed_candidate_evidence_bindings(
            connection,
            tenant=tenant,
            index=6,
            bindings=(
                ("https://evidence.example/items/hinge", f"{6:064x}", NOW),
                ("https://other-evidence.example/items/hinge", f"{6:064x}", NOW),
            ),
        )
    reader = PostgresCandidateEvidenceSnapshotReader(
        async_sessionmaker(integration_engine, expire_on_commit=False), tenant
    )

    snapshot = await reader.read_verified(tenant, valid)

    assert snapshot.canonical_url == "https://evidence.example/items/hinge"
    assert snapshot.content_hash == canonical_hash
    with pytest.raises(ValidationError, match="租户绑定"):
        await reader.read_verified(other, valid)
    for artifact_id in (
        wrong_kind,
        wrong_hash,
        default_port,
        time_drift,
        source_drift,
    ):
        with pytest.raises(ValidationError, match="候选网页证据不可验证"):
            await reader.read_verified(tenant, artifact_id)


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
async def test_scheduler_runtime_factory_skips_sourcing_construction_only_when_absent_or_exactly_disabled(
    db_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P66：真实 root 的缺失/严格 disabled 分支均不得碰寻源组合。"""

    from tests.integration.test_scheduler_worker import (
        _factory_dependencies,
        _factory_environ,
        _FactoryHealthServer,
        _FactoryResolver,
    )

    runtime_module = importlib.import_module("apps.scheduler_worker.runtime")
    tenant = TenantId(new_id("tn"))
    dependencies = _factory_dependencies(runtime_module, with_hunter=False)
    calls: list[str] = []

    def forbidden(*args: object, **kwargs: object) -> object:
        del args, kwargs
        calls.append("sourcing")
        raise AssertionError("disabled sourcing must not be constructed")

    monkeypatch.setattr(runtime_module, "build_sourcing_research_chain", forbidden)
    monkeypatch.setattr(runtime_module, "build_sourcing_case_composition", forbidden)
    absent = runtime_module.SchedulerRuntimeFactory(
        _factory_environ(db_url, tenant, hunter_enabled=False),
        dependencies,
        resolver_factory=_FactoryResolver,
        health_server_factory=_FactoryHealthServer,
    )
    async with absent() as runtime:
        assert "sourcing_case.v2.check_ladder" not in runtime.workflow._handlers

    disabled_environ = _factory_environ(db_url, tenant, hunter_enabled=False)
    disabled_environ["TRADEOS_SOURCING_SETTINGS_JSON"] = '{"enabled": false}'
    disabled = runtime_module.SchedulerRuntimeFactory(
        disabled_environ,
        replace(
            dependencies,
            sourcing_case=SourcingResearchComposition(
                playbook=_Playbook(),  # type: ignore[arg-type]
                search_transport=_SearchTransport(),  # type: ignore[arg-type]
                page_transport=_PageTransport(),  # type: ignore[arg-type]
                artifacts=_Artifacts(),  # type: ignore[arg-type]
                model_port=_Model(),
            ),
        ),
        resolver_factory=_FactoryResolver,
        health_server_factory=_FactoryHealthServer,
    )
    async with disabled() as runtime:
        assert "sourcing_case.v2.check_ladder" not in runtime.workflow._handlers
    assert calls == []


@pytest.mark.asyncio
async def test_scheduler_runtime_factory_enabled_root_binds_typed_model_and_all_sourcing_events(
    db_url: str,
) -> None:
    """P66：实际 root 只在 enabled 时装配完整五事件路径，构造期零网络/零模型调用。"""

    from tests.integration.test_scheduler_worker import (
        _factory_dependencies,
        _factory_environ,
        _FactoryHealthServer,
        _FactoryResolver,
    )

    runtime_module = importlib.import_module("apps.scheduler_worker.runtime")
    tenant = TenantId(new_id("tn"))
    model = _Model()
    transport = _SearchTransport()
    pages = _PageTransport()
    artifacts = _Artifacts()
    playbook = _Playbook()
    dependencies = replace(
        _factory_dependencies(runtime_module, with_hunter=False),
        sourcing_case=SourcingResearchComposition(
            playbook=playbook,  # type: ignore[arg-type]
            search_transport=transport,  # type: ignore[arg-type]
            page_transport=pages,  # type: ignore[arg-type]
            artifacts=artifacts,  # type: ignore[arg-type]
            model_port=model,
        ),
    )
    environ = _factory_environ(db_url, tenant, hunter_enabled=False)
    environ["TRADEOS_SOURCING_SETTINGS_JSON"] = json.dumps(
        {
            "enabled": True,
            "model_identifier": model.model_identifier,
            "tavily_secret_ref": "TAVILY_DEPLOYMENT_REFERENCE",
            "max_search_queries_per_plan": 4,
            "max_pages_per_plan": 12,
            "system_actor_id": "system:sourcing-runtime",
        }
    )
    factory = runtime_module.SchedulerRuntimeFactory(
        environ,
        dependencies,
        resolver_factory=_FactoryResolver,
        health_server_factory=_FactoryHealthServer,
        now=lambda: NOW,
    )

    async with factory() as runtime:
        assert "sourcing_case.v2.check_ladder" in runtime.workflow._handlers
        event_types = {
            "NeedValidated",
            "NeedBecameSourcingReady",
            "SourcingCandidatesVerified",
            "SourcingCandidatesReady",
            "SourcingCaseHandedToCosting",
        }
        assert set(runtime.outbox._handlers).issuperset(event_types)

        class _DurableRecorder:
            def __init__(self) -> None:
                self.events: list[str] = []

            async def handle(self, event: object) -> None:
                self.events.append(type(event).__name__)

        recorder = _DurableRecorder()
        # Root 的生产注册已在上面断言；下面把业务副作用替换为受控 terminal
        # handler，只验证五个契约经真正的 PG Outbox 反序列化、delivery 持久化与收敛。
        runtime.outbox._handlers = {
            name: [(f"controlled.{name}", recorder)] for name in event_types
        }
        evidence_level = EvidenceLevel.CUSTOMER_QUANTITY_AND_TIMING
        case_id = SourcingCaseId(new_id("src"))
        need_id = ValidatedNeedId(new_id("need"))
        candidate_id = SupplierCandidateId(new_id("sc"))
        option_id = SourcingSupplyOptionId(new_id("sop"))
        events = (
            NeedValidated(
                tenant_id=tenant,
                occurred_at=NOW,
                need_id=need_id,
                category="hinges",
                evidence_level=evidence_level,
                completeness=3,
            ),
            NeedBecameSourcingReady(
                tenant_id=tenant,
                occurred_at=NOW,
                need_id=need_id,
                completeness=3,
            ),
            SourcingCandidatesVerified(
                tenant_id=tenant,
                occurred_at=NOW,
                case_id=case_id,
                candidate_ids=(candidate_id,),
                case_version=1,
                candidate_set_hash="a" * 64,
            ),
            SourcingCandidatesReady(
                tenant_id=tenant,
                occurred_at=NOW,
                case_id=case_id,
                option_ids=(option_id,),
                candidate_ids=(candidate_id,),
            ),
            SourcingCaseHandedToCosting(
                tenant_id=tenant,
                occurred_at=NOW,
                case_id=case_id,
                need_id=need_id,
                opportunity_id=OpportunityId(new_id("opp")),
                review_id=SourcingReviewId(new_id("srw")),
            ),
        )
        from infra.db.outbox import PostgresEventBus

        session = runtime.outbox._factory()
        try:
            bus = PostgresEventBus(session, tenant, now=lambda: NOW)
            for event in events:
                await bus.publish(event)
            await session.commit()
        finally:
            await session.close()

        assert await runtime.outbox.drain() == 5
        assert set(recorder.events) == event_types
        async with runtime.outbox._factory() as session:
            states = (
                await session.execute(
                    text(
                        "SELECT event_type, status FROM outbox_events "
                        "WHERE tenant_id=:tenant AND event_type = ANY(:types)"
                    ),
                    {"tenant": str(tenant), "types": list(event_types)},
                )
            ).all()
        assert set(states) == {(name, "delivered") for name in event_types}
    assert model.calls == transport.calls == pages.calls == artifacts.calls == 0
    assert playbook.calls == 0


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
            await session.flush()
            plan_id = SourcingPlanId(new_id("spl"))
            plan_hash = "b" * 64
            session.add(
                SourcingPublicPlanRow(
                    tenant_id=tenant,
                    plan_id=plan_id,
                    case_id=case_id,
                    target_countries=["US"],
                    product_category="hinges",
                    queries=[
                        {
                            "query_text": "hinge manufacturer",
                            "target_country": "US",
                        }
                    ],
                    max_search_queries=1,
                    max_pages_read=3,
                    provider="tavily",
                    search_depth="basic",
                    usage_credits_remaining=3,
                    worst_case_credits=1,
                    version=1,
                    expected_case_version=1,
                    plan_hash=plan_hash,
                    status="authorized",
                    confirmed_by="system-audit",
                    confirmed_at=NOW,
                    authorized_plan_hash=plan_hash,
                    created_at=NOW,
                )
            )
            await session.flush()
            case = await session.get(
                SourcingCaseRow, {"tenant_id": tenant, "case_id": case_id}
            )
            assert case is not None
            case.active_search_plan_id = plan_id
            candidate_id = new_id("sc")
            session.add(
                SourcingCandidateRow(
                    tenant_id=tenant,
                    candidate_id=candidate_id,
                    case_id=case_id,
                    supplier_name="Audit Factory",
                    source_platform=None,
                    product_title="Stainless hinge",
                    observed_facts={},
                    supplier_claims={},
                    match_inferences={},
                    verified_specs=[],
                    indicative_price_tiers=[
                        {
                            "minimum_quantity": 1,
                            "amount": "1.00",
                            "currency": "USD",
                            "unit": "piece",
                            "provenance": {},
                            "evidence_ref": "audit-evidence",
                        }
                    ],
                    moq=None,
                    price_unit=None,
                    currency=None,
                    match_explanation={},
                    rejected=False,
                    rejection_reasons=[],
                    verified_by=None,
                    public_draft_source_key=None,
                    created_at=NOW,
                )
            )
            product_id = new_id("prd")
            session.add(
                ProductRow(
                    tenant_id=tenant,
                    product_id=product_id,
                    pool="candidate",
                    candidate_status="source_only",
                    name_zh="审计合页",
                    name_en="Audit hinge",
                    category="hinges",
                    normalized_category="hinges",
                    spec_summary=None,
                    moq=None,
                    lead_time_days_min=None,
                    lead_time_days_max=None,
                    supplier_id=None,
                    internal_cost_amount=None,
                    internal_cost_currency=None,
                    internal_cost_basis=None,
                    internal_cost_unit=None,
                    internal_cost_source_ref=None,
                    allowed_price_min_amount=None,
                    allowed_price_min_currency=None,
                    allowed_price_max_amount=None,
                    allowed_price_max_currency=None,
                    sellable_markets=[],
                    customizable=False,
                    selling_points=[],
                    known_issues=[],
                    created_at=NOW,
                )
            )
            await session.flush()
            option_id = new_id("sop")
            alternate_option_id = new_id("sop")
            session.add(
                SourcingSupplyOptionRow(
                    tenant_id=tenant,
                    option_id=option_id,
                    case_id=case_id,
                    source="supplier_candidate",
                    product_id=product_id,
                    supplier_candidate_id=candidate_id,
                    is_qualified=True,
                    created_at=NOW,
                )
            )
            session.add(
                SourcingSupplyOptionRow(
                    tenant_id=tenant,
                    option_id=alternate_option_id,
                    case_id=case_id,
                    source="existing_product",
                    product_id=product_id,
                    supplier_candidate_id=None,
                    is_qualified=True,
                    created_at=NOW,
                )
            )
            await session.flush()
            session.add(
                SourcingReviewRow(
                    tenant_id=tenant,
                    review_id=new_id("srw"),
                    case_id=case_id,
                    primary_option_id=option_id,
                    primary_selection={"source": "audit"},
                    alternate_option_ids=[alternate_option_id],
                    reason="audit review",
                    expected_case_version=1,
                    submitted_by="system-audit",
                    submitted_at=NOW,
                    confirmed_by=None,
                    confirmed_at=None,
                )
            )
            session.add_all(
                [
                    ToolCallRow(
                        tenant_id=tenant,
                        tool_call_id=new_id("tc"),
                        tool_id="web.search",
                        tool_version="v1",
                        risk_level="low",
                        cost_class="free",
                        idempotency_key="sourcing-audit-search-1",
                        request_fingerprint="c" * 64,
                        fingerprint_version="v1",
                        status="succeeded",
                        duplicate_of=None,
                        lease_owner=None,
                        lease_expires_at=None,
                        attempt_count=1,
                        run_id=run_id,
                        user_id="system-audit",
                        campaign_id=None,
                        message_attempt_id=None,
                        provider_ref="search-audit-1",
                        error_category=None,
                        retry_after_at=None,
                        created_at=NOW,
                        updated_at=NOW,
                        completed_at=NOW,
                    ),
                    ToolCallRow(
                        tenant_id=tenant,
                        tool_call_id=new_id("tc"),
                        tool_id="web.search",
                        tool_version="v1",
                        risk_level="low",
                        cost_class="free",
                        idempotency_key="sourcing-audit-search-2",
                        request_fingerprint="d" * 64,
                        fingerprint_version="v1",
                        status="succeeded",
                        duplicate_of=None,
                        lease_owner=None,
                        lease_expires_at=None,
                        attempt_count=1,
                        run_id=run_id,
                        user_id="system-audit",
                        campaign_id=None,
                        message_attempt_id=None,
                        provider_ref="search-audit-2",
                        error_category=None,
                        retry_after_at=None,
                        created_at=NOW,
                        updated_at=NOW,
                        completed_at=NOW,
                    ),
                ]
            )
            session.add_all(
                [
                    SourcingPageAttemptRow(
                        tenant_id=tenant,
                        case_id=case_id,
                        plan_id=plan_id,
                        run_id=run_id,
                        plan_hash=plan_hash,
                        query_index=0,
                        result_index=index,
                        status="claimed",
                        outcome=None,
                        draft_id=None,
                        attempted_at=NOW,
                        completed_at=None,
                    )
                    for index in range(3)
                ]
            )
            session.add(
                SearchQuotaAccountRow(
                    tenant_id=tenant,
                    provider="tavily",
                    ceiling=10,
                    reservations=3,
                    cost_status="free",
                    usage_limit=10,
                    usage_used=3,
                    paygo_enabled=False,
                    checked_at=NOW,
                )
            )
            session.add_all(
                [
                    SearchQuotaReservationRow(
                        tenant_id=tenant,
                        provider="tavily",
                        run_id=run_id,
                        request_key=character * 64,
                        status=status,
                        created_at=NOW,
                        updated_at=NOW,
                    )
                    for character, status in (
                        ("e", "consumed"),
                        ("f", "reserved"),
                        ("1", "uncertain"),
                    )
                ]
            )
        repository = PostgresRunAuditRepository(factory)

        detail = await repository.get_run(tenant, run_id)

        assert detail is not None
        assert detail.summary.sourcing is not None
        assert detail.summary.sourcing.case_id == case_id
        assert detail.summary.sourcing.plan_status == "authorized"
        assert detail.summary.sourcing.search_attempt_count == 2
        assert detail.summary.sourcing.page_attempt_count == 3
        assert detail.summary.sourcing.candidate_count == 1
        assert detail.summary.sourcing.primary_count == 1
        assert detail.summary.sourcing.alternate_count == 1
        assert detail.summary.sourcing.consumed_credits == 1
        assert detail.summary.sourcing.reserved_credits == 1
        assert detail.summary.sourcing.uncertain_credits == 1
        assert detail.summary.sourcing.stop_reason is not None
        assert detail.summary.sourcing.stop_reason.code == "manual_stop"
        assert "must never be projected" not in detail.model_dump_json()
        assert await repository.get_run(other, run_id) is None
        await transaction.rollback()
