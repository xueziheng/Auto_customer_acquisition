"""Catalog Product Proposal 纯规则评估。"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError as PydanticValidationError

from domains.products.catalog_rules import evaluate_catalog_facts
from domains.products.schemas import (
    CatalogClusterFactsInput,
    CatalogProposalPolicyContent,
    CatalogProposalRuleResult,
)
from shared.schemas.identifiers import (
    NeedClusterId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
)

NOW = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
HASH_A = "a" * 64


def test_rule_result_rejects_free_form_explanation() -> None:
    """任意解释字符串会为模型生成的未审计结论打开持久化入口。"""

    with pytest.raises(PydanticValidationError):
        CatalogProposalRuleResult(
            rule="distinct_accounts",
            status="passed",
            actual_value=3,
            required_value=3,
            explanation_code="模型认为这些客户大概足够",
        )

    with pytest.raises(PydanticValidationError):
        CatalogProposalRuleResult(
            rule="distinct_accounts",
            status="passed",
            actual_value=3,
            required_value=3,
            explanation_code="复购客户数达到策略门槛",
        )


def _policy(**changes: object) -> CatalogProposalPolicyContent:
    payload: dict[str, object] = {
        "minimum_distinct_accounts": 3,
        "minimum_recurring_accounts": None,
        "minimum_distinct_countries": None,
        "minimum_quantity_unit_accounts": None,
        "require_unified_unit": False,
    }
    payload.update(changes)
    return CatalogProposalPolicyContent.model_validate(payload)


def _facts(**changes: object) -> CatalogClusterFactsInput:
    payload: dict[str, object] = {
        "tenant_id": TenantId("tn_catalog"),
        "cluster_id": NeedClusterId("ncl_catalog"),
        "cluster_category": "three_wheelers",
        "member_need_ids": (
            ValidatedNeedId("need_1"),
            ValidatedNeedId("need_2"),
            ValidatedNeedId("need_3"),
        ),
        "distinct_account_ids": (
            ProspectAccountId("acc_1"),
            ProspectAccountId("acc_2"),
            ProspectAccountId("acc_3"),
        ),
        "member_count": 3,
        "distinct_account_count": 3,
        "known_country_codes": ("KE",),
        "unknown_country_account_count": 2,
        "recurring_true_account_count": 0,
        "recurring_false_account_count": 0,
        "recurring_unknown_account_count": 3,
        "quantity_unit_covered_account_count": 0,
        "unified_unit": None,
        "safe_total_quantity": None,
        "evidence_summaries": (),
        "display_codes": (),
        "facts_observed_at": NOW,
        "facts_hash": HASH_A,
    }
    payload.update(changes)
    return CatalogClusterFactsInput.model_validate(payload)


def test_controlled_policy_passes_with_non_required_unknown_facts() -> None:
    """把非门槛未知项当阻断项会违背受控策略的明确边界。"""

    result = evaluate_catalog_facts(_policy(), _facts())

    assert [item.rule for item in result.rule_results] == [
        "membership_integrity",
        "distinct_accounts",
        "recurring_accounts",
        "distinct_countries",
        "quantity_unit_coverage",
        "unified_unit",
    ]
    assert [item.status for item in result.rule_results] == [
        "passed",
        "passed",
        "unknown",
        "unknown",
        "unknown",
        "unknown",
    ]
    assert result.overall_passed is True
    assert result.blocked_reason is None
    assert result.rule_results[1].actual_value == 3
    assert result.rule_results[1].required_value == 3
    assert all(item.explanation_code for item in result.rule_results)
    assert all(
        "probability" not in type(item).model_fields for item in result.rule_results
    )
    assert all(
        "explanation" not in type(item).model_fields for item in result.rule_results
    )


def test_fully_known_non_required_facts_are_not_required() -> None:
    """把已知展示项误报为 unknown 会掩盖实际事实覆盖。"""

    result = evaluate_catalog_facts(
        _policy(),
        _facts(
            known_country_codes=("KE", "TZ", "UG"),
            unknown_country_account_count=0,
            recurring_true_account_count=1,
            recurring_false_account_count=2,
            recurring_unknown_account_count=0,
            quantity_unit_covered_account_count=3,
            unified_unit="vehicle",
            safe_total_quantity=30,
        ),
    )

    assert [item.status for item in result.rule_results] == [
        "passed",
        "passed",
        "not_required",
        "not_required",
        "not_required",
        "not_required",
    ]
    assert result.overall_passed is True


def test_required_failed_and_unknown_rules_both_block() -> None:
    """硬门槛的明确不足与事实未知都必须失败关闭。"""

    failed = evaluate_catalog_facts(
        _policy(minimum_recurring_accounts=2),
        _facts(
            recurring_true_account_count=1,
            recurring_false_account_count=2,
            recurring_unknown_account_count=0,
        ),
    )
    unknown = evaluate_catalog_facts(
        _policy(minimum_recurring_accounts=2),
        _facts(
            recurring_true_account_count=1,
            recurring_false_account_count=1,
            recurring_unknown_account_count=1,
        ),
    )

    assert failed.rule_results[2].status == "failed"
    assert failed.overall_passed is False
    assert unknown.rule_results[2].status == "unknown"
    assert unknown.overall_passed is False


def test_required_count_and_unified_unit_rules_have_deterministic_values() -> None:
    """规则映射错位或将混合单位算作统一会错误生成提案。"""

    policy = _policy(
        minimum_recurring_accounts=1,
        minimum_distinct_countries=2,
        minimum_quantity_unit_accounts=3,
        require_unified_unit=True,
    )
    passed = evaluate_catalog_facts(
        policy,
        _facts(
            known_country_codes=("KE", "TZ"),
            unknown_country_account_count=1,
            recurring_true_account_count=1,
            recurring_false_account_count=1,
            recurring_unknown_account_count=1,
            quantity_unit_covered_account_count=3,
            unified_unit="vehicle",
            safe_total_quantity=30,
        ),
    )
    mixed = evaluate_catalog_facts(
        policy,
        _facts(
            known_country_codes=("KE", "TZ"),
            unknown_country_account_count=1,
            recurring_true_account_count=1,
            recurring_false_account_count=1,
            recurring_unknown_account_count=1,
            quantity_unit_covered_account_count=3,
            unified_unit=None,
            safe_total_quantity=None,
        ),
    )

    assert [item.status for item in passed.rule_results] == ["passed"] * 6
    assert passed.rule_results[3].actual_value == 2
    assert passed.rule_results[4].actual_value == 3
    assert passed.rule_results[5].actual_value == "vehicle"
    assert passed.rule_results[5].required_value is True
    assert passed.overall_passed is True
    assert mixed.rule_results[5].status == "failed"
    assert mixed.overall_passed is False


def test_damaged_facts_return_one_fixed_blocked_shape_without_guessed_zeroes() -> None:
    """绕过 DTO 构造的损坏输入也不能被解释为零客户或零覆盖。"""

    damaged = CatalogClusterFactsInput.model_construct(
        **{
            **_facts().model_dump(mode="python"),
            "member_count": 999,
            "facts_hash": "damaged",
        }
    )
    result = evaluate_catalog_facts(_policy(), damaged)

    assert [item.rule for item in result.rule_results] == [
        "membership_integrity",
        "distinct_accounts",
        "recurring_accounts",
        "distinct_countries",
        "quantity_unit_coverage",
        "unified_unit",
    ]
    assert result.overall_passed is False
    assert result.blocked_reason == "catalog_facts_invalid"
    assert result.rule_results[0].status == "unknown"
    assert all(item.actual_value is None for item in result.rule_results)
    assert result.rule_results[0].explanation_code == "目录事实损坏，评估已阻断"
