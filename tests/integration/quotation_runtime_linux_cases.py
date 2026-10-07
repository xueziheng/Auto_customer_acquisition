"""B2专用真实API/worker→PG→受限parser→Gateway→PDF链，SDK网络受控。"""

import asyncio
import json
import sys
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from apps.scheduler_worker import runtime as worker
from apps.scheduler_worker.main import WorkerStartStatus, run_scheduler_worker
from domains.costing.service import cost_item_type_values
from infra.db.tables import ApprovalPackageRow, EmployeeRow, QuotationStateEventRow
from tests.integration.test_need_units import (
    unit_engine as unit_engine,  # noqa: PLC0414 - pytest fixture
)
from tests.integration.test_quote_runtime import (
    actual_api_case,
    runtime_request,
    seed_runtime_facts,
    worker_environment,
)
from tests.integration.test_scheduler_worker import (
    _factory_dependencies,
    _FactoryHealthServer,
    _FactoryResolver,
    _TrackingEnvironmentSecrets,
)


async def locate(case, source, scope, profile, page, text):
    preview = await runtime_request(
        case,
        "POST",
        "/evidence/preview",
        {
            "operation": "preview",
            "source_ref": source,
            "scope": scope,
            "profile": profile,
            "page": page,
        },
    )
    start = preview["text"].index(text)
    located = await runtime_request(
        case,
        "POST",
        "/evidence/locator",
        {
            "operation": "locate",
            "source_ref": source,
            "scope": scope,
            "profile": profile,
            "page": page,
            "start": start,
            "end": start + len(text),
            "expected_raw_hash": preview["raw_hash"],
            "expected_text_hash": preview["text_hash"],
        },
    )
    assert located["excerpt"] == text
    return located


