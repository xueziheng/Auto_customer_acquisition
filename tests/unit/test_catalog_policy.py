"""Catalog Product Proposal 策略契约与规范哈希。"""

from __future__ import annotations

import importlib
import json
from dataclasses import is_dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any, cast

import pytest
from pydantic import ValidationError as PydanticValidationError

from domains.products.catalog_rules import catalog_policy_content_hash
from domains.products.permissions import (
    Phase2ProductAuthorizer,
    ProductAction,
    ProductActor,
    ProductRole,
)
from domains.products.schemas import (
    CatalogClusterFactsInput,
    CatalogEvidenceSummaryInput,
    CatalogProposalPolicyContent,
    CatalogProposalPolicyView,
)
from shared.errors import PermissionDenied, ValidationError
from shared.schemas.identifiers import (
    ApprovalId,
    CatalogProposalPolicyVersionId,
    EmployeeId,
    NeedClusterId,
    ProspectAccountId,
    TenantId,
    ValidatedNeedId,
)

TENANT = TenantId("tn_catalog")
NOW = datetime(2026, 9, 4, 8, 0, tzinfo=UTC)
HASH_A = "a" * 64


def _symbol(module: str, name: str) -> object:
    try:
        return getattr(importlib.import_module(module), name)
    except (ModuleNotFoundError, AttributeError) as exc:
        pytest.fail(f"RED：{module}.{name} 尚未实现（{exc}）")


def _controlled_policy() -> CatalogProposalPolicyContent:
    return CatalogProposalPolicyContent(
        minimum_distinct_accounts=3,
        minimum_recurring_accounts=None,
        minimum_distinct_countries=None,
        minimum_quantity_unit_accounts=None,
        require_unified_unit=False,
    )


def _facts_payload() -> dict[str, object]:
    evidence = CatalogEvidenceSummaryInput(
        source_type="conversation",
        source_id="msg_catalog_1",
        extracted_by="employee",
        confirmed_by="emp_catalog",
        confirmed_at=NOW,
        observed_at=NOW,
        content_hash=HASH_A,
    )
    return {
        "tenant_id": TENANT,
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
        "evidence_summaries": (evidence,),
        "display_codes": (),
        "facts_observed_at": NOW,
        "facts_hash": HASH_A,
    }


def test_controlled_policy_is_strict_frozen_and_hashes_canonical_json() -> None:
    """字段顺序或可变状态不能改变同一策略的内容承诺。"""

    policy = _controlled_policy()
    reordered = CatalogProposalPolicyContent.model_validate(
        {
            "require_unified_unit": False,
            "minimum_quantity_unit_accounts": None,
            "minimum_distinct_countries": None,
            "minimum_recurring_accounts": None,
            "minimum_distinct_accounts": 3,
        }
    )

    assert catalog_policy_content_hash(policy) == catalog_policy_content_hash(reordered)
    assert catalog_policy_content_hash(policy) == (
        "0009fe90557bdef9dd0e0422f242755ccf0c74218b8348db38de28f20b8273ed"
    )
    with pytest.raises(PydanticValidationError, match="extra"):
        CatalogProposalPolicyContent.model_validate(
            {**policy.model_dump(mode="python"), "production_default": True}
        )
    with pytest.raises(PydanticValidationError, match="frozen"):
        policy.minimum_distinct_accounts = 4  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field_name", "bad_value"),
    (
        ("minimum_distinct_accounts", True),
        ("minimum_distinct_accounts", 1),
        ("minimum_distinct_accounts", 0),
        ("minimum_distinct_accounts", -1),
        ("minimum_distinct_accounts", 2_147_483_648),
        ("minimum_recurring_accounts", True),
        ("minimum_recurring_accounts", 0),
        ("minimum_recurring_accounts", -1),
        ("minimum_recurring_accounts", 2_147_483_648),
        ("minimum_distinct_countries", True),
        ("minimum_distinct_countries", 1),
        ("minimum_distinct_countries", 2_147_483_648),
        ("minimum_quantity_unit_accounts", True),
        ("minimum_quantity_unit_accounts", 0),
        ("minimum_quantity_unit_accounts", 2_147_483_648),
    ),
)
def test_policy_rejects_non_storage_safe_thresholds(
    field_name: str, bad_value: object
) -> None:
    """放宽类型、下界或 PG signed-int 上界会产生不可存储或无业务意义策略。"""

    with pytest.raises(PydanticValidationError):
        CatalogProposalPolicyContent.model_validate(
            {**_controlled_policy().model_dump(mode="python"), field_name: bad_value}
        )


