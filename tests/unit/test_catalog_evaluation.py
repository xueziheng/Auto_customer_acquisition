"""Catalog Product Proposal 纯规则评估。"""

from __future__ import annotations

import importlib
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import ValidationError as PydanticValidationError

from domains.products.catalog_rules import evaluate_catalog_facts
from domains.products.schemas import (
    CatalogClusterFactsInput,
    CatalogProposalEvaluationResult,
    CatalogProposalEvaluationView,
    CatalogProposalPolicyContent,
    CatalogProposalRuleResult,
)
from shared.errors import ValidationError
from shared.schemas.identifiers import (
    CatalogProposalEvaluationId,
    CatalogProposalPolicyVersionId,
    NeedClusterId,
    ProspectAccountId,
    RunId,
    TenantId,
    ValidatedNeedId,
)

NOW = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
HASH_A = "a" * 64


def _blocked_facts_type() -> Any:
    try:
        return importlib.import_module(
            "domains.products.schemas"
        ).CatalogBlockedFactsInput
    except AttributeError as exc:
        pytest.fail(f"RED：CatalogBlockedFactsInput 尚未实现（{exc}）")


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


@pytest.mark.parametrize(
    "payload",
    (
        {
            "rule": "distinct_accounts",
            "status": "passed",
            "actual_value": True,
            "required_value": 3,
            "explanation_code": "去重客户数达到策略门槛",
        },
        {
            "rule": "distinct_accounts",
            "status": "passed",
            "actual_value": -1,
            "required_value": 3,
            "explanation_code": "去重客户数达到策略门槛",
        },
        {
            "rule": "distinct_accounts",
            "status": "passed",
            "actual_value": 2_147_483_648,
            "required_value": 3,
            "explanation_code": "去重客户数达到策略门槛",
        },
        {
            "rule": "distinct_accounts",
            "status": "passed",
            "actual_value": 2,
            "required_value": 3,
            "explanation_code": "去重客户数达到策略门槛",
        },
        {
            "rule": "distinct_accounts",
            "status": "failed",
            "actual_value": 3,
            "required_value": 3,
            "explanation_code": "去重客户数未达到策略门槛",
        },
        {
            "rule": "recurring_accounts",
            "status": "unknown",
            "actual_value": None,
            "required_value": 2,
            "explanation_code": "复购客户事实不完整",
        },
        {
            "rule": "recurring_accounts",
            "status": "unknown",
            "actual_value": 2,
            "required_value": 2,
            "explanation_code": "复购客户事实不完整",
        },
        {
            "rule": "recurring_accounts",
            "status": "not_required",
            "actual_value": 1,
            "required_value": 2,
            "explanation_code": "策略不要求复购客户数",
        },
    ),
)
def test_count_rule_result_rejects_non_exact_or_incoherent_values(
    payload: dict[str, object],
) -> None:
    """公共规则 DTO 拒绝宽松数值、缺失确认计数或矛盾比较。"""

    with pytest.raises(PydanticValidationError):
        CatalogProposalRuleResult.model_validate(payload)


def test_count_rule_unknown_accepts_confirmed_actual_value() -> None:
    """unknown 表示未知账户可能改变结论，不表示已确认计数不可信。"""

    result = CatalogProposalRuleResult(
        rule="recurring_accounts",
        status="unknown",
        actual_value=1,
        required_value=2,
        explanation_code="复购客户事实不完整",
    )

    assert result.actual_value == 1
    assert result.required_value == 2


@pytest.mark.parametrize(
    "payload",
    (
        {
            "rule": "membership_integrity",
            "status": "passed",
            "actual_value": False,
            "required_value": True,
            "explanation_code": "成员关系与品类完整一致",
        },
        {
            "rule": "membership_integrity",
            "status": "passed",
            "actual_value": 1,
            "required_value": True,
            "explanation_code": "成员关系与品类完整一致",
        },
        {
            "rule": "membership_integrity",
            "status": "passed",
            "actual_value": True,
            "required_value": False,
            "explanation_code": "成员关系与品类完整一致",
        },
    ),
)
def test_membership_rule_result_requires_exact_boolean_semantics(
    payload: dict[str, object],
) -> None:
    """成员完整性不能以整数或 false+passed 自相矛盾。"""

    with pytest.raises(PydanticValidationError):
        CatalogProposalRuleResult.model_validate(payload)