async def prepare_quote(case):
    get = lambda path, **kwargs: runtime_request(case, "GET", path, **kwargs)
    post = lambda path, body, **kwargs: runtime_request(
        case, "POST", path, body, **kwargs
    )
    unit_path = f"/needs/{case.need}/unit"
    context_path = f"/opportunities/{case.opportunity}/quote-context"
    first = await get(context_path)
    assert {item["field"] for item in first["blockers"]} == {"unit", "issuer"}
    before = len(case.objects.calls)
    await get(unit_path, actor="finance-reader", expected=403)
    assert len(case.objects.calls) == before
    unit = await get(unit_path)
    message_text = "We need 50 pieces."
    located = await locate(
        case,
        "message:" + case.message,
        {"purpose": "need_unit", "need_id": case.need, "action": "confirm"},
        "rfc822-plain-v1",
        None,
        message_text,
    )
    unit_command = {
        "unit": "pieces",
        "source_message_id": case.message,
        "locator": located["locator"],
        "source_quote": message_text,
        "expected_quantity_fact_hash": unit["quantity_fact_hash"],
        "expected_unit_confirmation_id": None,
    }
    receipt = await post(
        f"/needs/{case.need}/unit-confirmations", unit_command, key="unit"
    )
    assert (
        await post(f"/needs/{case.need}/unit-confirmations", unit_command, key="unit")
        == receipt
    )
    assert (await get(unit_path))["unit_confirmation_id"] == receipt["confirmation_id"]
    assert (
        await get(f"/needs/{case.need}/unit-confirmations/{receipt['confirmation_id']}")
        == receipt
    )
    issuer = await post(
        "/issuer",
        {
            "name": "Controlled Supplier",
            "address": "Test address",
            "contact": "sales@example.test",
        },
        key="issuer",
    )
    assert (await get("/issuer"))["content_hash"] == issuer["content_hash"]
    context = await get(context_path)
    assert context["blockers"] == [] and context["context_hash"]
    assert "Private provenance marker" not in json.dumps(context)
    for actor in ("product-reader", "finance-reader"):
        assert (await get(context_path, actor=actor))["need_facts_hash"] == context[
            "need_facts_hash"
        ]
    located = await locate(
        case, case.source, {"purpose": "pricing"}, "pdf-text-v1", 1, case.statement
    )
    valid_until = (case.clock[0] + timedelta(hours=12)).isoformat()
    price_command = {
        "kind": "supplier_price",
        "opportunity_id": case.opportunity,
        "need_id": case.need,
        "supplier_ref": "controlled:supplier",
        "specification": "hardware steel 50 mm cartons",
        "unit": "pieces",
        "destination": "DE",
        "currency": "USD",
        "source_ref": case.source,
        "locator": located["locator"],
        "amount": "2.00",
        "basis": "quoted",
        "quantity_min": 50,
        "quantity_max": 50,
        "moq": 1,
        "quoted_at": case.clock[0].isoformat(),
        "valid_until": valid_until,
    }
    price = await post("/price-evidence", price_command, key="price")
    assert await post("/price-evidence", price_command, key="price") == price
    assert (await get(f"/opportunities/{case.opportunity}/price-evidence"))[0] == price
    policy = await post(
        "/policies",
        {
            "category": None,
            "minimum_margin_rate": "0.10",
            "target_margin_rate": "0.20",
            "cost_groups": {name: "goods" for name in cost_item_type_values()},
            "effective_from": case.clock[0].isoformat(),
            "source_ref": case.source,
        },
        key="policy",
    )
    assert (await get("/policies"))["content_hash"] == policy["content_hash"]
    fx = await post(
        "/quote-fx",
        {
            "base_currency": "USD",
            "quote_currency": "EUR",
            "rate": "0.90",
            "source_ref": case.source,
            "observed_at": case.clock[0].isoformat(),
        },
        key="fx",
    )
    assert await get("/quote-fx/" + fx["fx_id"]) == fx
    sheet = await post(
        f"/opportunities/{case.opportunity}/cost-sheets",
        {
            "version_type": "quoted",
            "quantity": 50,
            "base_currency": "USD",
            "quote_currency": "EUR",
            "fx_snapshot_id": "controlled-cost-fx",
            "fx_rates": [],
        },
        expected=201,
    )
    sheet_id = sheet["cost_sheet_id"]
    await post(
        f"/cost-sheets/{sheet_id}/items",
        {
            "item_type": "product_purchase",
            "amount": "2.00",
            "currency": "USD",
            "price_basis": "quoted",
            "is_per_unit": True,
            "source_ref": price["evidence_id"],
            "note": None,
        },
        expected=204,
    )
    sheet = await get(f"/cost-sheets/{sheet_id}")
    coverage = await post(
        f"/cost-sheets/{sheet_id}/coverage",
        {
            "expected_sheet_hash": sheet["content_hash"],
            "acquisition_mode": "detail",
            "decisions": [
                {
                    "item_type": name,
                    "applicable": name == "product_purchase",
                    "reason": "人工核对",
                    "item_bindings": [
                        {
                            "item_sequence": 1,
                            "evidence_id": price["evidence_id"],
                            "source_line_ref": located["locator"],
                            "allocation_scope": "order:one",
                        }
                    ]
                    if name == "product_purchase"
                    else [],
                }
                for name in cost_item_type_values()
            ],
        },
        key="coverage",
    )
    assert await get(f"/cost-sheets/{sheet_id}/coverage") == coverage
    scope = await post(
        f"/cost-sheets/{sheet_id}/scope-confirmations",
        {
            "coverage_id": coverage["coverage_id"],
            "expected_sheet_hash": sheet["content_hash"],
            "expected_coverage_hash": coverage["content_hash"],
            "expected_need_facts_hash": context["need_facts_hash"],
            "terms": [],
            "valid_until": valid_until,
            "evidence_bindings": [
                {
                    "evidence_id": price["evidence_id"],
                    "evidence_hash": price["evidence_hash"],
                    "applicability_note": "人工核对规格包装目的地数量适用",
                }
            ],
        },
        key="scope",
    )
    assert (await get(f"/cost-sheets/{sheet_id}/scope-confirmations"))[0] == scope
    rounding = {"unit_places": 2, "total_places": 2, "strategy": "ROUND_HALF_UP"}
    options = {
        "mode": "manual",
        "unit_price": {"amount": "4.00", "currency": "EUR"},
        "rounding": rounding,
        "quote_fx_ref": fx["fx_id"],
        "algorithm_version": "costing-v1",
    }
    calculation = await post(f"/cost-sheets/{sheet_id}/calculate", options)
    assert calculation
    command = {
        "opportunity_id": case.opportunity,
        "cost_sheet_id": sheet_id,
        "expected_context_hash": context["context_hash"],
        "expected_sheet_hash": sheet["content_hash"],
        "scope_confirmation_id": scope["confirmation_id"],
        "unit_price": options["unit_price"],
        "rounding": rounding,
        "quote_fx_ref": fx["fx_id"],
        "valid_until": valid_until,
        "terms": [],
        "replaces_quote_id": None,
        "expected_quote_version": None,
    }
    quote = await post(
        f"/opportunities/{case.opportunity}/quotes", command, key="quote"
    )
    assert (
        await post(f"/opportunities/{case.opportunity}/quotes", command, key="quote")
        == quote
    )
    assert quote["prepared_by"] == case.actor and quote["state"] == "draft"
    assert "Private provenance marker" not in json.dumps(quote)
    await get("/quotes/" + quote["quote_id"], actor=case.owner, expected=403)
    return quote


