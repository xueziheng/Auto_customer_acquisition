"""报价审批平面展示只格式化已验证事实，不重算数字或展开原件。"""

import importlib
import json
import subprocess
import sys
from datetime import timedelta
from decimal import Decimal, localcontext

import pytest

from domains.quotations import service as public
from domains.quotations.approval_schemas import QuoteApprovalPackagePayload
from shared.schemas.money import FxRate
from shared.schemas.quote_creation import QuoteTerm
from tests.unit.test_quote_approval_contracts import quote_case


def project(payload):
    assert hasattr(public, "project_quote_approval_display"), (
        "缺少报价审批纯中文展示出口"
    )
    return public.project_quote_approval_display(payload)


def payload_case():
    original = quote_case()
    now = original.content.created_at
    rates = (
        FxRate(
            "EUR", "USD", Decimal("1.123400"), now, "https://private.invalid/source"
        ),
        FxRate(
            "EUR",
            "USD",
            Decimal("1.120000"),
            now - timedelta(hours=1),
            "private-source",
        ),
    )
    terms = (
        QuoteTerm(kind="discount", text="Same term"),
        QuoteTerm(kind="discount", text="Same term"),
    )
    quote = quote_case(cost_fx=rates, terms=terms)
    payload = public.quote_approval_payloads(quote, quote_case())[0]
    metrics = payload.calculation.metrics.model_copy(
        update={
            "gross_profit": Decimal("-1.2300"),
            "margin_rate": Decimal("-0.0100"),
            "discount_headroom": Decimal("0.0000"),
            "additional_acquisition_headroom": Decimal("-0.2500"),
        }
    )
    calculation = payload.calculation.model_copy(
        update={"metrics": metrics, "quote_fx": payload.calculation.cost_fx_rates[0]}
    )
    evidence = payload.evidence[0]
    payload = payload.model_copy(
        update={
            "calculation": calculation,
            "evidence": (
                evidence,
                evidence.model_copy(
                    update={
                        "valid_until": None,
                        "kind": "confirmed_expense",
                        "basis": "actual",
                    }
                ),
            ),
        }
    )
    return QuoteApprovalPackagePayload.model_validate_json(payload.model_dump_json())


def test_projection_exposes_complete_explicit_identity_customer_and_policy_fields():
    payload = payload_case()
    result = project(payload)
    expected = {
        "租户编号": payload.tenant_id,
        "报价编号": payload.quote_id,
        "报价版本": str(payload.quote_version),
        "机会编号": payload.opportunity_id,
        "报价内容哈希": payload.content_hash,
        "准备上下文哈希": payload.context_hash,
        "冻结依据编号": payload.basis_id,
        "冻结依据哈希": payload.basis_hash,
        "起草员工": payload.prepared_by,
        "提交时负责人": payload.submitted_owner_id,
        "本版本·公司抬头": payload.customer.issuer_name,
        "本版本·公司地址": payload.customer.issuer_address,
        "本版本·公司联系方式": payload.customer.issuer_contact,
        "本版本·客户名称": payload.customer.account_name,
        "本版本·客户国家": payload.customer.country,
        "本版本·产品描述": payload.customer.line.description,
        "本版本·完整规格": payload.customer.line.specification,
        "本版本·数量": str(payload.customer.line.quantity),
        "本版本·单位": payload.customer.line.unit,
        "本版本·报价有效期": payload.customer.valid_until.isoformat(),
        "本版本·政策编号": payload.policy.policy_id,
        "本版本·政策哈希": payload.policy.content_hash,
        "本版本·政策生效时间": payload.policy.effective_from.isoformat(),
        "本版本·单价舍入位数": str(payload.customer.line.rounding.unit_places),
        "本版本·整单舍入位数": str(payload.customer.line.rounding.total_places),
        "本版本·舍入策略": payload.customer.line.rounding.strategy,
    }
    assert result.items() >= expected.items()
    assert payload.approval_type in result["当前审批类型"]
    for index, kind in enumerate(payload.required_types, 1):
        assert kind in result[f"所需审批 {index}"]
    assert "不自动发送" in result["批准边界"] and "原件访问权" in result["批准边界"]
    assert all(
        isinstance(k, str)
        and isinstance(v, str)
        and any("\u4e00" <= ch <= "\u9fff" for ch in k)
        for k, v in result.items()
    )


def test_projection_preserves_decimal_scale_negatives_units_and_all_metrics_under_low_precision():
    payload = payload_case()
    expected = project(payload)
    with localcontext() as context:
        context.prec = 2
        assert project(payload) == expected
    base = payload.calculation.base_currency
    assert expected["本版本·单件毛利润"] == f"-1.2300 {base} / 单件"
    assert expected["本版本·利润率"] == "-0.0100（比例，1=100%）"
    assert expected["本版本·折扣空间"] == "0.0000（比例，1=100%）"
    assert expected["本版本·额外获客空间"] == f"-0.2500 {base} / 单件"
    metric_labels = {
        "unit_full_cost": "单件完全成本",
        "minimum_price": "单件最低售价",
        "target_price": "单件目标售价",
        "gross_profit": "单件毛利润",
        "contribution_profit": "单件贡献利润",
        "full_cost_profit": "单件完全成本利润",
        "additional_acquisition_headroom": "额外获客空间",
    }
    for name, label in metric_labels.items():
        assert (
            expected["本版本·" + label]
            == f"{getattr(payload.calculation.metrics, name)} {base} / 单件"
        )
    assert (
        expected["本版本·政策最低利润率"]
        == f"{payload.policy.minimum_margin_rate}（比例，1=100%）"
    )
    assert (
        expected["本版本·政策目标利润率"]
        == f"{payload.policy.target_margin_rate}（比例，1=100%）"
    )
    for label, money in (
        ("实际有效单件收入", payload.calculation.effective_unit_revenue),
        ("核算采用客户单价", payload.calculation.displayed_unit_price),
        ("核算采用整单合计", payload.calculation.displayed_total),
        ("客户单价", payload.customer.line.unit_price),
        ("客户整单合计", payload.customer.line.line_total),
    ):
        assert expected["本版本·" + label] == f"{money.amount} {money.currency}"
    assert not any("整单利润" in key or "差额" in key for key in expected)


