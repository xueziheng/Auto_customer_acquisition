"""采集模型授权绑定原任务、当前员工、租约和固定来源调用序号。"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from domains.prospecting.service import AcquisitionJob, InventoryQuery
from shared.schemas.identifiers import AcquisitionJobId, EmployeeId, TenantId, UserId
from shared.schemas.model_invocation import ModelGenerationError

NOW = datetime(2026, 10, 13, tzinfo=UTC)
TENANT, EMPLOYEE = TenantId("tn_test"), EmployeeId("emp_test")


def setup():
    from apps.scheduler_worker.inventory_model import InventoryModelAuthority

    job = AcquisitionJob(
        id=AcquisitionJobId("acq_01K39P9M5D6K4A91YEQ80EJZ0X"),
        tenant_id=TENANT,
        employee_id=EMPLOYEE,
        query=InventoryQuery(product_tags=("tricycle",)),
        requested_count=5,
        remaining_count=5,
        status="processing",
        attempts=1,
        lease_token="fence-1",
        lease_until=NOW + timedelta(minutes=8),
        created_at=NOW,
        updated_at=NOW,
    )
    reader = SimpleNamespace(get_job=AsyncMock(return_value=job))
    employee = SimpleNamespace(
        tenant_id=TENANT,
        employee_id=EMPLOYEE,
        user_id=UserId("usr_test"),
        is_active=True,
        role="sales",
    )
    service = SimpleNamespace(get_employee=AsyncMock(return_value=employee))

    @asynccontextmanager
    async def employees(tenant):
        assert tenant == TENANT
        yield service

    config = SimpleNamespace(authorize=AsyncMock())
    authority = InventoryModelAuthority(
        job=job,
        source_id="company_websites",
        configuration_version="model-v1",
        backend=reader,
        employees=employees,
        configuration=config,
        now=lambda: NOW,
    )
    return authority, job, reader, employee, config


@pytest.mark.asyncio
async def test_identity_is_job_bound_stable_per_source():
    authority, job, _reader, _employee, config = setup()
    first = await authority.identity()
    assert first == await authority.identity()
    assert first.capability == "inventory_discovery" and first.run_id == job.id
    await authority.check(first)
    assert config.authorize.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"tenant_id": "tn_other"},
        {"employee_id": "emp_other"},
        {"user_id": "usr_other"},
        {"run_id": "run_other"},
        {"sequence": 999},
        {"capability": "research"},
        {"configuration_version": "old-model"},
        {"turn_id": "turn_other"},
    ],
)
async def test_untrusted_identity_cannot_enter_model(change):
    authority, _, _, _, config = setup()
    identity = (await authority.identity()).model_copy(update=change)
    with pytest.raises(ModelGenerationError):
        await authority.check(identity)
    config.authorize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"status": "complete"},
        {"lease_token": "new-fence"},
        {"lease_until": NOW},
        {"employee_id": EmployeeId("emp_other")},
    ],
)
async def test_stale_job_lease_cannot_dispatch_or_deliver_model(change):
    authority, job, reader, _, config = setup()
    identity = await authority.identity()
    reader.get_job.return_value = job.model_copy(update=change)
    with pytest.raises(ModelGenerationError):
        await authority.check(identity)
    config.authorize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [("is_active", False), ("role", "viewer"), ("user_id", "usr_reassigned")],
)
async def test_current_employee_revocation_rejects_model(field, value):
    authority, _, _, employee, config = setup()
    identity = await authority.identity()
    setattr(employee, field, value)
    with pytest.raises(ModelGenerationError):
        await authority.check(identity)
    config.authorize.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["intent", "label", "payload", "context", "evidence"])
@pytest.mark.parametrize("marker", ["password=placeholder", "token=SYNTHETIC_PLACEHOLDER", "sk-" + "X" * 24, "tvly-" + "X" * 24, None])
async def test_actual_model_dispatch_guards_complete_search_and_fit_projection(monkeypatch, field, marker):
    from apps.scheduler_worker import inventory_model as module
    from domains.prospecting.inventory_fit import (
        InventoryFitBatch,
        fit_query_fingerprint,
    )
    from tests.unit.test_prospect_inventory import candidate

    _, job, reader, _, _ = setup()
    value = marker or "普通经销商目标"
    query = InventoryQuery(product_tags=(value if field == "label" else "hinges",),
        intent=value if field == "intent" else "正常目标",
        confirmed_refs=({"document_id": "doc", "revision_id": "rev"},))
    # 旧请求序列化后重新加载，输入护栏不能依赖API曾经检查。
    job = AcquisitionJob.model_validate_json(job.model_copy(update={"query": query, "attempts": 2}).model_dump_json())
    reader.get_job.return_value = job
    identity = SimpleNamespace()
    monkeypatch.setattr(module, "InventoryModelAuthority", lambda **_: SimpleNamespace(identity=AsyncMock(return_value=identity), check=AsyncMock()))
    settings = SimpleNamespace(configuration_version="v1", api_key_file="never-read", codex_job_root="never-created",
        model="synthetic", limits=SimpleNamespace(timeout_seconds=1, max_output_tokens=100))
    monkeypatch.setattr(module, "resolve_task_model", lambda *_: settings)
    generator = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(text='{"candidates": []}')))
    composition = SimpleNamespace(generator=generator, aclose=AsyncMock())
    monkeypatch.setattr(module, "build_model_composition", lambda **_: composition)
    model = module.InventoryDiscoveryModel(tenant_id=TENANT, backend=reader, employees=None, configuration=None,
        settings=settings, resolver=None, sessions=None, fingerprints=None, lease_owner="test",
        context_reader=SimpleNamespace(read=AsyncMock(return_value=[{"facts": {"name": value if field == "context" else "steel"}}])))

    async def dispatch():
        if field == "evidence":
            c = candidate(evidence_text=value)
            batch = InventoryFitBatch(sequence=0, query_fingerprint=fit_query_fingerprint(query),
                candidates=({"candidate_id": c.id, "evidence_version": module.candidate_evidence_version(c)},), state="started")
            generator.generate.return_value.text = __import__("json").dumps({"assessments": [{"candidate_id": c.id,
                "matched": False, "evidence_quote": value, "inference": "推断证据不足"}]})
            return await model.evaluate(job, batch, (c,), AsyncMock())
        return await model.generate(job, "company_websites", None, {"hint": value if field == "payload" else "normal"})

    if marker:
        with pytest.raises(ModelGenerationError):
            await dispatch()
        generator.generate.assert_not_awaited()
    else:
        await dispatch()
        generator.generate.assert_awaited_once()