async def cycle(runtime):
    stop = asyncio.Event()

    async def end(interval, event):
        event.set()

    result = await run_scheduler_worker(
        runtime, stop_event=stop, wait=end, install_signal_handlers=False
    )
    assert result.status is WorkerStartStatus.STARTED and result.cycles_completed == 1


async def test_actual_linux_api_worker_quote_approval_file_chain(
    unit_engine, monkeypatch
):
    assert sys.platform == "linux", "not_run：B2同链必须在受限Linux执行"
    async with actual_api_case(unit_engine, monkeypatch) as case:
        assert case.parser.capability().status == "available"
        await seed_runtime_facts(case)
        quote = await prepare_quote(case)
        quote_id = quote["quote_id"]
        run = await runtime_request(
            case, "POST", f"/quotes/{quote_id}/submit", {}, expected=202
        )
        assert run["quote_id"] == quote_id

        class Secrets(_TrackingEnvironmentSecrets):
            def resolve(self, ref):
                if ref in {"TEST_ACCESS", "TEST_SECRET"}:
                    return "controlled-sdk-secret"
                return super().resolve(ref)

        monkeypatch.setattr(worker, "EnvironmentSecretResolver", lambda _: Secrets())
        disabled = worker.SchedulerRuntimeFactory(
            worker_environment(unit_engine, case.tenant, "no_config", database_url=case.database_url),
            _factory_dependencies(worker, with_hunter=False),
            resolver_factory=_FactoryResolver,
            health_server_factory=_FactoryHealthServer,
            now=lambda: case.clock[0],
        )
        async with disabled() as old_runtime:
            before = await old_runtime.workflow.get_run(case.tenant, run["run_id"])
            await cycle(old_runtime)
            assert (
                await old_runtime.workflow.get_run(case.tenant, run["run_id"]) == before
            )
            assert old_runtime.quote_expiry_driver is None
        factory = worker.SchedulerRuntimeFactory(
            worker_environment(unit_engine, case.tenant, "enabled", database_url=case.database_url),
            _factory_dependencies(worker, with_hunter=False),
            resolver_factory=_FactoryResolver,
            health_server_factory=_FactoryHealthServer,
            now=lambda: case.clock[0],
        )
        async with factory() as runtime:
            assert runtime.activation.quotation_lifecycle._parser is not case.parser
            for _ in range(4):
                await cycle(runtime)
            async with case.sessions() as session:
                packages = (
                    await session.scalars(
                        select(ApprovalPackageRow).where(
                            ApprovalPackageRow.tenant_id == case.tenant
                        )
                    )
                ).all()
            if len(packages) != 1:
                fact = await runtime.workflow.get_run(case.tenant, run["run_id"])
                for label, value in (
                    ("step", fact.current_step),
                    ("status", fact.status.value),
                    ("outcome", fact.context.get("outcome", "none")),
                    ("error", fact.context.get("error_code", "none")),
                    ("exception", (fact.last_error or "none").rsplit(": ", 1)[-1]),
                ):
                    if isinstance(value, str) and value.replace("_", "").isalnum():
                        print("runtime_state_" + label + "=" + value)
                print("runtime_package_count=" + str(len(packages)))
            assert len(packages) == 1
            package_id = packages[0].approval_id
            readable = await case.client.get(
                f"/approvals/{package_id}",
                headers={"X-Employee-Id": case.actor, "X-Tenant-Id": case.tenant},
            )
            assert readable.status_code == 200
            display = readable.json()["proposed_change_display"]
            assert display["报价编号"] == quote_id and display["起草员工"] == case.actor
            assert display["本版本·客户整单合计"] == "200.00 EUR"
            assert "比例，1=100%" in display["本版本·折扣空间"]
            assert all(isinstance(value, str) for value in display.values())
            assert "customer" not in display and "calculation" not in display
            assert "Private provenance marker" not in json.dumps(display)
            response = await case.client.post(
                f"/approvals/{package_id}/decide",
                json={"decision": "approve", "reason": None},
                headers={"X-Employee-Id": case.actor, "X-Tenant-Id": case.tenant},
            )
            assert response.status_code == 403
            for changes in (
                {"is_active": False},
                {"role": "finance", "is_active": True},
            ):
                async with case.sessions.begin() as session:
                    await session.execute(
                        update(EmployeeRow)
                        .where(
                            EmployeeRow.tenant_id == case.tenant,
                            EmployeeRow.employee_id == case.decider,
                        )
                        .values(**changes)
                    )
                denied = await case.client.post(
                    f"/approvals/{package_id}/decide",
                    json={"decision": "approve", "reason": None},
                    headers={"X-Employee-Id": case.decider, "X-Tenant-Id": case.tenant},
                )
                if denied.status_code != 403:
                    print("runtime_http_error=status_" + str(denied.status_code))
                assert denied.status_code == 403
            async with case.sessions.begin() as session:
                await session.execute(
                    update(EmployeeRow)
                    .where(
                        EmployeeRow.tenant_id == case.tenant,
                        EmployeeRow.employee_id == case.decider,
                    )
                    .values(role="boss", is_active=True)
                )
            response = await case.client.post(
                f"/approvals/{package_id}/decide",
                json={"decision": "approve", "reason": None},
                headers={"X-Employee-Id": case.decider, "X-Tenant-Id": case.tenant},
            )
            assert response.status_code == 200
            assert response.json()["proposed_change_display"] == display
            for _ in range(7):
                await cycle(runtime)
            final = await runtime_request(case, "GET", f"/quotes/{quote_id}")
            assert final["state"] == "approved"
            file = await runtime_request(case, "POST", f"/quotes/{quote_id}/files", {})
            assert (
                await runtime_request(case, "POST", f"/quotes/{quote_id}/files", {})
                == file
            )
            calls = len(case.objects.calls)
            await runtime_request(
                case,
                "POST",
                f"/quotes/{quote_id}/files",
                {},
                actor="finance-reader",
                expected=403,
            )
            assert len(case.objects.calls) == calls
            pdf = await runtime_request(
                case,
                "GET",
                f"/quotes/{quote_id}/files/{file['file_id']}",
                actor=case.owner,
            )
            assert (
                pdf.content.startswith(b"%PDF-")
                and pdf.headers["content-type"] == "application/pdf"
            )
            page = await runtime_request(
                case,
                "GET",
                f"/opportunities/{case.opportunity}/customer-quote-versions?limit=10",
                actor=case.owner,
            )
            assert quote_id in json.dumps(page)
            assert "Private provenance marker" not in json.dumps(page)
            await assert_gateway_identity_boundaries(case, quote_id)
            assert (
                runtime.activation.quotation_lifecycle._parser.capability().status
                == "available"
            )
            await deliver_quote_result(case, run["run_id"], quote_id)
            case.clock[0] = case.clock[0] + timedelta(hours=12)
            await cycle(runtime)
            await cycle(runtime)
            expired = await runtime_request(case, "GET", f"/quotes/{quote_id}")
            assert expired["state"] == "expired"
            async with case.sessions() as session:
                events = (
                    await session.scalars(
                        select(QuotationStateEventRow).where(
                            QuotationStateEventRow.tenant_id == case.tenant,
                            QuotationStateEventRow.quote_id == quote_id,
                            QuotationStateEventRow.to_state == "expired",
                        )
                    )
                ).all()
            assert len(events) == 1 and events[0].reason == "expiry"
            assert events[0].from_state == "approved" and events[0].actor_id is None
            calls = len(case.objects.calls)
            await runtime_request(
                case,
                "GET",
                f"/quotes/{quote_id}/files/{file['file_id']}",
                actor=case.owner,
                expected=409,
            )
            assert len(case.objects.calls) == calls
            history = await runtime_request(
                case,
                "GET",
                f"/quotes/{quote_id}/files/{file['file_id']}/history",
                actor=case.owner,
            )
            assert (
                history.content == pdf.content
                and "attachment" in history.headers["content-disposition"]
            )


