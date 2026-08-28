"""已验证报价审批载荷的中文纯文本白名单；不计算、不授权、不展开来源。"""

from decimal import Decimal

from domains.quotations.approval_schemas import (
    QuoteApprovalCalculationSummary,
    QuoteApprovalCustomerSummary,
    QuoteApprovalFxSummary,
    QuoteApprovalPackagePayload,
    QuoteApprovalPolicySummary,
)
from shared.schemas.money import Money

_APPROVAL_LABELS = {
    "quote_send": "正式报价发送",
    "margin_floor_override": "最低利润覆盖",
    "discount": "折扣",
    "delivery_commitment": "交货期承诺",
    "payment_terms": "付款条件",
    "certification_commitment": "认证承诺",
}


def _kind(value: str) -> str:
    return f"{_APPROVAL_LABELS[value]}（{value}）"


def _money(value: Money) -> str:
    return f"{value.amount} {value.currency}"


def _ratio(value: Decimal) -> str:
    return f"{value}（比例，1=100%）"


def _customer(value: QuoteApprovalCustomerSummary) -> dict[str, str]:
    line = value.line
    result = {
        "公司抬头": value.issuer_name,
        "公司地址": value.issuer_address,
        "公司联系方式": value.issuer_contact,
        "客户名称": value.account_name,
        "客户国家": value.country,
        "报价行号": str(line.line_number),
        "产品描述": line.description,
        "完整规格": line.specification,
        "数量": str(line.quantity),
        "单位": line.unit,
        "客户单价": _money(line.unit_price),
        "客户整单合计": _money(line.line_total),
        "单价舍入位数": str(line.rounding.unit_places),
        "整单舍入位数": str(line.rounding.total_places),
        "舍入策略": line.rounding.strategy,
        "报价有效期": value.valid_until.isoformat(),
    }
    if not value.terms:
        result["商业条款"] = "无商业条款"
    for index, term in enumerate(value.terms, 1):
        result[f"商业条款 {index}·类型"] = _kind(term.kind)
        result[f"商业条款 {index}·内容"] = term.text
    return result


def _fx(value: QuoteApprovalFxSummary) -> dict[str, str]:
    return {
        "币种对": f"{value.source_currency} → {value.target_currency}",
        "汇率": str(value.rate),
        "观察时间": value.observed_at.isoformat(),
        "引用编号": value.reference_id,
    }


def _calculation(value: QuoteApprovalCalculationSummary) -> dict[str, str]:
    metrics = value.metrics

    def unit(amount: Decimal) -> str:
        return f"{amount} {value.base_currency} / 单件"

    result = {
        "核算币种": value.base_currency,
        "报价币种": value.quote_currency,
        "实际有效单件收入": _money(value.effective_unit_revenue),
        "核算采用客户单价": _money(value.displayed_unit_price),
        "核算采用整单合计": _money(value.displayed_total),
        "单件完全成本": unit(metrics.unit_full_cost),
        "单件最低售价": unit(metrics.minimum_price),
        "单件目标售价": unit(metrics.target_price),
        "单件毛利润": unit(metrics.gross_profit),
        "单件贡献利润": unit(metrics.contribution_profit),
        "单件完全成本利润": unit(metrics.full_cost_profit),
        "利润率": _ratio(metrics.margin_rate),
        "折扣空间": _ratio(metrics.discount_headroom),
        "额外获客空间": unit(metrics.additional_acquisition_headroom),
    }
    if not value.cost_fx_rates:
        result["成本汇率"] = "无成本汇率快照"
    for index, rate in enumerate(value.cost_fx_rates, 1):
        result.update(
            {f"成本汇率 {index}·{key}": text for key, text in _fx(rate).items()}
        )
    if value.quote_fx is None:
        result["报价汇率"] = "无报价汇率快照"
    else:
        result.update(
            {f"报价汇率·{key}": text for key, text in _fx(value.quote_fx).items()}
        )
    return result


def _policy(value: QuoteApprovalPolicySummary) -> dict[str, str]:
    return {
        "政策编号": value.policy_id,
        "政策哈希": value.content_hash,
        "政策分类": value.category if value.category is not None else "未指定分类",
        "政策最低利润率": _ratio(value.minimum_margin_rate),
        "政策目标利润率": _ratio(value.target_margin_rate),
        "政策生效时间": value.effective_from.isoformat(),
    }


def project_quote_approval_display(
    payload: QuoteApprovalPackagePayload,
) -> dict[str, str]:
    """保留原Decimal字符串、有序重复项与前版事实；仅供已授权内部审批展示。"""
    if not isinstance(payload, QuoteApprovalPackagePayload):
        raise TypeError("报价审批展示需要已验证载荷")
    result = {
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
        "当前审批类型": _kind(payload.approval_type),
        "批准边界": "本次批准不自动发送；引用仅供核对，不授予原件访问权。",
    }
    for index, kind in enumerate(payload.required_types, 1):
        result[f"所需审批 {index}"] = _kind(kind)
    current = (
        _customer(payload.customer)
        | _calculation(payload.calculation)
        | _policy(payload.policy)
    )
    result.update({f"本版本·{key}": text for key, text in current.items()})
    for index, evidence in enumerate(payload.evidence, 1):
        fields = {
            "编号": evidence.evidence_id,
            "哈希": evidence.evidence_hash,
            "类别": "供应商报价（supplier_price）"
            if evidence.kind == "supplier_price"
            else "已确认费用（confirmed_expense）",
            "价格基准": "实报价（quoted）"
            if evidence.basis == "quoted"
            else "实际费用（actual）",
            "原金额": _money(evidence.amount),
            "有效期": evidence.valid_until.isoformat()
            if evidence.valid_until is not None
            else "未指定有效期",
            "确认员工": evidence.confirmed_by,
            "确认时间": evidence.confirmed_at.isoformat(),
        }
        result.update({f"证据 {index}·{key}": text for key, text in fields.items()})
    previous = payload.previous
    if previous is None:
        result["上一版本"] = "无上一版本"
    else:
        fields = (
            {
                "报价编号": previous.quote_id,
                "报价版本": str(previous.version),
                "报价内容哈希": previous.content_hash,
            }
            | _customer(previous.customer)
            | _calculation(previous.calculation)
            | _policy(previous.policy)
        )
        result["上一版本"] = "有上一版本，仅展示原事实，不计算差额"
        result.update({f"上一版本·{key}": text for key, text in fields.items()})
    return result