@pytest.mark.parametrize(
    "changes",
    (
        {"minimum_distinct_accounts": 3, "minimum_recurring_accounts": 4},
        {"minimum_distinct_accounts": 3, "minimum_quantity_unit_accounts": 4},
        {"require_unified_unit": True},
    ),
)
def test_policy_rejects_incoherent_optional_thresholds(
    changes: dict[str, object],
) -> None:
    """跨字段关系缺失会把不可能满足的策略带入审批。"""

    with pytest.raises(PydanticValidationError):
        CatalogProposalPolicyContent.model_validate(
            {**_controlled_policy().model_dump(mode="python"), **changes}
        )

    # 国家数按规格是唯一可高于客户数的阈值。
    assert CatalogProposalPolicyContent(
        minimum_distinct_accounts=2,
        minimum_recurring_accounts=None,
        minimum_distinct_countries=3,
        minimum_quantity_unit_accounts=2,
        require_unified_unit=True,
    ).minimum_distinct_countries == 3


@pytest.mark.parametrize(
    "changes",
    (
        {"facts_hash": "A" * 64},
        {"facts_hash": "a" * 63},
        {"facts_observed_at": NOW.replace(tzinfo=None)},
        {
            "facts_observed_at": datetime(
                2026, 9, 4, 16, 0, tzinfo=timezone(timedelta(hours=8))
            )
        },
        {"member_count": True},
        {"distinct_account_count": 2},
    ),
)
def test_facts_input_rejects_bad_hash_time_count_or_membership_shape(
    changes: dict[str, object],
) -> None:
    """不严格的事实输入会让损坏快照伪装成可评估事实。"""

    with pytest.raises(PydanticValidationError):
        CatalogClusterFactsInput.model_validate({**_facts_payload(), **changes})


def test_policy_view_is_safe_strict_and_utc() -> None:
    """公共策略视图不得接受底层请求哈希、创建键或非 UTC 时间。"""

    view = CatalogProposalPolicyView(
        policy_version_id=CatalogProposalPolicyVersionId("cpv_catalog"),
        content=_controlled_policy(),
        content_hash=catalog_policy_content_hash(_controlled_policy()),
        base_active_version_id=None,
        proposed_by=EmployeeId("emp_catalog"),
        approval_id=None,
        state="pending_approval",
        created_at=NOW,
        activated_at=None,
        terminal_at=None,
    )
    dumped = json.dumps(view.model_dump(mode="json"), ensure_ascii=False)
    assert "creation_key" not in dumped
    assert "creation_request_hash" not in dumped
    with pytest.raises(PydanticValidationError):
        CatalogProposalPolicyView.model_validate(
            {**view.model_dump(mode="python"), "created_at": NOW.replace(tzinfo=None)}
        )
    with pytest.raises(PydanticValidationError):
        CatalogProposalPolicyView.model_validate(
            {**view.model_dump(mode="python"), "content_hash": "b" * 64}
        )


@pytest.mark.parametrize(
    ("role", "action", "allowed"),
    (
        (ProductRole.PRODUCT, ProductAction.CATALOG_POLICY_PROPOSE, True),
        (ProductRole.SOURCING, ProductAction.CATALOG_POLICY_PROPOSE, True),
        (ProductRole.BOSS, ProductAction.CATALOG_POLICY_PROPOSE, False),
        (ProductRole.SALES, ProductAction.CATALOG_POLICY_PROPOSE, False),
        (ProductRole.SYSTEM, ProductAction.CATALOG_EVALUATE, True),
        (ProductRole.PRODUCT, ProductAction.CATALOG_EVALUATE, False),
        (ProductRole.SYSTEM, ProductAction.CATALOG_SYSTEM_APPLY, True),
        (ProductRole.BOSS, ProductAction.CATALOG_SYSTEM_APPLY, False),
        (ProductRole.SYSTEM, ProductAction.CATALOG_POLICY_READ, True),
        (ProductRole.SYSTEM, ProductAction.CATALOG_PROPOSAL_READ, True),
        (ProductRole.SYSTEM, ProductAction.CATALOG_CULTIVATION_READ, False),
        (ProductRole.FINANCE, ProductAction.CATALOG_PROPOSAL_READ, True),
        (ProductRole.CUSTOMER, ProductAction.CATALOG_CULTIVATION_READ, False),
    ),
)
def test_catalog_permissions_separate_human_reads_from_system_application(
    role: ProductRole, action: ProductAction, allowed: bool
) -> None:
    """把人工或客户角色放入系统应用路径会绕过 durable workflow。"""

    authorizer = Phase2ProductAuthorizer(TENANT)
    actor = ProductActor(actor_id="actor_catalog", role=role, tenant_id=TENANT)
    if allowed:
        assert authorizer.require(actor, action, TENANT).endswith(action.value)
    else:
        with pytest.raises(PermissionDenied):
            authorizer.require(actor, action, TENANT)