async def assert_gateway_identity_boundaries(case, quote_id):
    from shared.schemas.evidence_read import (
        EvidencePreviewRequest,
        PricingEvidenceScope,
        QuoteEvidenceError,
    )
    from workflows.quote_approval.file_schemas import QuoteFileApplicationError

    # Unicode不能假装latin-1认证头；直接调用真实factory已注入的受信员工端口。
    for actor in ("中文员工", "boss internal", "boss-secret"):
        async with case.sessions.begin() as session:
            session.add(
                EmployeeRow(
                    tenant_id=case.tenant,
                    employee_id=actor,
                    name=actor,
                    role="boss",
                    is_active=True,
                    created_at=case.clock[0],
                )
            )
        composition = case.dependencies.quotation
        metadata = await composition.domain.preparation_reads.get(
            case.tenant, case.opportunity, actor_id=actor
        )
        assert metadata.prepared_by == actor
        before = len(case.objects.calls)
        request = EvidencePreviewRequest(
            operation="preview",
            source_ref=case.source,
            scope=PricingEvidenceScope(purpose="pricing"),
            profile="pdf-text-v1",
            page=1,
        )
        with pytest.raises(QuoteEvidenceError) as error:
            await composition.evidence.preview_reader.read(
                case.tenant, request, actor_id=actor
            )
        assert error.value.code == "gateway_unavailable"
        with pytest.raises(QuoteFileApplicationError) as error:
            await composition.files_application.generate(
                case.tenant, quote_id, actor_id=actor
            )
        assert error.value.detail.code == "invalid_input"
        assert len(case.objects.calls) == before


