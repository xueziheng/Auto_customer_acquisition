"""目录产品提案的规范哈希与纯确定性评估规则。"""

from __future__ import annotations

import hashlib
import json

from pydantic import ValidationError as PydanticValidationError

from domains.products.schemas import (
    CatalogClusterFactsInput,
    CatalogExplanationCode,
    CatalogProposalEvaluationResult,
    CatalogProposalPolicyContent,
    CatalogProposalRuleResult,
    CatalogRuleName,
    CatalogRuleStatus,
)


def catalog_policy_content_hash(content: CatalogProposalPolicyContent) -> str:
    """对完整且严格的策略内容计算稳定 SHA-256，不引入运行时默认值。"""

    validated = CatalogProposalPolicyContent.model_validate(
        content.model_dump(mode="python")
    )
    canonical = json.dumps(
        validated.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _result(
    rule: CatalogRuleName,
    status: CatalogRuleStatus,
    actual_value: int | str | bool | None,
    required_value: int | str | bool | None,
    explanation_code: CatalogExplanationCode,
) -> CatalogProposalRuleResult:
    return CatalogProposalRuleResult.model_validate(
        {
            "rule": rule,
            "status": status,
            "actual_value": actual_value,
            "required_value": required_value,
            "explanation_code": explanation_code,
        }
    )


def _count_result(
    *,
    rule: CatalogRuleName,
    actual: int,
    required: int | None,
    unknown_count: int,
    passed_code: CatalogExplanationCode,
    failed_code: CatalogExplanationCode,
    unknown_code: CatalogExplanationCode,
    not_required_code: CatalogExplanationCode,
) -> CatalogProposalRuleResult:
    if required is None:
        if unknown_count:
            return _result(rule, "unknown", None, None, unknown_code)
        return _result(rule, "not_required", actual, None, not_required_code)
    if actual >= required:
        return _result(rule, "passed", actual, required, passed_code)
    if unknown_count:
        return _result(rule, "unknown", None, required, unknown_code)
    return _result(rule, "failed", actual, required, failed_code)


def _blocked_results(
    policy: CatalogProposalPolicyContent,
) -> tuple[CatalogProposalRuleResult, ...]:
    code: CatalogExplanationCode = "目录事实损坏，评估已阻断"
    requirements: tuple[int | bool | None, ...] = (
        True,
        policy.minimum_distinct_accounts,
        policy.minimum_recurring_accounts,
        policy.minimum_distinct_countries,
        policy.minimum_quantity_unit_accounts,
        True if policy.require_unified_unit else None,
    )
    rules: tuple[CatalogRuleName, ...] = (
        "membership_integrity",
        "distinct_accounts",
        "recurring_accounts",
        "distinct_countries",
        "quantity_unit_coverage",
        "unified_unit",
    )
    return tuple(
        _result(rule, "unknown", None, required, code)
        for rule, required in zip(rules, requirements, strict=True)
    )


def evaluate_catalog_facts(
    policy: CatalogProposalPolicyContent,
    facts: CatalogClusterFactsInput,
) -> CatalogProposalEvaluationResult:
    """按固定顺序评估 Products 自有快照；任何损坏输入统一失败关闭。"""

    try:
        valid_policy = CatalogProposalPolicyContent.model_validate(
            policy.model_dump(mode="python")
        )
        valid_facts = CatalogClusterFactsInput.model_validate(
            facts.model_dump(mode="python")
        )
    except (AttributeError, PydanticValidationError, TypeError, ValueError):
        try:
            safe_policy = CatalogProposalPolicyContent.model_validate(
                policy.model_dump(mode="python")
            )
        except (AttributeError, PydanticValidationError, TypeError, ValueError):
            raise ValueError("目录策略内容无效") from None
        return CatalogProposalEvaluationResult(
            rule_results=_blocked_results(safe_policy),
            overall_passed=False,
            blocked_reason="catalog_facts_invalid",
        )

    recurring = _count_result(
        rule="recurring_accounts",
        actual=valid_facts.recurring_true_account_count,
        required=valid_policy.minimum_recurring_accounts,
        unknown_count=valid_facts.recurring_unknown_account_count,
        passed_code="复购客户数达到策略门槛",
        failed_code="复购客户数未达到策略门槛",
        unknown_code="复购客户事实不完整",
        not_required_code="策略不要求复购客户数",
    )
    countries = _count_result(
        rule="distinct_countries",
        actual=len(valid_facts.known_country_codes),
        required=valid_policy.minimum_distinct_countries,
        unknown_count=valid_facts.unknown_country_account_count,
        passed_code="已知国家数达到策略门槛",
        failed_code="已知国家数未达到策略门槛",
        unknown_code="客户国家事实不完整",
        not_required_code="策略不要求已知国家数",
    )
    quantity_unknown = (
        valid_facts.distinct_account_count
        - valid_facts.quantity_unit_covered_account_count
    )
    quantity = _count_result(
        rule="quantity_unit_coverage",
        actual=valid_facts.quantity_unit_covered_account_count,
        required=valid_policy.minimum_quantity_unit_accounts,
        unknown_count=quantity_unknown,
        passed_code="数量单位覆盖达到策略门槛",
        failed_code="数量单位覆盖未达到策略门槛",
        unknown_code="数量单位事实不完整",
        not_required_code="策略不要求数量单位覆盖",
    )
    if valid_policy.require_unified_unit:
        if valid_facts.unified_unit is not None:
            unified = _result(
                "unified_unit",
                "passed",
                valid_facts.unified_unit,
                True,
                "有效数量单位已经统一",
            )
        elif quantity_unknown:
            unified = _result(
                "unified_unit",
                "unknown",
                None,
                True,
                "统一单位事实不完整",
            )
        else:
            unified = _result(
                "unified_unit",
                "failed",
                None,
                True,
                "有效数量单位不统一",
            )
    elif valid_facts.unified_unit is None:
        unified = _result(
            "unified_unit", "unknown", None, None, "统一单位事实未知"
        )
    else:
        unified = _result(
            "unified_unit",
            "not_required",
            valid_facts.unified_unit,
            None,
            "策略不要求统一单位",
        )

    rules = (
        _result(
            "membership_integrity",
            "passed",
            True,
            True,
            "成员关系与品类完整一致",
        ),
        _result(
            "distinct_accounts",
            (
                "passed"
                if valid_facts.distinct_account_count
                >= valid_policy.minimum_distinct_accounts
                else "failed"
            ),
            valid_facts.distinct_account_count,
            valid_policy.minimum_distinct_accounts,
            (
                "去重客户数达到策略门槛"
                if valid_facts.distinct_account_count
                >= valid_policy.minimum_distinct_accounts
                else "去重客户数未达到策略门槛"
            ),
        ),
        recurring,
        countries,
        quantity,
        unified,
    )
    required_failure = any(
        item.status == "failed"
        or (item.status == "unknown" and item.required_value is not None)
        for item in rules
    )
    return CatalogProposalEvaluationResult(
        rule_results=rules,
        overall_passed=not required_failure,
        blocked_reason=None,
    )


__all__ = ("catalog_policy_content_hash", "evaluate_catalog_facts")