@pytest.mark.parametrize(
    "actual_value",
    (" vehicle", "vehicle\n", "veh\x00icle", "veh\u0085icle", "v" * 65),
)
def test_unified_unit_rule_result_rejects_unsafe_text(actual_value: str) -> None:
    """统一单位必须是可展示、可持久化的短文本。"""

    with pytest.raises(PydanticValidationError):
        CatalogProposalRuleResult(
            rule="unified_unit",
            status="passed",
            actual_value=actual_value,
            required_value=True,
            explanation_code="有效数量单位已经统一",
        )


@pytest.mark.parametrize(
    "payload",
    (
        {
            "status": "passed",
            "actual_value": None,
            "required_value": True,
            "explanation_code": "有效数量单位已经统一",
        },
        {
            "status": "failed",
            "actual_value": "vehicle",
            "required_value": True,
            "explanation_code": "有效数量单位不统一",
        },
        {
            "status": "not_required",
            "actual_value": "vehicle",
            "required_value": True,
            "explanation_code": "策略不要求统一单位",
        },
        {
            "status": "unknown",
            "actual_value": "vehicle",
            "required_value": None,
            "explanation_code": "统一单位事实未知",
        },
    ),
)
def test_unified_unit_rule_result_rejects_gate_inconsistency(
    payload: dict[str, object],
) -> None:
    """统一单位规则的 gate、状态和值必须相互自证。"""

    with pytest.raises(PydanticValidationError):
        CatalogProposalRuleResult.model_validate(
            {"rule": "unified_unit", **payload}
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


def test_blocked_facts_envelope_is_strict_minimal_locator() -> None:
    """blocked persistence 只能保留可信 locator/hash，不能夹带损坏业务值。"""

    blocked_facts_type = _blocked_facts_type()
    payload: dict[str, object] = {
        "tenant_id": TenantId("tn_catalog"),
        "cluster_id": NeedClusterId("ncl_catalog"),
        "facts_hash": HASH_A,
    }
    envelope = blocked_facts_type.model_validate(payload)

    assert envelope.model_dump(mode="python") == payload
    for changes in (
        {"tenant_id": TenantId("")},
        {"cluster_id": NeedClusterId(" ncl_catalog")},
        {"facts_hash": "A" * 64},
        {"facts_hash": "a" * 63},
        {"member_count": 3},
    ):
        with pytest.raises(PydanticValidationError):
            blocked_facts_type.model_validate({**payload, **changes})


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
    assert [item.actual_value for item in result.rule_results[2:5]] == [0, 1, 0]
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
    assert unknown.rule_results[2].actual_value == 1
    assert unknown.rule_results[2].required_value == 2
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
    assert [item.required_value for item in result.rule_results] == [
        True,
        3,
        None,
        None,
        None,
        None,
    ]
    tampered_rules = list(result.rule_results)
    tampered_rules[2] = result.rule_results[2].model_copy(
        update={"actual_value": 1}
    )
    with pytest.raises(PydanticValidationError):
        CatalogProposalEvaluationResult(
            rule_results=tuple(tampered_rules),
            overall_passed=False,
            blocked_reason="catalog_facts_invalid",
        )


def test_evaluation_result_rejects_blocked_and_normal_shape_crossovers() -> None:
    """blocked_reason 与六条固定阻断规则必须是不可拆分的一种结果形状。"""

    damaged = CatalogClusterFactsInput.model_construct(
        **{**_facts().model_dump(mode="python"), "member_count": 999}
    )
    blocked = evaluate_catalog_facts(_policy(), damaged)
    normal = evaluate_catalog_facts(_policy(), _facts())

    with pytest.raises(PydanticValidationError):
        CatalogProposalEvaluationResult(
            rule_results=(normal.rule_results[0], *blocked.rule_results[1:]),
            overall_passed=False,
            blocked_reason="catalog_facts_invalid",
        )
    with pytest.raises(PydanticValidationError):
        CatalogProposalEvaluationResult(
            rule_results=blocked.rule_results,
            overall_passed=False,
            blocked_reason=None,
        )
    with pytest.raises(PydanticValidationError):
        CatalogProposalEvaluationResult(
            rule_results=blocked.rule_results,
            overall_passed=True,
            blocked_reason="catalog_facts_invalid",
        )


def test_evaluation_result_rejects_impossible_optional_requirement_shape() -> None:
    """规则各自合法也不能绕过策略中 optional 门槛不得超过客户数的关系。"""

    normal = evaluate_catalog_facts(_policy(), _facts())
    impossible_recurring = CatalogProposalRuleResult(
        rule="recurring_accounts",
        status="passed",
        actual_value=4,
        required_value=4,
        explanation_code="复购客户数达到策略门槛",
    )

    with pytest.raises(PydanticValidationError):
        CatalogProposalEvaluationResult(
            rule_results=(
                *normal.rule_results[:2],
                impossible_recurring,
                *normal.rule_results[3:],
            ),
            overall_passed=True,
            blocked_reason=None,
        )


@pytest.mark.parametrize(
    ("rule", "explanation_code"),
    (
        ("recurring_accounts", "复购客户数达到策略门槛"),
        ("distinct_countries", "已知国家数达到策略门槛"),
        ("quantity_unit_coverage", "数量单位覆盖达到策略门槛"),
    ),
)
def test_evaluation_result_rejects_optional_actual_above_account_count(
    rule: str,
    explanation_code: str,
) -> None:
    """复购、国家与数量覆盖的实际账户数都不能超过去重客户数。"""

    normal = evaluate_catalog_facts(_policy(), _facts())
    position = {
        "recurring_accounts": 2,
        "distinct_countries": 3,
        "quantity_unit_coverage": 4,
    }[rule]
    impossible = CatalogProposalRuleResult.model_validate(
        {
            "rule": rule,
            "status": "passed",
            "actual_value": 4,
            "required_value": 2,
            "explanation_code": explanation_code,
        }
    )
    rules = list(normal.rule_results)
    rules[position] = impossible

    with pytest.raises(PydanticValidationError):
        CatalogProposalEvaluationResult(
            rule_results=tuple(rules),
            overall_passed=True,
            blocked_reason=None,
        )


def test_persisted_evaluation_revalidates_rule_result_contract() -> None:
    """内部实体不得持久化空规则、矛盾 overall 或自由底层异常文本。"""

    facts = _facts()
    result = evaluate_catalog_facts(_policy(), facts)
    payload: dict[str, object] = {
        "tenant_id": facts.tenant_id,
        "evaluation_id": CatalogProposalEvaluationId("cpe_catalog"),
        "cluster_id": facts.cluster_id,
        "policy_version_id": CatalogProposalPolicyVersionId("cpv_catalog"),
        "facts_hash": facts.facts_hash,
        "facts": facts,
        "rule_results": result.rule_results,
        "overall_passed": result.overall_passed,
        "blocked_reason": result.blocked_reason,
        "proposed_by_run": RunId("run_catalog"),
        "created_at": NOW,
    }
    evaluation_type = cast(
        Any,
        importlib.import_module(
            "domains.products.models"
        ).CatalogProposalEvaluation,
    )

    for changes in (
        {"rule_results": ()},
        {"overall_passed": False},
        {"blocked_reason": "database timeout"},
        {
            "facts": CatalogClusterFactsInput.model_construct(
                **{**facts.model_dump(mode="python"), "member_count": 999}
            )
        },
    ):
        with pytest.raises(ValidationError):
            evaluation_type(**{**payload, **changes})


def test_persisted_evaluation_binds_rules_exactly_to_facts() -> None:
    """内部规则即使自身合法，也不能伪造 facts 计数或阻断正常事实。"""

    facts = _facts(
        recurring_true_account_count=1,
        recurring_false_account_count=2,
        recurring_unknown_account_count=0,
    )
    policy = _policy(minimum_recurring_accounts=1)
    result = evaluate_catalog_facts(policy, facts)
    fraudulent_rule = CatalogProposalRuleResult(
        rule="recurring_accounts",
        status="passed",
        actual_value=2,
        required_value=1,
        explanation_code="复购客户数达到策略门槛",
    )
    fraudulent_rules = list(result.rule_results)
    fraudulent_rules[2] = fraudulent_rule
    damaged = CatalogClusterFactsInput.model_construct(
        **{**facts.model_dump(mode="python"), "member_count": 999}
    )
    false_blocked = evaluate_catalog_facts(policy, damaged)
    payload: dict[str, object] = {
        "tenant_id": facts.tenant_id,
        "evaluation_id": CatalogProposalEvaluationId("cpe_catalog_binding"),
        "cluster_id": facts.cluster_id,
        "policy_version_id": CatalogProposalPolicyVersionId("cpv_catalog"),
        "facts_hash": facts.facts_hash,
        "facts": facts,
        "rule_results": result.rule_results,
        "overall_passed": result.overall_passed,
        "blocked_reason": result.blocked_reason,
        "proposed_by_run": RunId("run_catalog"),
        "created_at": NOW,
    }
    evaluation_type = cast(
        Any,
        importlib.import_module(
            "domains.products.models"
        ).CatalogProposalEvaluation,
    )

    for changes in (
        {"rule_results": tuple(fraudulent_rules)},
        {
            "rule_results": false_blocked.rule_results,
            "overall_passed": false_blocked.overall_passed,
            "blocked_reason": false_blocked.blocked_reason,
        },
    ):
        with pytest.raises(ValidationError):
            evaluation_type(**{**payload, **changes})


def test_persisted_blocked_evaluation_uses_only_minimal_facts_envelope() -> None:
    """合法 blocked 记录应可持久化，但不得要求或保留损坏业务字段。"""

    facts = _facts()
    policy = _policy(minimum_recurring_accounts=2)
    damaged = CatalogClusterFactsInput.model_construct(
        **{**facts.model_dump(mode="python"), "member_count": 999}
    )
    blocked_result = evaluate_catalog_facts(policy, damaged)
    blocked_facts_type = _blocked_facts_type()
    envelope = blocked_facts_type(
        tenant_id=facts.tenant_id,
        cluster_id=facts.cluster_id,
        facts_hash=facts.facts_hash,
    )
    evaluation_type = cast(
        Any,
        importlib.import_module(
            "domains.products.models"
        ).CatalogProposalEvaluation,
    )
    payload: dict[str, object] = {
        "tenant_id": facts.tenant_id,
        "evaluation_id": CatalogProposalEvaluationId("cpe_catalog_blocked"),
        "cluster_id": facts.cluster_id,
        "policy_version_id": CatalogProposalPolicyVersionId("cpv_catalog"),
        "facts_hash": facts.facts_hash,
        "facts": envelope,
        "rule_results": blocked_result.rule_results,
        "overall_passed": blocked_result.overall_passed,
        "blocked_reason": blocked_result.blocked_reason,
        "proposed_by_run": RunId("run_catalog"),
        "created_at": NOW,
    }

    persisted = evaluation_type(**payload)

    assert persisted.facts == envelope
    assert set(persisted.facts.model_dump(mode="python")) == {
        "tenant_id",
        "cluster_id",
        "facts_hash",
    }
    normal_result = evaluate_catalog_facts(policy, facts)
    for changes in (
        {
            "rule_results": normal_result.rule_results,
            "overall_passed": normal_result.overall_passed,
            "blocked_reason": normal_result.blocked_reason,
        },
        {"tenant_id": TenantId("tn_other")},
        {"cluster_id": NeedClusterId("ncl_other")},
        {"facts_hash": "b" * 64},
    ):
        with pytest.raises(ValidationError):
            evaluation_type(**{**payload, **changes})


def test_evaluation_view_preserves_blocked_facts_type_safety() -> None:
    """安全视图不得把 blocked envelope 伪装成可读取的完整事实。"""

    facts = _facts()
    damaged = CatalogClusterFactsInput.model_construct(
        **{**facts.model_dump(mode="python"), "member_count": 999}
    )
    blocked_result = evaluate_catalog_facts(_policy(), damaged)
    normal_result = evaluate_catalog_facts(_policy(), facts)
    blocked_facts_type = _blocked_facts_type()
    envelope = blocked_facts_type(
        tenant_id=facts.tenant_id,
        cluster_id=facts.cluster_id,
        facts_hash=facts.facts_hash,
    )
    payload: dict[str, object] = {
        "evaluation_id": CatalogProposalEvaluationId("cpe_catalog_view"),
        "cluster_id": facts.cluster_id,
        "policy_version_id": CatalogProposalPolicyVersionId("cpv_catalog"),
        "facts_hash": facts.facts_hash,
        "facts": envelope,
        "rule_results": blocked_result.rule_results,
        "overall_passed": blocked_result.overall_passed,
        "blocked_reason": blocked_result.blocked_reason,
        "proposed_by_run": RunId("run_catalog"),
        "created_at": NOW,
    }

    view = CatalogProposalEvaluationView.model_validate(payload)

    assert type(view.facts) is blocked_facts_type
    for changes in (
        {
            "rule_results": normal_result.rule_results,
            "overall_passed": normal_result.overall_passed,
            "blocked_reason": normal_result.blocked_reason,
        },
        {
            "facts": facts,
            "rule_results": blocked_result.rule_results,
            "overall_passed": blocked_result.overall_passed,
            "blocked_reason": blocked_result.blocked_reason,
        },
    ):
        with pytest.raises(PydanticValidationError):
            CatalogProposalEvaluationView.model_validate(
                {**payload, **changes}
            )