async def deliver_quote_result(case, run_id, quote_id):
    from apps.notification_worker.runtime import NotificationRoutingPolicy
    from infra.db.repositories.in_app_notifications import (
        PostgresInAppNotificationStore,
    )
    from infra.db.repositories.notification_jobs import PostgresNotificationJobStore
    from infra.db.repositories.notifications import PostgresNotificationDedupStore
    from notification_gateway.channels.in_app import InAppChannel
    from notification_gateway.router import NotificationRouter
    from notification_gateway.templates import FixedNotificationTemplateRenderer
    from shared.schemas.identifiers import new_id

    jobs = PostgresNotificationJobStore(case.sessions, now=lambda: case.clock[0])
    claims = await jobs.claim_due(case.tenant, limit=10, lease_owner="quote-runtime")
    results = [claim for claim in claims if claim.source_event == "QuoteApprovalResult"]
    assert len(results) == 1
    claim = results[0]
    notification = FixedNotificationTemplateRenderer().render(claim)
    assert notification.recipient == case.actor
    assert (
        notification.context.primary_id == quote_id
        and notification.context.secondary_id == run_id
    )
    assert "Private provenance marker" not in repr(notification)
    store = PostgresInAppNotificationStore(case.sessions)

    class NoEmail:
        name = "email"
        calls = 0

        async def deliver(self, notification):
            self.calls += 1
            pytest.fail("LOW报价结果不能外发邮件")

    email = NoEmail()
    router = NotificationRouter(
        PostgresNotificationDedupStore(case.sessions, now=lambda: case.clock[0]),
        NotificationRoutingPolicy(),
    )
    router.register_channel(InAppChannel(store, now=lambda: case.clock[0]))
    router.register_channel(email)
    await router.dispatch(notification)
    await router.dispatch(notification)
    assert email.calls == 0
    assert await jobs.complete(case.tenant, claim.job_id, claim_token=claim.claim_token)
    inbox = await store.list_for_recipient(
        case.tenant, case.actor, limit=10, before=None
    )
    assert (
        len(inbox) == 1
        and inbox[0].relative_link == f"/costing-quotes/quotes/{quote_id}"
    )
    assert (
        await store.list_for_recipient(new_id("tn"), case.actor, limit=10, before=None)
        == ()
    )
