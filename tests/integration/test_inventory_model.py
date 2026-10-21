"""候选模型实际通过Gateway与PG账本；只替换外部模型端口。"""

from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from apps.scheduler_worker.inventory_model import InventoryDiscoveryModel
from connectors.codex_agent.discovery import DiscoverySession
from domains.prospecting.service import InventoryQuery
from infra.db.prospect_inventory import PostgresInventoryService, PostgresPublicCatalog
from infra.db.tables import ModelInvocationRow, ToolCallRow
from infra.standalone.settings import StandaloneModelSettings
from shared.schemas.identifiers import EmployeeId, TenantId, UserId, new_id
from shared.schemas.model_invocation import (
    ModelGenerationError,
    ModelLimits,
    ModelResponse,
    ModelUsage,
)
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414
)
from tool_gateway.fingerprint import HmacFingerprintProvider


@pytest.mark.parametrize(
    "response_text,valid,shape",
    [
        ('{"candidates":[]}', True, "valid"),
        ('{"invented":"private-invalid-result"}', False, "invalid_top_keys"),
        ("[]", False, "not_object"),
        ('{"candidates":null}', False, "candidates_not_list"),
        ("PRIVATE", False, "invalid_json"),
        (
            '{"candidates":[' + ",".join(["null"] * 101) + "]}",
            False,
            "candidates_over_limit",
        ),
    ],
)
@pytest.mark.parametrize("with_context", [False, True])
async def test_inventory_model_uses_job_identity_and_persistent_gateway(
    unit_engine, monkeypatch, response_text, valid, shape, with_context
):
    tenant, control = TenantId(new_id("tn")), TenantId(new_id("tn"))
    employee = EmployeeId(new_id("emp"))
    sessions = async_sessionmaker(unit_engine, expire_on_commit=False)
    catalog = PostgresPublicCatalog(sessions, control)
    backend = PostgresInventoryService(sessions, tenant, catalog)
    context_reader = None
    refs = ()
    if with_context:
        from domains.products.schemas import KnowledgeActor
        from domains.products.service import EnterpriseKnowledgeServiceImpl
        from tests.unit.test_enterprise_knowledge import (
            ANALYSIS,
            SOURCE,
            TEXT,
            MemoryRepo,
            MemoryUow,
        )
        from workflows.enterprise_knowledge.confirmed_context import (
            ConfirmedContextReader,
        )

        repo = MemoryRepo()
        repo.roles[employee] = ("boss", "usr_fixture")
        knowledge = EnterpriseKnowledgeServiceImpl(lambda tenant: MemoryUow(repo))
        actor_knowledge = KnowledgeActor(tenant_id=tenant, employee_id=employee)
        doc = await knowledge.register_upload(
            tenant, actor_knowledge, SOURCE, idempotency_key="synthetic"
        )
        claim = await knowledge.claim_next(tenant, lease_owner="synthetic")
        detail = await knowledge.complete_processing(
            tenant,
            claim,
            source_text=TEXT,
            analysis=ANALYSIS,
            model="synthetic",
            extracted_by="synthetic",
        )
        await knowledge.confirm(
            tenant,
            actor_knowledge,
            doc.document_id,
            revision_id=detail.revision.revision_id,
            expected_version=detail.document.version,
        )
        refs = (
            {
                "document_id": doc.document_id,
                "revision_id": detail.revision.revision_id,
            },
        )
        context_reader = ConfirmedContextReader(knowledge)
    original_fresh = catalog.fresh

    async def public_fresh(query, now):
        assert query is None or (query.intent is None and not query.confirmed_refs)
        return await original_fresh(query, now)

    catalog.fresh = public_fresh
    await backend.request(
        tenant,
        employee,
        InventoryQuery(
            product_tags=("private-query",),
            intent="内部目标" if with_context else None,
            confirmed_refs=refs,
        ),
        1,
        "inventory-model-request",
    )
    job = await backend.claim_job(tenant, timedelta(minutes=10))
    assert job is not None
    actor = SimpleNamespace(
        tenant_id=tenant,
        employee_id=employee,
        user_id=UserId(new_id("usr")),
        is_active=True,
        role="sales",
    )

    @asynccontextmanager
    async def employees(current_tenant):
        assert current_tenant == tenant
        yield SimpleNamespace(get_employee=AsyncMock(return_value=actor))

    configuration = SimpleNamespace(authorize=AsyncMock())
    settings = StandaloneModelSettings(
        provider="deepseek",
        model="test-model",
        configuration_version="inventory-model-v1",
        secret_ref="SYNTHETIC_MODEL",
        api_control_tenant=str(control),
        api_key_file=Path("/synthetic/unused/key"),
        codex_job_root=Path("/synthetic/unused/jobs"),
        model_data_export_enabled=True,
        limits=ModelLimits(
            window_seconds=86400,
            tenant_calls=3,
            employee_calls=3,
            tenant_concurrency=1,
            employee_concurrency=1,
            max_input_bytes=65536,
            max_output_tokens=8192,
            timeout_seconds=20,
        ),
    )

    class Resolver:
        def resolve(self, ref):
            raise AssertionError("本测试不得解析凭证")

    class Provider:
        calls = 0

        async def generate(self, request):
            self.calls += 1
            assert request.payload["query"] == job.query.model_dump(mode="json")
            assert request.payload["allowed_source"] == "company_websites"
            if with_context:
                assert (
                    request.payload["internal_confirmed_context"][0]["facts"][0][
                        "value"
                    ]
                    == "stainless steel"
                )
                assert request.payload["internal_confirmed_context"][0][
                    "fact_provenance"
                ]
            else:
                assert request.payload["internal_confirmed_context"] == []
            return ModelResponse(
                text=response_text,
                model=request.model,
                usage=ModelUsage(
                    input_tokens=20, cached_input_tokens=0, output_tokens=10
                ),
            )

        async def aclose(self):
            pass

    provider = Provider()

    async def tools(name, arguments):
        raise AssertionError("本测试不做外部网页调用")

    async with DiscoverySession(
        tools, max_requests=1, timeout_seconds=20, max_result_bytes=1024
    ) as session:

        def configured(selected, identity, *, discovery):
            assert discovery is session and identity.run_id == job.id
            return provider

        monkeypatch.setattr(
            "apps.scheduler_worker.inventory_model.configured_codex_provider",
            configured,
        )
        model = InventoryDiscoveryModel(
            tenant_id=tenant,
            backend=backend,
            employees=employees,
            configuration=configuration,
            settings=settings,
            resolver=Resolver(),
            sessions=sessions,
            fingerprints=HmacFingerprintProvider("v1", b"k" * 32),
            lease_owner="inventory-model-test",
            context_reader=context_reader,
        )
        if valid:
            assert await model.generate(
                job, "company_websites", session, {"query": "spoof"}
            ) == {"candidates": []}
        else:
            with pytest.raises(ModelGenerationError) as failure:
                await model.generate(
                    job, "company_websites", session, {"query": "spoof"}
                )
            assert failure.value.code == "invalid_response"
            assert failure.value.summary.input_shape == shape
            assert (
                "private-invalid-result" not in failure.value.summary.model_dump_json()
            )
        with pytest.raises(ModelGenerationError):
            await model.generate(job, "company_websites", session, {"query": "spoof"})
    assert provider.calls == 1
    async with sessions() as db:
        rows = (
            await db.scalars(
                select(ModelInvocationRow).where(ModelInvocationRow.tenant_id == tenant)
            )
        ).all()
        assert (
            len(rows) == 1
            and rows[0].capability == "inventory_discovery"
            and rows[0].state == "succeeded"
        )
        assert rows[0].input_tokens == 20 and rows[0].output_tokens == 10
        records = (
            await db.scalars(select(ToolCallRow).where(ToolCallRow.tenant_id == tenant))
        ).all()
        assert records
        safe = str(
            [
                {c.name: getattr(r, c.name) for c in r.__table__.columns}
                for r in [*rows, *records]
            ]
        )
        assert "private-query" not in safe and "private-invalid-result" not in safe