def test_ordered_duplicate_terms_fx_and_evidence_are_not_merged_and_previous_is_complete():
    payload = payload_case()
    result = project(payload)
    for index, term in enumerate(payload.customer.terms, 1):
        assert result[f"本版本·商业条款 {index}·内容"] == term.text
        assert term.kind in result[f"本版本·商业条款 {index}·类型"]
    for index, rate in enumerate(payload.calculation.cost_fx_rates, 1):
        assert result[f"本版本·成本汇率 {index}·汇率"] == str(rate.rate)
        assert result[f"本版本·成本汇率 {index}·引用编号"] == rate.reference_id
        assert (
            result[f"本版本·成本汇率 {index}·观察时间"] == rate.observed_at.isoformat()
        )
    assert result["本版本·报价汇率·汇率"] == str(payload.calculation.quote_fx.rate)
    for index, evidence in enumerate(payload.evidence, 1):
        assert result[f"证据 {index}·编号"] == evidence.evidence_id
        assert result[f"证据 {index}·哈希"] == evidence.evidence_hash
        assert (
            result[f"证据 {index}·原金额"]
            == f"{evidence.amount.amount} {evidence.amount.currency}"
        )
        assert result[f"证据 {index}·确认员工"] == evidence.confirmed_by
        assert result[f"证据 {index}·确认时间"] == evidence.confirmed_at.isoformat()
    assert result["证据 2·有效期"] == "未指定有效期"
    assert result["证据 2·类别"] == "已确认费用（confirmed_expense）"
    assert result["证据 2·价格基准"] == "实际费用（actual）"
    assert result["证据 1·类别"] == "供应商报价（supplier_price）"
    assert result["证据 1·价格基准"] == "实报价（quoted）"
    previous = payload.previous
    assert result["上一版本·报价编号"] == previous.quote_id
    assert result["上一版本·报价版本"] == str(previous.version)
    assert result["上一版本·报价内容哈希"] == previous.content_hash
    assert result["上一版本·完整规格"] == previous.customer.line.specification
    assert result["上一版本·政策编号"] == previous.policy.policy_id
    assert (
        result["上一版本·单件毛利润"]
        == f"{previous.calculation.metrics.gross_profit} {previous.calculation.base_currency} / 单件"
    )
    assert result["上一版本·成本汇率"] == "无成本汇率快照"
    assert result["上一版本·报价汇率"] == "无报价汇率快照"


def test_empty_optional_sections_are_explicit_and_raw_sources_never_enter_display():
    payload = public.quote_approval_payloads(quote_case(), None)[0]
    result = project(payload)
    assert result["上一版本"] == "无上一版本"
    assert result["本版本·商业条款"] == "无商业条款"
    assert result["本版本·成本汇率"] == "无成本汇率快照"
    assert result["本版本·报价汇率"] == "无报价汇率快照"
    encoded = json.dumps(project(payload_case()), ensure_ascii=False)
    for marker in (
        "private.invalid",
        "private-source",
        "source_quote",
        "source_url",
        "locator",
        "supplier_ref",
        "field_provenance",
        "need_facts",
    ):
        assert marker not in encoded


def test_projection_public_export_is_same_function_and_independent_import():
    project(payload_case())
    module = importlib.import_module("domains.quotations.approval_display")
    assert (
        public.project_quote_approval_display is module.project_quote_approval_display
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from domains.quotations.service import project_quote_approval_display; from workflows.quote_approval.approvals import QuotationApprovalAccess",
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0


async def test_actual_access_adapter_has_pure_bound_display_without_quotation_io():
    from domains.quotations.errors import QuoteApprovalError
    from tests.unit.test_approval_service import quote_service_case, submit_quote
    from workflows.quote_approval.approvals import QuotationApprovalAccess

    svc, _, _, _ = quote_service_case()
    payload, approval_id = await submit_quote(svc)
    fact = await svc.read_fact(payload.tenant_id, approval_id)
    adapter = QuotationApprovalAccess(object())
    assert hasattr(adapter, "display"), "缺少真实审批展示adapter"
    assert adapter.display(fact) == project(payload)
    for update in (
        {"tenant_id": "other"},
        {"contract_namespace": None},
        {"proposed_by_employee": "wrong"},
    ):
        with pytest.raises(QuoteApprovalError):
            adapter.display(fact.model_copy(update=update))


def test_projection_does_not_dump_models_or_accept_untyped_payload(monkeypatch):
    from pydantic import BaseModel

    payload = payload_case()

    def forbidden(*args, **kwargs):
        pytest.fail("纯白名单展示不得递归dump模型")

    monkeypatch.setattr(BaseModel, "model_dump", forbidden)
    monkeypatch.setattr(BaseModel, "model_dump_json", forbidden)
    assert project(payload)["报价编号"] == payload.quote_id
    with pytest.raises(TypeError, match="已验证载荷"):
        project({"quote_id": payload.quote_id})
