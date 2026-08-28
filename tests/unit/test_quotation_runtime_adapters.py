"""报价装配环和旧员工编号在真实workflow步骤中的窄兼容。"""

import importlib
from unittest.mock import AsyncMock

import pytest

from domains.quotations.errors import QuotationError, QuotationUnavailableError
from tests.unit.test_quotation_contracts import basis_case
from tests.unit.test_quote_approval_contracts import QUOTE, RUN
from workflows.engine.runner import StepStatus, WorkflowRun
from workflows.quote_approval import issuer_reader
from workflows.quote_approval.steps import QuoteApprovalStep


def runtime_readers():
    assert importlib.util.find_spec("workflows.quote_approval.runtime_readers"), (
        "缺少真实当前身份适配"
    )
    return importlib.import_module("workflows.quote_approval.runtime_readers")


@pytest.mark.parametrize("active", [True, False])
async def test_actor_readers_preserve_original_fact_and_only_active_cost_identity(
    active,
):
    module = runtime_readers()
    fact = basis_case()[2].runtime.current_actor.model_copy(
        update={"is_active": active}
    )
    contexts = AsyncMock()
    contexts.read_actor.return_value = fact
    quote = await module.CurrentQuotationActorReader(contexts).read_current(
        fact.tenant_id, fact.employee_id
    )
    assert quote is fact
    costing = await module.CurrentCostingActorReader(contexts).read_current(
        fact.tenant_id, fact.employee_id
    )
    if active:
        assert (costing.actor_id, costing.role, costing.scope.value) == (
            fact.employee_id,
            fact.role,
            "tenant",
        )
    else:
        assert costing is None


@pytest.mark.parametrize(
    "reader", ["CurrentQuotationActorReader", "CurrentCostingActorReader"]
)
async def test_actor_dependency_fault_is_not_missing_identity(reader):
    module = runtime_readers()
    contexts = AsyncMock()
    contexts.read_actor.side_effect = RuntimeError("controlled failure")
    with pytest.raises(RuntimeError):
        await getattr(module, reader)(contexts).read_current("tenant", "employee")


async def test_unconfigured_send_receipt_fails_closed_without_outreach_or_download_fallback():
    module = runtime_readers()
    with pytest.raises(QuotationUnavailableError) as error:
        await module.UnavailableQuoteSendReceiptReader().read(
            "tenant", "attempt", actor_id="employee"
        )
    assert error.value.code == "dependency_unavailable"


def scope_case():
    from shared.schemas.evidence_read import (
        AuthorizedEvidenceReference,
        EvidenceRawMeta,
        PricingEvidenceScope,
    )
    from tests.unit.test_quote_http_projection import frozen_fixture
    from workflows.quote_approval import source_access

    assert hasattr(source_access, "CurrentCostScopeSourceAccess"), (
        "缺少持久依据当前资料ACL绑定"
    )
    evidence = frozen_fixture().price_evidence[0]
    actor = "boss-runtime"
    # 仅构建metadata端口回执；原件、parser、Gateway从未交给scope adapter。
    source = evidence.source.model_copy(
        update={
            "tenant_id": "tn_01KZXT00000000000000000001",
            "artifact_id": "art_01KZXT00000000000000000001",
            "source_ref": "upload:upl_01KZXT00000000000000000001",
        }
    )
    evidence = evidence.model_copy(
        update={"source": source, "source_ref": source.source_ref}
    )
    ref = AuthorizedEvidenceReference(
        tenant_id=source.tenant_id,
        actor_id=actor,
        source_ref=source.source_ref,
        message_id=None,
        conversation_id=None,
        account_id=None,
        scope=PricingEvidenceScope(purpose="pricing"),
        raw=EvidenceRawMeta(
            tenant_id=source.tenant_id,
            artifact_id=source.artifact_id,
            content_hash=source.content_hash,
            size_bytes=10,
            kind="pdf",
            mime_type="application/pdf",
            observed_at=source.observed_at,
        ),
    )
    access = AsyncMock()
    access.authorize.return_value = ref
    return source_access.CurrentCostScopeSourceAccess(access), access, evidence, ref


async def test_scope_access_rechecks_all_persistent_sources_without_read_or_parse():
    adapter, access, evidence, ref = scope_case()
    await adapter.require(ref.tenant_id, (evidence, evidence), actor_id=ref.actor_id)
    assert access.authorize.await_count == 2
    assert len(access.mock_calls) == 2
    assert access.authorize.await_args.args == (
        ref.tenant_id,
        evidence.source.source_ref,
    )


@pytest.mark.parametrize(
    "change", ["tenant_id", "actor_id", "artifact_id", "content_hash", "source_ref"]
)
async def test_scope_access_never_accepts_mismatched_authorized_metadata(change):
    from shared.schemas.evidence_read import QuoteEvidenceError

    adapter, access, evidence, ref = scope_case()
    if change in {"artifact_id", "content_hash"}:
        ref = ref.model_copy(
            update={"raw": ref.raw.model_copy(update={change: "mismatch"})}
        )
    else:
        ref = ref.model_copy(update={change: "mismatch"})
    access.authorize.return_value = ref
    with pytest.raises(QuoteEvidenceError):
        await adapter.require(
            evidence.source.tenant_id, (evidence,), actor_id="boss-runtime"
        )