def test_catalog_internal_entities_are_frozen_dataclasses() -> None:
    """可变的策略、评估、提案或培养实体会破坏快照与审批绑定。"""

    for name in (
        "CatalogProposalPolicyVersion",
        "CatalogProposalEvaluation",
        "CatalogProductProposal",
        "CatalogCultivationCase",
    ):
        entity = _symbol("domains.products.models", name)
        assert is_dataclass(entity)
        assert entity.__dataclass_params__.frozen is True


def test_policy_entity_recomputes_and_rejects_tampered_content_hash() -> None:
    """仅检查 hash 形状会让被篡改的策略内容冒充已审批版本。"""

    policy_version_type = cast(
        Any, _symbol("domains.products.models", "CatalogProposalPolicyVersion")
    )
    policy_state = cast(
        Any, _symbol("domains.products.models", "CatalogProposalPolicyState")
    )
    with pytest.raises(ValidationError, match="content_hash"):
        policy_version_type(
            tenant_id=TENANT,
            policy_version_id=CatalogProposalPolicyVersionId("cpv_catalog"),
            content=_controlled_policy(),
            content_hash="b" * 64,
            base_active_version_id=None,
            proposed_by=EmployeeId("emp_catalog"),
            creation_key="catalog-policy-key",
            creation_request_hash=HASH_A,
            approval_id=None,
            state=policy_state.PENDING_APPROVAL,
            created_at=NOW,
        )


def test_catalog_service_protocol_exposes_only_bounded_domain_operations() -> None:
    """缺少显式公共入口会迫使 workflow 导入 Products 内部实现。"""

    service = _symbol("domains.products.service", "CatalogProposalService")
    expected = {
        "create_policy_candidate",
        "get_active_policy",
        "list_policy_versions",
        "get_policy_change_snapshot",
        "bind_policy_approval",
        "apply_policy_decision",
        "evaluate_cluster",
        "get_evaluation",
        "list_evaluations",
        "get_proposal",
        "list_proposals",
        "bind_proposal_approval",
        "apply_cultivation_decision",
        "get_cultivation_case",
        "list_cultivation_cases",
    }
    assert expected <= set(service.__dict__)


def test_catalog_approval_decision_input_is_strict_utc_and_self_approval_safe() -> None:
    """workflow 映射若接受伪造类型、非 UTC 或自批事实会绕过审批边界。"""

    decision_type = _symbol(
        "domains.products.schemas", "CatalogApprovalDecisionInput"
    )
    payload = {
        "approval_id": ApprovalId("apr_catalog"),
        "approval_type": "catalog_proposal_policy_change",
        "contract_namespace": "catalog-policy-v1",
        "change_set_ref": f"catalog-policy:cpv_catalog:{HASH_A}",
        "request_hash": HASH_A,
        "state": "approved",
        "proposed_by_run": None,
        "proposed_by_employee": EmployeeId("emp_owner"),
        "owner_employee": EmployeeId("emp_owner"),
        "decided_by_employee": EmployeeId("emp_boss"),
        "decided_at": NOW,
        "expires_at": NOW + timedelta(days=7),
    }
    assert decision_type.model_validate(payload).state == "approved"
    for changes in (
        {"extra": True},
        {"contract_namespace": "catalog-cultivation-v1"},
        {"decided_at": NOW.replace(tzinfo=None)},
        {"decided_by_employee": EmployeeId("emp_owner")},
    ):
        with pytest.raises(PydanticValidationError):
            decision_type.model_validate({**payload, **changes})