async def test_scope_access_permission_failure_keeps_costing_permission_category():
    from shared.errors import PermissionDenied
    from shared.schemas.evidence_read import QuoteEvidenceError

    adapter, access, evidence, ref = scope_case()
    access.authorize.side_effect = QuoteEvidenceError("permission_denied")
    with pytest.raises(PermissionDenied):
        await adapter.require(ref.tenant_id, (evidence,), actor_id=ref.actor_id)


@pytest.mark.parametrize(
    "state", ["valid", "missing", "inactive", "cross_tenant", "changed_actor", "fault"]
)
async def test_approval_starter_reads_current_actor_then_calls_real_start_boundary(
    monkeypatch, state
):
    from domains.quotations.errors import QuotationPermissionError
    from workflows.quote_approval import http

    fact = basis_case()[2].runtime.current_actor
    actors, quotes, engine, start = (
        AsyncMock(),
        AsyncMock(),
        AsyncMock(),
        AsyncMock(return_value=RUN),
    )
    actors.read_current.return_value = {
        "missing": None,
        "inactive": fact.model_copy(update={"is_active": False}),
        "cross_tenant": fact.model_copy(update={"tenant_id": "other"}),
        "changed_actor": fact.model_copy(update={"employee_id": "other"}),
    }.get(state, fact)
    if state == "fault":
        actors.read_current.side_effect = RuntimeError("private storage")
    assert hasattr(http, "CurrentQuoteApprovalStarter"), "缺少当前身份审批启动适配"
    monkeypatch.setattr(http, "start_quote_approval", start)
    starter = http.CurrentQuoteApprovalStarter(quotes, engine, actors)
    if state == "valid":
        result = await starter.start(fact.tenant_id, QUOTE, actor_id=fact.employee_id)
        assert (result.quote_id, result.run_id) == (QUOTE, RUN)
        assert start.await_args.args == (engine, quotes, fact.tenant_id, QUOTE)
        assert start.await_args.kwargs["actor"].employee_id == fact.employee_id
        assert start.await_args.kwargs["actor"].role == fact.role
    else:
        with pytest.raises(
            QuotationUnavailableError if state == "fault" else QuotationPermissionError
        ):
            await starter.start(fact.tenant_id, QUOTE, actor_id=fact.employee_id)
        start.assert_not_awaited()


async def test_deferred_issuer_fails_closed_until_bound_then_delegates_real_value():
    assert hasattr(issuer_reader, "DeferredQuoteIssuerReader"), "缺少抬头延迟发布适配"
    service = None
    reader = issuer_reader.DeferredQuoteIssuerReader(lambda: service)
    with pytest.raises(QuotationUnavailableError) as error:
        await reader.get_confirmed("tenant")
    assert error.value.code == "dependency_unavailable"
    service = AsyncMock()
    value = basis_case()[2].issuer
    service.get_confirmed_issuer.return_value = value
    assert await reader.get_confirmed("tenant") is value
    service.get_confirmed_issuer.assert_awaited_once_with("tenant")
    service.get_confirmed_issuer.side_effect = QuotationError("issuer_not_found")
    with pytest.raises(QuotationError) as missing:
        await reader.get_confirmed("tenant")
    assert missing.value.code == "issuer_not_found"


def run_for(prepared, initiated):
    return WorkflowRun(
        RUN,
        "tenant",
        "quote_approval",
        1,
        QUOTE,
        "notify",
        StepStatus.RUNNING,
        basis_case()[3],
        context={
            "quote_id": QUOTE,
            "quote_version": 1,
            "content_hash": "a" * 64,
            "prepared_by": prepared,
            "initiated_by": initiated,
            "outcome": "approved",
        },
    )


@pytest.mark.parametrize(
    "employee", ["boss-runtime", "员工 甲", "legacy.owner", "x" * 40]
)
async def test_real_notify_step_preserves_original_valid_legacy_employee_identity(
    employee,
):
    application, notifier = AsyncMock(), AsyncMock()
    result = await QuoteApprovalStep("notify", application, notifier).execute(
        run_for(employee, employee)
    )
    assert result == ("advance", "complete", {})
    assert notifier.notify.await_args.kwargs["recipient_id"] == employee


@pytest.mark.parametrize("employee", [None, 1, True, "", " x", "x ", "x\n", "x" * 41])
@pytest.mark.parametrize("field", ["prepared", "initiated"])
async def test_real_step_rejects_malformed_employee_before_application_or_notification(
    employee, field
):
    application, notifier = AsyncMock(), AsyncMock()
    values = {"prepared": "emp_valid", "initiated": "emp_valid", field: employee}
    result = await QuoteApprovalStep("notify", application, notifier).execute(
        run_for(**values)
    )
    assert result[0:2] == ("fail", "workflow_binding_invalid")
    assert application.mock_calls == [] and notifier.mock_calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("quote_version", None),
        ("quote_version", True),
        ("quote_version", 0),
        ("quote_version", "1"),
        ("content_hash", None),
        ("content_hash", ""),
        ("content_hash", "g" * 64),
        ("content_hash", "a" * 63),
    ],
)
async def test_real_step_original_strict_binding_validation_is_preserved(field, value):
    application, notifier = AsyncMock(), AsyncMock()
    run = run_for("boss-runtime", "boss-runtime")
    run.context[field] = value
    result = await QuoteApprovalStep("notify", application, notifier).execute(run)
    assert result[0:2] == ("fail", "workflow_binding_invalid")
    assert application.mock_calls == [] and notifier.mock_calls == []
